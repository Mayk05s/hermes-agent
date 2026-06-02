"""Tests for the Telegram /transcribe command."""

from unittest.mock import MagicMock

import pytest
import yaml

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, telegram_menu_commands


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake", extra={})}
    )
    runner.adapters = {}
    runner.hooks = MagicMock()
    return runner


def _event(text: str, *, chat_id="-100123", thread_id="456", platform=Platform.TELEGRAM):
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=platform,
            chat_id=str(chat_id),
            chat_type="group",
            user_id="42",
            user_name="tester",
            thread_id=str(thread_id) if thread_id is not None else None,
        ),
    )


@pytest.mark.asyncio
async def test_transcribe_on_defaults_to_current_topic(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text("telegram: {}\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

    runner = _make_runner()
    result = await runner._handle_transcribe_command(_event("/transcribe on"))

    assert "включена" in result
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    rules = saved["telegram"]["audio_transcription_rules"]
    assert rules == [
        {
            "chat_id": -100123,
            "thread_id": 456,
            "enabled": True,
            "message_types": ["audio"],
            "send_transcript": True,
            "on_no_match": "transcript_only",
        }
    ]
    assert runner.config.platforms[Platform.TELEGRAM].extra["audio_transcription_rules"] == rules


@pytest.mark.asyncio
async def test_transcribe_on_chat_scope_from_topic_omits_thread(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text("telegram: {}\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

    runner = _make_runner()
    result = await runner._handle_transcribe_command(_event("/transcribe on chat"))

    assert "чат" in result
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    rule = saved["telegram"]["audio_transcription_rules"][0]
    assert rule["chat_id"] == -100123
    assert "thread_id" not in rule


@pytest.mark.asyncio
async def test_transcribe_off_preserves_voice_rule_but_removes_audio(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        "telegram:\n"
        "  audio_transcription_rules:\n"
        "    - chat_id: -100123\n"
        "      thread_id: 456\n"
        "      enabled: true\n"
        "      message_types: [voice, audio]\n"
        "      send_transcript: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

    runner = _make_runner()
    result = await runner._handle_transcribe_command(_event("/transcribe off"))

    assert "выключена" in result
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    rule = saved["telegram"]["audio_transcription_rules"][0]
    assert rule["enabled"] is True
    assert rule["message_types"] == ["voice"]


@pytest.mark.asyncio
async def test_transcribe_status_reports_audio_state(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        "telegram:\n"
        "  audio_transcription_rules:\n"
        "    - chat_id: -100123\n"
        "      thread_id: 456\n"
        "      enabled: true\n"
        "      message_types: [audio]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

    runner = _make_runner()
    result = await runner._handle_transcribe_command(_event("/transcribe status"))

    assert "включена" in result
    assert "топик 456" in result


@pytest.mark.asyncio
async def test_transcribe_rejects_non_telegram(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

    runner = _make_runner()
    result = await runner._handle_transcribe_command(
        _event("/transcribe on", platform=Platform.DISCORD)
    )

    assert "только в Telegram" in result


def test_transcribe_is_registered_and_visible_in_telegram_menu():
    assert "transcribe" in GATEWAY_KNOWN_COMMANDS
    names = [name for name, _desc in telegram_menu_commands(max_commands=30)[0]]
    assert "transcribe" in names
