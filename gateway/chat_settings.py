from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from gateway.session import SessionSource


INHERIT = "default"

TRISTATE_FIELDS = {
    "transcribe_audio",
    "show_reasoning",
    "interim_assistant_messages",
    "long_running_notifications",
    "busy_ack_detail",
    "cleanup_progress",
    "streaming",
    "gateway_restart_notification",
}
SETTING_FIELDS = (
    "response_mode",
    "transcribe_audio",
    "reply_to_mode",
    "tool_progress",
    "show_reasoning",
    "tool_preview_length",
    "interim_assistant_messages",
    "long_running_notifications",
    "busy_ack_detail",
    "cleanup_progress",
    "streaming",
    "gateway_restart_notification",
)
DEFAULT_SETTINGS = {name: INHERIT for name in SETTING_FIELDS}


@dataclass(frozen=True)
class ChatSetting:
    platform: str
    chat_id: str
    label: str = ""
    values: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return chat_setting_key(self.platform, self.chat_id)


@dataclass(frozen=True)
class ChatSettingsConfig:
    defaults: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    settings: list[ChatSetting] = field(default_factory=list)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def chat_setting_key(platform: str, chat_id: str) -> str:
    return f"{_clean_text(platform).lower()}:{_clean_text(chat_id)}:"


def _coerce_tristate(value: Any, *, field_name: str) -> str:
    if value is None or value == "":
        return INHERIT
    if isinstance(value, bool):
        return "on" if value else "off"
    normalized = str(value).strip().lower()
    aliases = {
        "true": "on",
        "yes": "on",
        "1": "on",
        "enabled": "on",
        "enable": "on",
        "false": "off",
        "no": "off",
        "0": "off",
        "disabled": "off",
        "disable": "off",
        "inherit": INHERIT,
        "system": INHERIT,
        "auto": INHERIT,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {INHERIT, "on", "off"}:
        raise ValueError(f"Chat setting {field_name} must be default, on, or off")
    return normalized


def _coerce_setting(field_name: str, value: Any) -> Any:
    if field_name in TRISTATE_FIELDS:
        return _coerce_tristate(value, field_name=field_name)

    if field_name == "response_mode":
        normalized = _clean_text(value).lower() or INHERIT
        if normalized in {"inherit", "system", "auto"}:
            normalized = INHERIT
        if normalized not in {INHERIT, "all", "mentions"}:
            raise ValueError("Chat setting response_mode must be default, all, or mentions")
        return normalized

    if field_name == "reply_to_mode":
        normalized = _clean_text(value).lower() or INHERIT
        if normalized in {"inherit", "system", "auto"}:
            normalized = INHERIT
        if normalized not in {INHERIT, "off", "first", "all"}:
            raise ValueError("Chat setting reply_to_mode must be default, off, first, or all")
        return normalized

    if field_name == "tool_progress":
        if value is None or value == "":
            return INHERIT
        if value is False:
            return "off"
        if value is True:
            return "all"
        normalized = str(value).strip().lower()
        if normalized in {"inherit", "system", "auto"}:
            normalized = INHERIT
        if normalized not in {INHERIT, "off", "new", "all", "verbose"}:
            raise ValueError("Chat setting tool_progress must be default, off, new, all, or verbose")
        return normalized

    if field_name == "tool_preview_length":
        if value is None or value == "":
            return INHERIT
        if isinstance(value, str) and value.strip().lower() in {"default", "inherit", "system", "auto"}:
            return INHERIT
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError("Chat setting tool_preview_length must be default or an integer")
        if parsed < 0:
            raise ValueError("Chat setting tool_preview_length must not be negative")
        return parsed

    return value


def _normalize_values(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("Chat settings values must be a mapping")
    values = dict(DEFAULT_SETTINGS)
    for field_name in SETTING_FIELDS:
        if field_name in raw:
            values[field_name] = _coerce_setting(field_name, raw.get(field_name))
    return values


def _normalize_setting(raw: dict[str, Any]) -> ChatSetting:
    platform = _clean_text(raw.get("platform")).lower()
    chat_id = _clean_text(raw.get("chat_id"))
    if not platform:
        raise ValueError("Chat setting platform is required")
    if not chat_id:
        raise ValueError("Chat setting chat_id is required")
    return ChatSetting(
        platform=platform,
        chat_id=chat_id,
        label=_clean_text(raw.get("label")),
        values=_normalize_values(raw),
    )


def normalize_chat_settings_config(raw: Any) -> ChatSettingsConfig:
    if raw is None:
        return ChatSettingsConfig()
    if isinstance(raw, list):
        raw = {"settings": raw}
    if not isinstance(raw, dict):
        raise ValueError("chat_settings must be a mapping")

    defaults = _normalize_values(raw.get("defaults") or {})
    raw_items = raw.get("settings", raw.get("items", []))
    if not isinstance(raw_items, list):
        raise ValueError("chat_settings.settings must be a list")

    settings = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError("Each chat setting must be a mapping")
        settings.append(_normalize_setting(item))
    return ChatSettingsConfig(defaults=defaults, settings=settings)


def chat_settings_to_dict(config: ChatSettingsConfig) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for setting in config.settings:
        item: dict[str, Any] = {
            "id": setting.id,
            "platform": setting.platform,
            "chat_id": setting.chat_id,
        }
        if setting.label:
            item["label"] = setting.label
        item.update({name: setting.values.get(name, INHERIT) for name in SETTING_FIELDS})
        items.append(item)
    return {
        "defaults": {name: config.defaults.get(name, INHERIT) for name in SETTING_FIELDS},
        "settings": items,
    }


def _source_platform(source: SessionSource) -> str:
    platform = getattr(source, "platform", "")
    return _clean_text(getattr(platform, "value", platform)).lower()


def resolve_chat_settings(config: ChatSettingsConfig, *, platform: str, chat_id: str) -> dict[str, Any]:
    effective = dict(config.defaults)
    wanted_key = chat_setting_key(platform, chat_id)
    for setting in config.settings:
        if setting.id != wanted_key:
            continue
        for field_name, value in setting.values.items():
            if value != INHERIT:
                effective[field_name] = value
    return effective


def resolve_chat_settings_for_source(raw: Any, source: SessionSource) -> dict[str, Any]:
    config = normalize_chat_settings_config(raw)
    return resolve_chat_settings(
        config,
        platform=_source_platform(source),
        chat_id=_clean_text(getattr(source, "chat_id", "")),
    )


def tri_enabled(value: Any) -> bool | None:
    normalized = _coerce_tristate(value, field_name="value")
    if normalized == "on":
        return True
    if normalized == "off":
        return False
    return None


def apply_chat_settings_to_config(
    user_config: dict[str, Any],
    source: SessionSource,
    *,
    chat_settings_raw: Any = None,
) -> dict[str, Any]:
    """Overlay per-chat display settings onto a profile config copy.

    The dashboard stores chat settings in the gateway/default config because
    routes are selected before a profile runs.  Agent execution, however, uses
    the resolved profile config.  This helper applies the per-chat overlay to a
    copy of that profile config so existing display_config resolution keeps
    working without teaching every caller about chat_settings.
    """
    base = copy.deepcopy(user_config) if isinstance(user_config, dict) else {}
    raw = chat_settings_raw if chat_settings_raw is not None else base.get("chat_settings")
    effective = resolve_chat_settings_for_source(raw, source)
    platform = _source_platform(source)
    if not platform:
        return base

    display_updates: dict[str, Any] = {}
    for field_name in (
        "tool_progress",
        "show_reasoning",
        "tool_preview_length",
        "interim_assistant_messages",
        "long_running_notifications",
        "busy_ack_detail",
        "cleanup_progress",
        "streaming",
    ):
        value = effective.get(field_name, INHERIT)
        if value == INHERIT:
            continue
        if field_name in TRISTATE_FIELDS:
            parsed = tri_enabled(value)
            if parsed is not None:
                display_updates[field_name] = parsed
        else:
            display_updates[field_name] = value

    if display_updates:
        display = base.setdefault("display", {})
        if not isinstance(display, dict):
            display = {}
            base["display"] = display
        platforms = display.setdefault("platforms", {})
        if not isinstance(platforms, dict):
            platforms = {}
            display["platforms"] = platforms
        platform_settings = platforms.setdefault(platform, {})
        if not isinstance(platform_settings, dict):
            platform_settings = {}
            platforms[platform] = platform_settings
        platform_settings.update(display_updates)

    return base
