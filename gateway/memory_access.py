"""Trusted memory authorization context for gateway-originated requests.

The resolver in this module is deliberately independent from model/tool input.
It accepts only gateway-authenticated facts plus administrator configuration and
produces the complete memory boundary that later provider adapters must obey.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Mapping, Optional

from gateway.session import SessionSource


_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PLATFORM_RE = re.compile(r"^[a-z0-9_-]+$")
_GROUP_CHAT_TYPES = frozenset({"channel", "forum", "group", "supergroup", "thread"})
_DM_CHAT_TYPES = frozenset({"dm", "private"})
_TOP_LEVEL_KEYS = frozenset(
    {"version", "default_private_zone", "default_group_zone", "groups", "principals"}
)
_GROUP_KEYS = frozenset({"id", "platform", "chat_id", "thread_id", "zone"})
_PRINCIPAL_KEYS = frozenset(
    {
        "id",
        "identities",
        "dm_destinations",
        "private_zones",
        "group_memberships",
        "dm_rules",
    }
)
_IDENTITY_KEYS = frozenset({"platform", "user_id", "user_id_alt"})
_DM_DESTINATION_KEYS = frozenset({"platform", "chat_id"})
_DM_RULE_KEYS = frozenset({"scope", "private_zones", "group_memberships"})
_MAX_POLICY_ENTRIES = 1024
_PRINCIPAL_ID_RE = re.compile(r"^p_[a-z2-7]{16,52}$")


@dataclass(frozen=True, order=True)
class MemoryIdentity:
    platform: str
    kind: str
    value: str


@dataclass(frozen=True, order=True)
class MemoryDmDestination:
    platform: str
    chat_id: str


@dataclass(frozen=True, order=True)
class MemoryGroup:
    id: str
    platform: str
    chat_id: str
    thread_id: Optional[str]
    zone: str


@dataclass(frozen=True, order=True)
class MemoryDmRule:
    scope: str
    private_zones: tuple[str, ...]
    group_memberships: tuple[str, ...]


@dataclass(frozen=True, order=True)
class MemoryPrincipal:
    id: str
    identities: tuple[MemoryIdentity, ...]
    dm_destinations: tuple[MemoryDmDestination, ...]
    private_zones: tuple[str, ...]
    group_memberships: tuple[str, ...]
    dm_rules: tuple[MemoryDmRule, ...]


@dataclass(frozen=True)
class MemoryAccessPolicy:
    version: int
    default_private_zone: str
    default_group_zone: str
    groups: tuple[MemoryGroup, ...]
    principals: tuple[MemoryPrincipal, ...]
    fingerprint: str


@dataclass(frozen=True, order=True)
class MemoryGrant:
    id: str
    type: str


@dataclass(frozen=True)
class MemoryDestination:
    kind: str
    platform: str
    chat_id: str
    thread_id: str


@dataclass(frozen=True)
class MemoryAccessContext:
    """Immutable effective ACL generated after gateway authorization."""

    version: int
    authorization_subject: str
    principal_id: Optional[str]
    actor_ref: str
    source_ref: Optional[str]
    destination: MemoryDestination
    profile_name: str
    memory_scope: str
    read_wings: tuple[str, ...]
    write_wing: Optional[str]
    grants: tuple[MemoryGrant, ...]
    policy_fingerprint: str

    @property
    def destination_kind(self) -> str:
        return self.destination.kind

    @property
    def cache_fingerprint(self) -> str:
        """Stable cache discriminator; intentionally excludes message source_ref."""
        payload = {
            "authorization_subject": self.authorization_subject,
            "principal_id": self.principal_id,
            "actor_ref": self.actor_ref,
            "destination": {
                "kind": self.destination.kind,
                "platform": self.destination.platform,
                "chat_id": self.destination.chat_id,
                "thread_id": self.destination.thread_id,
            },
            "profile_name": self.profile_name,
            "memory_scope": self.memory_scope,
            "read_wings": list(self.read_wings),
            "write_wing": self.write_wing,
            "grants": [{"id": grant.id, "type": grant.type} for grant in self.grants],
            "policy_fingerprint": self.policy_fingerprint,
        }
        return _sha256_json(payload)


def _reject_unknown_keys(raw: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
    if any(not isinstance(key, str) for key in raw):
        raise ValueError(f"{path} keys must be strings")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{path} has unknown keys: {', '.join(unknown)}")


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a mapping")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")
    if len(value) > _MAX_POLICY_ENTRIES:
        raise ValueError(f"{path} has too many entries")
    return value


def _required_string(value: Any, path: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    if not value or value != value.strip() or len(value) > max_length:
        raise ValueError(f"{path} must be a non-empty normalized string")
    if "*" in value:
        raise ValueError(f"{path} must not contain wildcards")
    return value


def _component(value: Any, path: str) -> str:
    text = _required_string(value, path, max_length=64)
    if not _COMPONENT_RE.fullmatch(text):
        raise ValueError(f"{path} must match [A-Za-z0-9_-]+")
    return text


def _principal_id(value: Any, path: str) -> str:
    principal_id = _component(value, path)
    if not _PRINCIPAL_ID_RE.fullmatch(principal_id):
        raise ValueError(f"{path} must match opaque principal grammar p_[a-z2-7]{{16,52}}")
    return principal_id


def generate_memory_principal_id() -> str:
    """Generate a 80-bit opaque ID matching ``p_[a-z2-7]{16}``."""
    token = base64.b32encode(secrets.token_bytes(10)).decode("ascii").rstrip("=").lower()
    return f"p_{token}"


def decode_memory_opaque_id_key(value: Any) -> bytes:
    """Decode a canonical base64url secret used only for opaque references."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("HERMES_MEMORY_OPAQUE_ID_KEY must be canonical base64url")
    try:
        raw = value.encode("ascii")
        padding = b"=" * ((4 - len(raw) % 4) % 4)
        decoded = base64.b64decode(raw + padding, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("HERMES_MEMORY_OPAQUE_ID_KEY must be canonical base64url") from exc
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != value or not 32 <= len(decoded) <= 64:
        raise ValueError("HERMES_MEMORY_OPAQUE_ID_KEY must decode to 32..64 bytes")
    return decoded


def generate_memory_opaque_id_key() -> str:
    """Generate a canonical base64url-encoded 256-bit opaque-reference key."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _platform(value: Any, path: str) -> str:
    text = _required_string(value, path, max_length=32).lower()
    if not _PLATFORM_RE.fullmatch(text):
        raise ValueError(f"{path} has invalid characters")
    return text


def _unique_components(value: Any, path: str, *, nonempty: bool = False) -> tuple[str, ...]:
    items = tuple(_component(item, f"{path}[{index}]") for index, item in enumerate(_list(value, path)))
    if nonempty and not items:
        raise ValueError(f"{path} must not be empty")
    if len(set(items)) != len(items):
        raise ValueError(f"{path} contains duplicates")
    return tuple(sorted(items))


def _parse_group(raw: Any, index: int) -> MemoryGroup:
    path = f"memory.access.groups[{index}]"
    item = _mapping(raw, path)
    _reject_unknown_keys(item, _GROUP_KEYS, path)
    thread_id = item.get("thread_id")
    if thread_id is not None:
        thread_id = _required_string(thread_id, f"{path}.thread_id")
    return MemoryGroup(
        id=_component(item.get("id"), f"{path}.id"),
        platform=_platform(item.get("platform"), f"{path}.platform"),
        chat_id=_required_string(item.get("chat_id"), f"{path}.chat_id"),
        thread_id=thread_id,
        zone=_component(item.get("zone"), f"{path}.zone"),
    )


def _parse_identity(raw: Any, principal_index: int, index: int) -> MemoryIdentity:
    path = f"memory.access.principals[{principal_index}].identities[{index}]"
    item = _mapping(raw, path)
    _reject_unknown_keys(item, _IDENTITY_KEYS, path)
    present = [key for key in ("user_id", "user_id_alt") if key in item]
    if len(present) != 1:
        raise ValueError(f"{path} must contain exactly one of user_id or user_id_alt")
    kind = present[0]
    return MemoryIdentity(
        platform=_platform(item.get("platform"), f"{path}.platform"),
        kind=kind,
        value=_required_string(item.get(kind), f"{path}.{kind}"),
    )


def _parse_dm_destination(raw: Any, principal_index: int, index: int) -> MemoryDmDestination:
    path = f"memory.access.principals[{principal_index}].dm_destinations[{index}]"
    item = _mapping(raw, path)
    _reject_unknown_keys(item, _DM_DESTINATION_KEYS, path)
    return MemoryDmDestination(
        platform=_platform(item.get("platform"), f"{path}.platform"),
        chat_id=_required_string(item.get("chat_id"), f"{path}.chat_id"),
    )


def _parse_dm_rule(raw: Any, principal_index: int, index: int) -> MemoryDmRule:
    path = f"memory.access.principals[{principal_index}].dm_rules[{index}]"
    item = _mapping(raw, path)
    _reject_unknown_keys(item, _DM_RULE_KEYS, path)
    return MemoryDmRule(
        scope=_component(item.get("scope"), f"{path}.scope"),
        private_zones=_unique_components(
            item.get("private_zones"), f"{path}.private_zones", nonempty=True
        ),
        group_memberships=_unique_components(
            item.get("group_memberships", []), f"{path}.group_memberships"
        ),
    )


def _parse_principal(raw: Any, index: int) -> MemoryPrincipal:
    path = f"memory.access.principals[{index}]"
    item = _mapping(raw, path)
    _reject_unknown_keys(item, _PRINCIPAL_KEYS, path)
    identities = tuple(
        sorted(
            _parse_identity(identity, index, identity_index)
            for identity_index, identity in enumerate(_list(item.get("identities"), f"{path}.identities"))
        )
    )
    if not identities:
        raise ValueError(f"{path}.identities must not be empty")
    if len(set(identities)) != len(identities):
        raise ValueError(f"{path}.identities contains duplicates")
    dm_destinations = tuple(
        sorted(
            _parse_dm_destination(destination, index, destination_index)
            for destination_index, destination in enumerate(
                _list(item.get("dm_destinations"), f"{path}.dm_destinations")
            )
        )
    )
    if not dm_destinations:
        raise ValueError(f"{path}.dm_destinations must not be empty")
    if len(set(dm_destinations)) != len(dm_destinations):
        raise ValueError(f"{path}.dm_destinations contains duplicates")
    dm_rules = tuple(
        sorted(
            _parse_dm_rule(rule, index, rule_index)
            for rule_index, rule in enumerate(_list(item.get("dm_rules", []), f"{path}.dm_rules"))
        )
    )
    return MemoryPrincipal(
        id=_principal_id(item.get("id"), f"{path}.id"),
        identities=identities,
        dm_destinations=dm_destinations,
        private_zones=_unique_components(
            item.get("private_zones"), f"{path}.private_zones", nonempty=True
        ),
        group_memberships=_unique_components(
            item.get("group_memberships", []), f"{path}.group_memberships"
        ),
        dm_rules=dm_rules,
    )


def _policy_payload(
    *,
    version: int,
    default_private_zone: str,
    default_group_zone: str,
    groups: tuple[MemoryGroup, ...],
    principals: tuple[MemoryPrincipal, ...],
) -> dict[str, Any]:
    return {
        "version": version,
        "default_private_zone": default_private_zone,
        "default_group_zone": default_group_zone,
        "groups": [
            {
                "id": group.id,
                "platform": group.platform,
                "chat_id": group.chat_id,
                "thread_id": group.thread_id,
                "zone": group.zone,
            }
            for group in groups
        ],
        "principals": [
            {
                "id": principal.id,
                "identities": [
                    {"platform": identity.platform, identity.kind: identity.value}
                    for identity in principal.identities
                ],
                "dm_destinations": [
                    {"platform": destination.platform, "chat_id": destination.chat_id}
                    for destination in principal.dm_destinations
                ],
                "private_zones": list(principal.private_zones),
                "group_memberships": list(principal.group_memberships),
                "dm_rules": [
                    {
                        "scope": rule.scope,
                        "private_zones": list(rule.private_zones),
                        "group_memberships": list(rule.group_memberships),
                    }
                    for rule in principal.dm_rules
                ],
            }
            for principal in principals
        ],
    }


def _sha256_json(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def parse_memory_access_policy(raw: Any) -> MemoryAccessPolicy:
    """Strictly validate and normalize ``memory.access`` configuration."""
    item = _mapping(raw, "memory.access")
    _reject_unknown_keys(item, _TOP_LEVEL_KEYS, "memory.access")
    version = item.get("version")
    if type(version) is not int or version != 1:
        raise ValueError("memory.access.version must be integer 1")
    default_private_zone = _component(
        item.get("default_private_zone"), "memory.access.default_private_zone"
    )
    default_group_zone = _component(
        item.get("default_group_zone"), "memory.access.default_group_zone"
    )
    groups = tuple(
        sorted(
            (
                _parse_group(group, index)
                for index, group in enumerate(_list(item.get("groups"), "memory.access.groups"))
            ),
            key=lambda group: group.id,
        )
    )
    principals = tuple(
        sorted(
            (
                _parse_principal(principal, index)
                for index, principal in enumerate(
                    _list(item.get("principals"), "memory.access.principals")
                )
            ),
            key=lambda principal: principal.id,
        )
    )

    group_ids = [group.id for group in groups]
    if len(set(group_ids)) != len(group_ids):
        raise ValueError("memory.access.groups contains duplicate ids")
    group_destinations = [
        (group.platform, group.chat_id, group.thread_id, group.zone) for group in groups
    ]
    if len(set(group_destinations)) != len(group_destinations):
        raise ValueError("memory.access.groups contains duplicate destination zones")
    for group in groups:
        opaque_thread_length = 28 if group.thread_id is not None else 4
        wing_length = 6 + len(group.platform) + 1 + 28 + 1 + opaque_thread_length + 1 + len(group.zone)
        if wing_length > 128:
            raise ValueError(f"group {group.id} produces a wing longer than 128 characters")

    principal_ids = [principal.id for principal in principals]
    if len(set(principal_ids)) != len(principal_ids):
        raise ValueError("memory.access.principals contains duplicate ids")
    identities: dict[MemoryIdentity, str] = {}
    dm_destinations: dict[MemoryDmDestination, str] = {}
    known_groups = set(group_ids)
    for principal in principals:
        for zone in principal.private_zones:
            if len(f"private.{principal.id}.{zone}") > 128:
                raise ValueError(f"principal {principal.id} produces a wing longer than 128 characters")
        for identity in principal.identities:
            previous = identities.setdefault(identity, principal.id)
            if previous != principal.id:
                raise ValueError("memory.access identity belongs to multiple principals")
        for destination in principal.dm_destinations:
            previous = dm_destinations.setdefault(destination, principal.id)
            if previous != principal.id:
                raise ValueError("memory.access DM destination belongs to multiple principals")
        if not set(principal.group_memberships).issubset(known_groups):
            raise ValueError(f"principal {principal.id} references an unknown group membership")
        rule_scopes = [rule.scope for rule in principal.dm_rules]
        if len(set(rule_scopes)) != len(rule_scopes):
            raise ValueError(f"principal {principal.id} has duplicate dm rule scopes")
        for rule in principal.dm_rules:
            if rule.scope not in principal.private_zones:
                raise ValueError(f"principal {principal.id} dm rule has an undeclared scope")
            if not set(rule.private_zones).issubset(principal.private_zones):
                raise ValueError(f"principal {principal.id} dm rule widens private zones")
            if not set(rule.group_memberships).issubset(principal.group_memberships):
                raise ValueError(f"principal {principal.id} dm rule widens group memberships")
            if len(rule.group_memberships) > 16:
                raise ValueError(f"principal {principal.id} dm rule exceeds 16 grants")
            if len(rule.private_zones) + len(rule.group_memberships) > 32:
                raise ValueError(f"principal {principal.id} dm rule exceeds 32 read wings")

    payload = _policy_payload(
        version=version,
        default_private_zone=default_private_zone,
        default_group_zone=default_group_zone,
        groups=groups,
        principals=principals,
    )
    return MemoryAccessPolicy(
        version=version,
        default_private_zone=default_private_zone,
        default_group_zone=default_group_zone,
        groups=groups,
        principals=principals,
        fingerprint=_sha256_json(payload),
    )


def _source_platform(source: SessionSource) -> str:
    platform = getattr(source, "platform", "")
    return str(getattr(platform, "value", platform) or "").strip().lower()


def _source_text(source: SessionSource, name: str) -> Optional[str]:
    value = getattr(source, name, None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opaque_ref(key: bytes, prefix: str, *parts: str) -> str:
    payload = json.dumps(
        ["memory-ref-v1", prefix, *parts], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    digest = hmac.new(key, payload, hashlib.sha256).digest()
    token = base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:26]
    return f"{prefix}_{token}"


def _principal_for_source(
    policy: MemoryAccessPolicy, source: SessionSource, platform: str
) -> Optional[MemoryPrincipal]:
    candidates = []
    for kind in ("user_id", "user_id_alt"):
        value = _source_text(source, kind)
        if value:
            candidates.append(MemoryIdentity(platform=platform, kind=kind, value=value))
    matches = {
        principal
        for principal in policy.principals
        if any(candidate in principal.identities for candidate in candidates)
    }
    if len(matches) != 1:
        return None
    return next(iter(matches))


def _current_zone(source: SessionSource, default: str) -> str:
    raw = _source_text(source, "memory_scope") or default
    if len(raw) > 64 or not _COMPONENT_RE.fullmatch(raw):
        return ""
    return raw


def _group_wing(group: MemoryGroup, key: bytes) -> tuple[str, MemoryDestination]:
    chat_ref = _opaque_ref(key, "c", group.platform, group.chat_id)
    thread_ref = (
        _opaque_ref(key, "t", group.platform, group.chat_id, group.thread_id)
        if group.thread_id is not None
        else "root"
    )
    destination = MemoryDestination(
        kind="group",
        platform=group.platform,
        chat_id=chat_ref,
        thread_id=thread_ref,
    )
    return f"group.{group.platform}.{chat_ref}.{thread_ref}.{group.zone}", destination


def resolve_memory_access_context(
    source: SessionSource,
    policy: MemoryAccessPolicy,
    *,
    request_authorized: bool,
    principal_verified: bool,
    opaque_id_key: bytes,
) -> Optional[MemoryAccessContext]:
    """Resolve an effective boundary without consulting any model/tool args.

    ``request_authorized`` must be set only after gateway authentication.
    ``principal_verified`` is separate because an allowlisted group destination
    may be trusted even when its current participant cannot be canonicalized.
    """
    if type(request_authorized) is not bool or type(principal_verified) is not bool:
        return None
    if not request_authorized or not isinstance(opaque_id_key, bytes) or len(opaque_id_key) < 32:
        return None
    platform = _source_platform(source)
    chat_id = _source_text(source, "chat_id")
    chat_type = str(getattr(source, "chat_type", "") or "dm").strip().lower()
    if not platform or not chat_id:
        return None

    user_id_alt = _source_text(source, "user_id_alt")
    user_id = _source_text(source, "user_id")
    actor_kind, actor_value = (
        ("user_id_alt", user_id_alt) if user_id_alt else ("user_id", user_id)
    )
    actor_ref = (
        _opaque_ref(opaque_id_key, "a", platform, actor_kind, actor_value)
        if actor_value
        else "unknown"
    )
    message_id = _source_text(source, "message_id")
    thread_id = _source_text(source, "thread_id")
    source_ref = (
        _opaque_ref(opaque_id_key, "m", platform, chat_id, thread_id or "root", message_id)
        if message_id
        else None
    )
    principal = _principal_for_source(policy, source, platform) if principal_verified else None
    profile_name = _source_text(source, "profile_name") or "default"

    if chat_type in _DM_CHAT_TYPES:
        if not principal_verified or principal is None:
            return None
        if MemoryDmDestination(platform=platform, chat_id=chat_id) not in principal.dm_destinations:
            return None
        zone = _current_zone(source, policy.default_private_zone)
        if not zone or zone not in principal.private_zones:
            return None
        rule = next((candidate for candidate in principal.dm_rules if candidate.scope == zone), None)
        private_zones = rule.private_zones if rule else (zone,)
        membership_ids = rule.group_memberships if rule else ()
        read_wings = [f"private.{principal.id}.{private_zone}" for private_zone in private_zones]
        grants = []
        groups_by_id = {group.id: group for group in policy.groups}
        for group_id in membership_ids:
            group = groups_by_id[group_id]
            wing, _ = _group_wing(group, opaque_id_key)
            read_wings.append(wing)
            grants.append(MemoryGrant(id=group_id, type="read_from_group"))
        chat_ref = _opaque_ref(opaque_id_key, "c", platform, chat_id)
        return MemoryAccessContext(
            version=policy.version,
            authorization_subject=f"principal:{principal.id}",
            principal_id=principal.id,
            actor_ref=actor_ref,
            source_ref=source_ref,
            destination=MemoryDestination(
                kind="dm", platform=platform, chat_id=chat_ref, thread_id="root"
            ),
            profile_name=profile_name,
            memory_scope=zone,
            read_wings=tuple(sorted(read_wings)),
            write_wing=f"private.{principal.id}.{zone}" if source_ref else None,
            grants=tuple(sorted(grants, key=lambda grant: (grant.type, grant.id))),
            policy_fingerprint=policy.fingerprint,
        )

    if chat_type not in _GROUP_CHAT_TYPES:
        return None
    zone = _current_zone(source, policy.default_group_zone)
    exact_groups = [
        group
        for group in policy.groups
        if group.platform == platform
        and group.chat_id == chat_id
        and group.thread_id == thread_id
        and group.zone == zone
    ]
    if len(exact_groups) != 1:
        return None
    wing, destination = _group_wing(exact_groups[0], opaque_id_key)
    destination_ref = _opaque_ref(
        opaque_id_key, "d", platform, chat_id, thread_id or "root"
    )
    principal_id = principal.id if principal is not None else None
    subject = f"principal:{principal_id}" if principal_id else f"group_context:{destination_ref}"
    return MemoryAccessContext(
        version=policy.version,
        authorization_subject=subject,
        principal_id=principal_id,
        actor_ref=actor_ref,
        source_ref=source_ref,
        destination=destination,
        profile_name=profile_name,
        memory_scope=zone,
        read_wings=(wing,),
        write_wing=wing if source_ref else None,
        grants=(),
        policy_fingerprint=policy.fingerprint,
    )


def resolve_private_disclosure_context(
    source: SessionSource,
    policy: MemoryAccessPolicy,
    *,
    request_authorized: bool,
    principal_verified: bool,
    opaque_id_key: bytes,
) -> Optional[MemoryAccessContext]:
    """Resolve a one-message private-to-group disclosure boundary.

    This deliberately does not widen normal group memory.  It derives both
    the principal and the current exact destination from authenticated
    gateway facts, requires the principal's configured membership in that
    destination, and binds the exceptional read to this message's opaque
    ``source_ref``.  No model argument can construct or select the wings.
    """
    if not principal_verified:
        return None
    normal = resolve_memory_access_context(
        source,
        policy,
        request_authorized=request_authorized,
        principal_verified=True,
        opaque_id_key=opaque_id_key,
    )
    if normal is None or normal.destination_kind != "group" or normal.source_ref is None:
        return None
    platform = _source_platform(source)
    chat_id = _source_text(source, "chat_id")
    thread_id = _source_text(source, "thread_id")
    zone = _current_zone(source, policy.default_group_zone)
    principal = _principal_for_source(policy, source, platform)
    if principal is None or normal.principal_id != principal.id:
        return None
    exact_groups = [
        group
        for group in policy.groups
        if group.platform == platform
        and group.chat_id == chat_id
        and group.thread_id == thread_id
        and group.zone == zone
    ]
    if len(exact_groups) != 1 or exact_groups[0].id not in principal.group_memberships:
        return None
    private_wings = tuple(
        sorted(f"private.{principal.id}.{private_zone}" for private_zone in principal.private_zones)
    )
    if not private_wings:
        return None
    return MemoryAccessContext(
        version=normal.version,
        authorization_subject=f"principal:{principal.id}",
        principal_id=principal.id,
        actor_ref=normal.actor_ref,
        source_ref=normal.source_ref,
        destination=normal.destination,
        profile_name=normal.profile_name,
        memory_scope=normal.memory_scope,
        read_wings=private_wings,
        write_wing=None,
        grants=(MemoryGrant(id=normal.source_ref, type="disclose_private_to_group"),),
        policy_fingerprint=normal.policy_fingerprint,
    )
