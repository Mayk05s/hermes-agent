"""Profile-route updates shared by dashboard and messaging pairing controls."""

import re
from typing import Any, Dict, Optional


def pairing_route_id(platform: str, chat_id: str, thread_id: str = "") -> str:
    raw = f"pairing-{platform}-{chat_id}-{thread_id or 'chat'}"
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-") or "pairing-route"


def upsert_pairing_profile_route(
    pairing_result: Dict[str, Any],
    profile: str = "default",
) -> Optional[Dict[str, Any]]:
    """Map an approved chat pairing to an explicit Hermes profile route."""
    if (pairing_result.get("subject_type") or "user") != "chat":
        return None

    from gateway.profile_routing import (
        normalize_profile_routes_config,
        profile_routes_to_dict,
    )
    from hermes_cli.config import load_config, save_config

    platform = str(pairing_result.get("platform") or "").lower().strip()
    chat_id = str(pairing_result.get("chat_id") or "").strip()
    thread_id = str(pairing_result.get("thread_id") or "").strip()
    if not platform or not chat_id:
        return None

    cfg = load_config() or {}
    routes_cfg = normalize_profile_routes_config(
        cfg.get("profile_routes"),
        require_profile_exists=True,
    )
    routes_data = profile_routes_to_dict(routes_cfg)
    routes = list(routes_data.get("routes") or [])
    label = (
        str(pairing_result.get("chat_name") or "").strip()
        or str(pairing_result.get("user_name") or "").strip()
        or chat_id
    )
    next_route: Dict[str, Any] = {
        "id": pairing_route_id(platform, chat_id, thread_id),
        "enabled": True,
        "platform": platform,
        "chat_id": chat_id,
        "profile": profile,
        "label": label,
    }
    if thread_id:
        next_route["thread_id"] = thread_id
    else:
        # A chat-wide approval supersedes narrower topic routes created by the
        # same onboarding flow.
        routes = [
            route
            for route in routes
            if not (
                str(route.get("platform") or "").lower().strip() == platform
                and str(route.get("chat_id") or "").strip() == chat_id
                and str(route.get("thread_id") or "").strip()
            )
        ]

    replaced = False
    for index, route in enumerate(routes):
        if (
            str(route.get("platform") or "").lower().strip() == platform
            and str(route.get("chat_id") or "").strip() == chat_id
            and str(route.get("thread_id") or "").strip() == thread_id
        ):
            routes[index] = {**route, **next_route}
            replaced = True
            break
    if not replaced:
        routes.append(next_route)

    cfg["profile_routes"] = {
        "default_profile": routes_data.get("default_profile") or "default",
        "routes": routes,
    }
    save_config(cfg)
    return next_route


def upsert_pairing_chat_settings(
    pairing_result: Dict[str, Any],
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Replace the target chat's explicit settings from onboarding choices."""
    if (pairing_result.get("subject_type") or "user") != "chat":
        return None

    from gateway.chat_settings import (
        INHERIT,
        SETTING_FIELDS,
        chat_settings_to_dict,
        normalize_chat_settings_config,
    )
    from hermes_cli.config import load_config, save_config

    platform = str(pairing_result.get("platform") or "").lower().strip()
    chat_id = str(pairing_result.get("chat_id") or "").strip()
    if not platform or not chat_id:
        return None

    cfg = load_config() or {}
    normalized = normalize_chat_settings_config(cfg.get("chat_settings"))
    current = chat_settings_to_dict(normalized)
    items = [
        item
        for item in current.get("settings", [])
        if not (
            str(item.get("platform") or "").lower().strip() == platform
            and str(item.get("chat_id") or "").strip() == chat_id
        )
    ]

    requested = {
        field_name: settings.get(field_name, INHERIT)
        for field_name in SETTING_FIELDS
    }
    explicit = any(value != INHERIT for value in requested.values())
    saved_item: Optional[Dict[str, Any]] = None
    if explicit:
        saved_item = {
            "platform": platform,
            "chat_id": chat_id,
            "label": (
                str(pairing_result.get("chat_name") or "").strip()
                or str(pairing_result.get("user_name") or "").strip()
                or chat_id
            ),
            **requested,
        }
        items.append(saved_item)

    cfg["chat_settings"] = chat_settings_to_dict(
        normalize_chat_settings_config({
            "defaults": current.get("defaults", {}),
            "settings": items,
        })
    )
    save_config(cfg)
    return saved_item
