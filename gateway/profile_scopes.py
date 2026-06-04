from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gateway.session import SessionSource

_SCOPE_RE_ERROR = "Invalid scope name"


@dataclass(frozen=True)
class ProfileScope:
    id: str
    scope: str
    platform: str = ""
    chat_id: str = ""
    thread_id: str = ""
    enabled: bool = True
    label: str = ""
    memory_scope: str = ""
    skill_sets: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProfileScopeConfig:
    default_scope: str = "default"
    scopes: list[ProfileScope] = field(default_factory=list)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def validate_scope_name(name: str) -> str:
    from hermes_cli import profiles as profiles_mod

    normalized = profiles_mod.normalize_profile_name(name or "default")
    try:
        profiles_mod.validate_profile_name(normalized)
    except ValueError as exc:
        raise ValueError(f"{_SCOPE_RE_ERROR}: {exc}") from exc
    return normalized


def _skill_sets(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    mode = _clean_text(raw.get("mode")).lower()
    if mode not in {"", "allow"}:
        raise ValueError("profile scope skill_sets.mode must be 'allow'")
    names_raw = raw.get("names") or []
    if isinstance(names_raw, str):
        names_raw = [names_raw]
    if not isinstance(names_raw, list):
        raise ValueError("profile scope skill_sets.names must be a list")
    names = [_clean_text(item) for item in names_raw if _clean_text(item)]
    return {"mode": mode or "allow", "names": names} if names else {}


def _scope_from_dict(raw: dict[str, Any], index: int) -> ProfileScope:
    platform = _clean_text(raw.get("platform")).lower()
    chat_id = _clean_text(raw.get("chat_id"))
    thread_id = _clean_text(raw.get("thread_id"))
    scope = validate_scope_name(_clean_text(raw.get("scope")) or "default")
    memory_scope = validate_scope_name(_clean_text(raw.get("memory_scope")) or scope)
    route_id = _clean_text(raw.get("id")) or f"{platform}:{chat_id}:{thread_id or '*'}:{scope}:{index}"
    if not platform:
        raise ValueError("Profile scope platform is required")
    if not chat_id:
        raise ValueError("Profile scope chat_id is required")
    return ProfileScope(
        id=route_id,
        enabled=raw.get("enabled", True) is not False,
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
        scope=scope,
        label=_clean_text(raw.get("label")),
        memory_scope=memory_scope,
        skill_sets=_skill_sets(raw.get("skill_sets")),
    )


def normalize_profile_scopes_config(raw: Any) -> ProfileScopeConfig:
    if raw is None:
        return ProfileScopeConfig()
    if not isinstance(raw, dict):
        raise ValueError("profile_scopes must be a mapping")
    default_scope = validate_scope_name(_clean_text(raw.get("default_scope")) or "default")
    raw_scopes = raw.get("scopes") or []
    if not isinstance(raw_scopes, list):
        raise ValueError("profile_scopes.scopes must be a list")
    scopes = []
    for index, item in enumerate(raw_scopes):
        if not isinstance(item, dict):
            raise ValueError("Each profile scope must be a mapping")
        scopes.append(_scope_from_dict(item, index))
    return ProfileScopeConfig(default_scope=default_scope, scopes=scopes)


def profile_scopes_to_dict(config: ProfileScopeConfig) -> dict[str, Any]:
    scopes = []
    for scope in config.scopes:
        item = {
            "id": scope.id,
            "enabled": scope.enabled,
            "platform": scope.platform,
            "chat_id": scope.chat_id,
            "scope": scope.scope,
            "memory_scope": scope.memory_scope,
        }
        if scope.thread_id:
            item["thread_id"] = scope.thread_id
        if scope.label:
            item["label"] = scope.label
        if scope.skill_sets:
            item["skill_sets"] = scope.skill_sets
        scopes.append(item)
    return {"default_scope": config.default_scope, "scopes": scopes}


def _source_platform(source: SessionSource) -> str:
    platform = getattr(source, "platform", "")
    return _clean_text(getattr(platform, "value", platform)).lower()


def resolve_scope_for_source(config: ProfileScopeConfig, source: SessionSource) -> ProfileScope:
    platform = _source_platform(source)
    chat_id = _clean_text(getattr(source, "chat_id", ""))
    thread_id = _clean_text(getattr(source, "thread_id", ""))

    chat_match = None
    for scope in config.scopes:
        if not scope.enabled:
            continue
        if scope.platform != platform or scope.chat_id != chat_id:
            continue
        if scope.thread_id and scope.thread_id == thread_id:
            return scope
        if not scope.thread_id:
            chat_match = scope
    if chat_match is not None:
        return chat_match
    default_scope = config.default_scope or "default"
    return ProfileScope(id="default", scope=default_scope, memory_scope=default_scope)
