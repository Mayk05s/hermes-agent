from gateway.chat_settings import (
    apply_chat_settings_to_config,
    normalize_chat_settings_config,
    resolve_chat_settings,
)
from gateway.config import Platform
from gateway.session import SessionSource


def test_chat_settings_defaults_and_chat_override_resolve():
    cfg = normalize_chat_settings_config(
        {
            "defaults": {
                "response_mode": "mentions",
                "tool_progress": "off",
                "participant_isolation": "off",
            },
            "settings": [
                {
                    "platform": "telegram",
                    "chat_id": "-1001",
                    "response_mode": "all",
                    "cleanup_progress": "on",
                    "participant_isolation": "on",
                    "observe_unmentioned": "off",
                }
            ],
        }
    )

    effective = resolve_chat_settings(cfg, platform="telegram", chat_id="-1001")

    assert effective["response_mode"] == "all"
    assert effective["tool_progress"] == "off"
    assert effective["cleanup_progress"] == "on"
    assert effective["participant_isolation"] == "on"
    assert effective["observe_unmentioned"] == "off"


def test_chat_settings_overlay_maps_to_display_platform_config():
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")

    config = apply_chat_settings_to_config(
        {"display": {"tool_progress": "all"}},
        source,
        chat_settings_raw={
            "settings": [
                {
                    "platform": "telegram",
                    "chat_id": "-1001",
                    "tool_progress": "off",
                    "show_reasoning": "on",
                    "interim_assistant_messages": "off",
                    "tool_preview_length": 12,
                    "streaming": "on",
                }
            ],
        },
    )

    telegram_display = config["display"]["platforms"]["telegram"]
    assert telegram_display["tool_progress"] == "off"
    assert telegram_display["show_reasoning"] is True
    assert telegram_display["interim_assistant_messages"] is False
    assert telegram_display["tool_preview_length"] == 12
    assert telegram_display["streaming"] is True


def test_pairing_chat_settings_replace_target_override(monkeypatch):
    from gateway.pairing_routes import upsert_pairing_chat_settings

    current = {
        "chat_settings": {
            "defaults": {"response_mode": "mentions"},
            "settings": [
                {
                    "platform": "telegram",
                    "chat_id": "-1001",
                    "response_mode": "all",
                },
                {
                    "platform": "telegram",
                    "chat_id": "-2002",
                    "audio_trigger": "on",
                },
            ],
        }
    }
    saved = {}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: current)
    monkeypatch.setattr("hermes_cli.config.save_config", lambda value: saved.update(value))

    result = upsert_pairing_chat_settings(
        {
            "platform": "telegram",
            "subject_type": "chat",
            "chat_id": "-1001",
            "chat_name": "Research Group",
        },
        {
            "response_mode": "mentions",
            "audio_trigger": "off",
            "show_transcription": "on",
        },
    )

    assert result["response_mode"] == "mentions"
    normalized = normalize_chat_settings_config(saved["chat_settings"])
    target = resolve_chat_settings(normalized, platform="telegram", chat_id="-1001")
    untouched = resolve_chat_settings(normalized, platform="telegram", chat_id="-2002")
    assert target["response_mode"] == "mentions"
    assert target["audio_trigger"] == "off"
    assert target["show_transcription"] == "on"
    assert untouched["audio_trigger"] == "on"
