"""Opt-in exact trusted-source provider routing; no model-controlled selectors.

Routes bind either a verified DM or one exact group destination. Nonmatching
chats retain their existing provider unless an explicit shared-chat denial
template is enabled. Matching broken routes fail closed.
"""
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

from gateway.memory_access import (
    MemoryAccessContext, MemoryGroup, decode_memory_opaque_id_key,
    parse_memory_access_policy, resolve_memory_access_context,
    resolve_private_disclosure_context,
)


@dataclass(frozen=True)
class MemoryProviderRoute:
    provider: str
    context: MemoryAccessContext
    credential_file: str
    client_config: str = '{}'
    automatic_config: str = ''

    @property
    def provider_options(self):
        return {'credential_file': self.credential_file, 'client_config': json.loads(self.client_config),
                'automatic_config': self.automatic_config}

    @property
    def cache_fingerprint(self):
        # Credentials are never cached or serialized; changes select a new
        # agent without putting a credential digest in user-facing logs.
        payload = [self.provider, self.context.cache_fingerprint, self.credential_file, self.client_config, self.automatic_config]
        return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def read_only_group_memory_route(route: MemoryProviderRoute) -> MemoryProviderRoute:
    """Return a turn-local group route that cannot persist any content.

    The original route is immutable and is resolved again on the next normal
    turn, so ordinary group writes are restored automatically.  ``write_wing``
    participates in both context and route cache fingerprints, forcing the
    disclosure turn onto a distinct agent instance.
    """
    if type(route) is not MemoryProviderRoute:
        raise ValueError("Typed memory provider route required")
    context = route.context
    if (
        context.destination_kind != "group"
        or len(context.read_wings) != 1
        or context.grants
        or context.write_wing not in (None, context.read_wings[0])
    ):
        raise ValueError("Ordinary group memory route required")
    return replace(route, context=replace(context, write_wing=None))


def verified_memory_identity(source, pairing_store=None, *, environ=None) -> bool:
    """Only individual exact allowlists/pairing, never allow-all/chat grants."""
    platform = getattr(getattr(source, 'platform', None), 'value', '')
    user_id = str(getattr(source, 'user_id', '') or '')
    if not platform or not user_id or getattr(source, 'is_bot', False):
        return False
    environment = os.environ if environ is None else environ
    for key in (f'{platform.upper()}_ALLOWED_USERS', 'GATEWAY_ALLOWED_USERS'):
        entries = str(environment.get(key, '')).split(',')
        if user_id in {item.strip() for item in entries if item.strip() != '*'}:
            return True
    return bool(pairing_store is not None and pairing_store.is_approved(platform, user_id))


def resolve_memory_provider_route(source, config: Mapping[str, Any], *, request_authorized: bool,
                                  principal_verified: bool) -> Optional[MemoryProviderRoute]:
    raw = (config.get('memory') or {}).get('trusted_provider_route')
    routes = (config.get('memory') or {}).get('trusted_provider_routes', [])
    if not isinstance(routes, list):
        raise ValueError('Invalid trusted memory routes')
    resolved = []
    for candidate in ([raw] if raw else []) + routes:
        if not isinstance(candidate, dict):
            raise ValueError('Invalid trusted memory route')
        if candidate.get('enabled') is not True:
            continue
        if candidate.get('destination_kind') == 'group':
            route = _resolve_group_route(source, candidate, request_authorized=request_authorized,
                                         principal_verified=principal_verified)
        else:
            route = _resolve_dm_route(source, candidate, request_authorized=request_authorized,
                                      principal_verified=principal_verified)
        if route is not None:
            resolved.append(route)
    if len(resolved) > 1:
        raise ValueError('Ambiguous trusted memory routes')
    if resolved:
        return resolved[0]
    return _resolve_shared_denial(source, (config.get('memory') or {}).get('trusted_shared_deny_route'),
        request_authorized=request_authorized, principal_verified=principal_verified)


def _resolve_shared_denial(source, raw, *, request_authorized, principal_verified):
    """Opt-in EMPTY boundary for unregistered shared Telegram destinations.

    Only trusted local configuration supplies provider/policy credentials. The
    actual source supplies the denied destination, never a granting selector.
    Force an empty topic map and omit automatic ingestion even if a template
    is accidentally configured with extra route fields.
    """
    platform = getattr(getattr(source, 'platform', None), 'value', '')
    if platform != 'telegram' or getattr(source, 'chat_type', '') not in {
        'group', 'supergroup', 'forum', 'channel', 'thread'
    }:
        return None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError('Invalid trusted shared memory denial template')
    if type(raw.get('enabled')) is not bool:
        raise ValueError('Invalid trusted shared memory denial enabled flag')
    if raw['enabled'] is False:
        return None
    if raw.get('platform') != 'telegram':
        raise ValueError('Invalid trusted shared memory denial platform')
    candidate = {key: raw[key] for key in (
        'provider', 'policy_file', 'opaque_id_key_file', 'credential_file', 'client_config'
    ) if key in raw}
    candidate.update(match={'platform': platform, 'chat_id': str(getattr(source, 'chat_id', '') or '')},
                     topic_scopes={})
    return _resolve_group_route(source, candidate, request_authorized=request_authorized,
                                principal_verified=principal_verified)


def resolve_private_disclosure_route(
    source,
    config: Mapping[str, Any],
    *,
    request_authorized: bool,
    principal_verified: bool,
) -> Optional[MemoryProviderRoute]:
    """Resolve an exceptional private-read route for one exact group event.

    Only an already enabled, exact group provider route may participate.  In
    particular, the empty boundary used for newly discovered topics is not a
    disclosure route and cannot be upgraded by the trigger phrase.
    """
    routes = (config.get("memory") or {}).get("trusted_provider_routes", [])
    if not isinstance(routes, list):
        raise ValueError("Invalid trusted memory routes")
    resolved = []
    for raw in routes:
        if not isinstance(raw, dict):
            raise ValueError("Invalid trusted memory route")
        if raw.get("enabled") is not True or raw.get("destination_kind") != "group":
            continue
        match = raw.get("match")
        if not isinstance(match, dict) or set(match) != {"platform", "chat_id"}:
            raise ValueError("Invalid trusted group memory route match")
        platform = getattr(getattr(source, "platform", None), "value", "")
        chat_id = str(getattr(source, "chat_id", "") or "")
        if platform != match.get("platform") or chat_id != match.get("chat_id"):
            continue
        if not request_authorized or not principal_verified:
            return None
        thread_id = str(getattr(source, "thread_id", "") or "")
        scopes = raw.get("topic_scopes")
        if (
            not isinstance(scopes, dict)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in scopes.items())
            or thread_id not in scopes
        ):
            return None
        policy = parse_memory_access_policy(json.loads(Path(raw["policy_file"]).read_text()))
        key = decode_memory_opaque_id_key(Path(raw["opaque_id_key_file"]).read_text().strip())
        context = resolve_private_disclosure_context(
            replace(source, memory_scope=scopes[thread_id]),
            policy,
            request_authorized=True,
            principal_verified=True,
            opaque_id_key=key,
        )
        if context is None:
            return None
        provider = raw.get("provider")
        credential_file = raw.get("credential_file")
        if (
            not isinstance(provider, str)
            or not provider.strip()
            or not isinstance(credential_file, str)
            or not Path(credential_file).is_absolute()
        ):
            raise ValueError("Trusted memory route provider unavailable")
        resolved.append(
            MemoryProviderRoute(
                provider,
                context,
                credential_file,
                json.dumps(raw.get("client_config", {}), sort_keys=True),
            )
        )
    if len(resolved) > 1:
        raise ValueError("Ambiguous trusted private disclosure routes")
    return resolved[0] if resolved else None


def _resolve_group_route(source, raw, *, request_authorized, principal_verified):
    match = raw.get('match')
    if not isinstance(match, dict) or set(match) != {'platform', 'chat_id'}:
        raise ValueError('Invalid trusted group memory route match')
    if any(not isinstance(v, str) or not v or '*' in v for v in match.values()):
        raise ValueError('Invalid trusted group memory route fields')
    platform = getattr(getattr(source, 'platform', None), 'value', '')
    if platform != match['platform'] or str(getattr(source, 'chat_id', '') or '') != match['chat_id']:
        return None
    if getattr(source, 'chat_type', '') not in {'group', 'supergroup', 'forum', 'channel', 'thread'}:
        raise ValueError('Trusted group memory route requires a group')
    if not request_authorized:
        raise ValueError('Trusted group memory route requires authorized request')
    policy = parse_memory_access_policy(json.loads(Path(raw['policy_file']).read_text()))
    key = decode_memory_opaque_id_key(Path(raw['opaque_id_key_file']).read_text().strip())
    thread_id = str(getattr(source, 'thread_id', '') or '')
    scopes = raw.get('topic_scopes')
    if not isinstance(scopes, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in scopes.items()):
        raise ValueError('Invalid trusted topic scopes')
    zone = scopes.get(thread_id)
    if zone is None:
        # New topics receive a typed but deliberately EMPTY boundary. They do
        # not fall back to profile files/history or gain an unregistered API
        # capability. An administrator must enroll the exact destination.
        zone = policy.default_group_zone
        placeholder = MemoryGroup('unregistered_destination', platform, match['chat_id'], thread_id or None, zone)
        closed_policy = replace(policy, groups=(placeholder,))
        context = resolve_memory_access_context(replace(source, memory_scope=zone), closed_policy,
            request_authorized=True, principal_verified=principal_verified, opaque_id_key=key)
        if context is None:
            raise ValueError('Cannot resolve closed group memory context')
        context = replace(context, read_wings=(), write_wing=None, grants=())
    else:
        context = resolve_memory_access_context(replace(source, memory_scope=zone), policy,
            request_authorized=True, principal_verified=principal_verified, opaque_id_key=key)
        if context is None or context.destination_kind != 'group':
            raise ValueError('Trusted group memory scope unavailable')
    provider = raw.get('provider')
    credential_file = raw.get('credential_file')
    if not isinstance(provider, str) or not provider.strip() or not isinstance(credential_file, str) or not Path(credential_file).is_absolute():
        raise ValueError('Trusted memory route provider unavailable')
    return MemoryProviderRoute(provider, context, credential_file, json.dumps(raw.get('client_config', {}), sort_keys=True), str(raw.get('automatic_config') or ''))


def _resolve_dm_route(source, raw, *, request_authorized, principal_verified):
    if not raw or raw.get('enabled') is not True:
        return None
    match = raw.get('match')
    if not isinstance(match, dict):
        raise ValueError('Invalid trusted memory route match')
    expected = {'platform', 'user_id', 'chat_id', 'chat_type', 'profile_name', 'thread_id'}
    if set(match) != expected or any(not isinstance(value, str) for value in match.values()):
        raise ValueError('Invalid trusted memory route fields')
    actual = {name: str(getattr(source, name, '') or '') for name in expected}
    actual['platform'] = getattr(getattr(source, 'platform', None), 'value', '')
    if actual != match:
        return None
    if match['chat_type'] != 'dm' or match['thread_id'] or not request_authorized or not principal_verified:
        raise ValueError('Trusted memory route requires verified root DM')
    if not match['user_id'] or match['user_id'] != match['chat_id'] or getattr(source, 'user_id_alt', None):
        raise ValueError('Trusted memory route identity mismatch')
    policy = parse_memory_access_policy(json.loads(Path(raw['policy_file']).read_text()))
    key = decode_memory_opaque_id_key(Path(raw['opaque_id_key_file']).read_text().strip())
    # Scope is fixed by trusted route configuration, never model-supplied.
    scoped_source = replace(source, memory_scope=raw['memory_scope'])
    context = resolve_memory_access_context(scoped_source, policy, request_authorized=True,
                                            principal_verified=True, opaque_id_key=key)
    if context is None or context.destination_kind != 'dm' or context.principal_id is None:
        raise ValueError('Trusted memory route cannot resolve context')
    own_wing = f'private.{context.principal_id}.{context.memory_scope}'
    if own_wing not in context.read_wings:
        raise ValueError('Trusted memory route scope is unavailable')
    # Temporary canary intentionally narrows existing federation policy.
    context = replace(context, read_wings=(own_wing,), grants=())
    provider = raw.get('provider')
    credential_file = raw.get('credential_file')
    if not isinstance(provider, str) or not provider.strip() or not isinstance(credential_file, str) or not Path(credential_file).is_absolute():
        raise ValueError('Trusted memory route provider unavailable')
    return MemoryProviderRoute(provider, context, credential_file, json.dumps(raw.get('client_config', {}), sort_keys=True), str(raw.get('automatic_config') or ''))
