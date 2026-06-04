from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gateway.session import SessionSource


@dataclass(frozen=True)
class ProfileRoute:
    id: str
    platform: str
    profile: str
    chat_id: str = ""
    thread_id: str = ""
    enabled: bool = True
    label: str = ""


@dataclass(frozen=True)
class ProfileRouteConfig:
    default_profile: str = "default"
    routes: list[ProfileRoute] = field(default_factory=list)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def validate_profile_name(name: str) -> str:
    from hermes_cli import profiles as profiles_mod

    normalized = profiles_mod.normalize_profile_name(name or "default")
    profiles_mod.validate_profile_name(normalized)
    return normalized


def validate_profile_exists(name: str) -> str:
    from hermes_cli import profiles as profiles_mod

    normalized = validate_profile_name(name)
    if normalized != "default" and not profiles_mod.profile_exists(normalized):
        raise ValueError(f"Profile '{normalized}' does not exist")
    return normalized


def _route_from_dict(raw: dict[str, Any], index: int, *, require_profile_exists: bool = False) -> ProfileRoute:
    platform = _clean_text(raw.get("platform")).lower()
    chat_id = _clean_text(raw.get("chat_id"))
    thread_id = _clean_text(raw.get("thread_id"))
    validator = validate_profile_exists if require_profile_exists else validate_profile_name
    profile = validator(_clean_text(raw.get("profile")) or "default")
    route_id = _clean_text(raw.get("id")) or f"{platform}:{chat_id}:{thread_id or '*'}:{profile}:{index}"
    if not platform:
        raise ValueError("Profile route platform is required")
    if not chat_id:
        raise ValueError("Profile route chat_id is required")
    return ProfileRoute(
        id=route_id,
        enabled=raw.get("enabled", True) is not False,
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
        profile=profile,
        label=_clean_text(raw.get("label")),
    )


def normalize_profile_routes_config(raw: Any, *, require_profile_exists: bool = False) -> ProfileRouteConfig:
    if raw is None:
        return ProfileRouteConfig()
    if not isinstance(raw, dict):
        raise ValueError("profile_routes must be a mapping")
    validator = validate_profile_exists if require_profile_exists else validate_profile_name
    default_profile = validator(_clean_text(raw.get("default_profile")) or "default")
    raw_routes = raw.get("routes") or []
    if not isinstance(raw_routes, list):
        raise ValueError("profile_routes.routes must be a list")
    routes: list[ProfileRoute] = []
    for index, item in enumerate(raw_routes):
        if not isinstance(item, dict):
            raise ValueError("Each profile route must be a mapping")
        routes.append(_route_from_dict(item, index, require_profile_exists=require_profile_exists))
    return ProfileRouteConfig(default_profile=default_profile, routes=routes)


def profile_routes_to_dict(config: ProfileRouteConfig) -> dict[str, Any]:
    routes = []
    for route in config.routes:
        item = {
            "id": route.id,
            "enabled": route.enabled,
            "platform": route.platform,
            "chat_id": route.chat_id,
            "profile": route.profile,
        }
        if route.thread_id:
            item["thread_id"] = route.thread_id
        if route.label:
            item["label"] = route.label
        routes.append(item)
    return {"default_profile": config.default_profile, "routes": routes}


def _source_platform(source: SessionSource) -> str:
    platform = getattr(source, "platform", "")
    return _clean_text(getattr(platform, "value", platform)).lower()


def resolve_profile_for_source(config: ProfileRouteConfig, source: SessionSource) -> str:
    platform = _source_platform(source)
    chat_id = _clean_text(getattr(source, "chat_id", ""))
    thread_id = _clean_text(getattr(source, "thread_id", ""))

    chat_match = None
    for route in config.routes:
        if not route.enabled:
            continue
        if route.platform != platform or route.chat_id != chat_id:
            continue
        if route.thread_id and route.thread_id == thread_id:
            return route.profile
        if not route.thread_id:
            chat_match = route.profile

    return chat_match or config.default_profile or "default"
