"""Tests for Telegram message reactions tied to processing lifecycle hooks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    AgentControlResponse,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
)
from gateway.session import SessionSource


def _make_adapter(**extra_env):
    from gateway.platforms.telegram import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="fake-token")
    adapter._bot = AsyncMock()
    adapter._bot.set_message_reaction = AsyncMock()
    adapter._processing_reaction_visible = set()
    adapter._background_tasks = set()
    return adapter


def _make_event(chat_id: str = "123", message_id: str = "456") -> MessageEvent:
    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            chat_type="private",
            user_id="42",
            user_name="TestUser",
        ),
        message_id=message_id,
    )


# ── _reactions_enabled ───────────────────────────────────────────────


def test_reactions_disabled_by_default(monkeypatch):
    """Telegram reactions should be disabled by default."""
    monkeypatch.delenv("TELEGRAM_REACTIONS", raising=False)
    adapter = _make_adapter()
    assert adapter._reactions_enabled() is False


def test_reactions_enabled_when_set_true(monkeypatch):
    """Setting TELEGRAM_REACTIONS=true enables reactions."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    assert adapter._reactions_enabled() is True


def test_reactions_enabled_with_1(monkeypatch):
    """TELEGRAM_REACTIONS=1 enables reactions."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "1")
    adapter = _make_adapter()
    assert adapter._reactions_enabled() is True


def test_reactions_disabled_with_false(monkeypatch):
    """TELEGRAM_REACTIONS=false disables reactions."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "false")
    adapter = _make_adapter()
    assert adapter._reactions_enabled() is False


def test_reactions_disabled_with_0(monkeypatch):
    """TELEGRAM_REACTIONS=0 disables reactions."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "0")
    adapter = _make_adapter()
    assert adapter._reactions_enabled() is False


def test_reactions_disabled_with_no(monkeypatch):
    """TELEGRAM_REACTIONS=no disables reactions."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "no")
    adapter = _make_adapter()
    assert adapter._reactions_enabled() is False


# ── lifecycle reaction settings ──────────────────────────────────────


def test_reaction_setting_reads_env(monkeypatch):
    from gateway.platforms.telegram import TelegramAdapter

    monkeypatch.setenv("TELEGRAM_REACTION_SUCCESS", "\U0001f389")

    assert TelegramAdapter._reaction_setting("TELEGRAM_REACTION_SUCCESS", "\u2705") == "\U0001f389"


@pytest.mark.asyncio
async def test_apply_reaction_action_clear(monkeypatch):
    adapter = _make_adapter()
    adapter._clear_reactions = AsyncMock(return_value=True)
    adapter._set_reaction = AsyncMock(return_value=True)

    result = await adapter._apply_reaction_action("123", "456", "clear")

    assert result is True
    adapter._clear_reactions.assert_awaited_once_with("123", "456")
    adapter._set_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_reaction_action_clears_when_terminal_reaction_fails(monkeypatch):
    adapter = _make_adapter()
    adapter._clear_reactions = AsyncMock(return_value=True)
    adapter._set_reaction = AsyncMock(return_value=False)

    result = await adapter._apply_reaction_action(
        "123",
        "456",
        "\u2705",
        clear_on_failure=True,
    )

    assert result is True
    adapter._set_reaction.assert_awaited_once_with("123", "456", "\u2705")
    adapter._clear_reactions.assert_awaited_once_with("123", "456")


@pytest.mark.asyncio
async def test_apply_reaction_action_keep(monkeypatch):
    adapter = _make_adapter()
    adapter._clear_reactions = AsyncMock(return_value=True)
    adapter._set_reaction = AsyncMock(return_value=True)

    result = await adapter._apply_reaction_action("123", "456", "keep")

    assert result is True
    adapter._clear_reactions.assert_not_awaited()
    adapter._set_reaction.assert_not_awaited()


# ── _set_reaction ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_reaction_calls_bot_api(monkeypatch):
    """_set_reaction should call bot.set_message_reaction with correct args."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()

    result = await adapter._set_reaction("123", "456", "\U0001f440")

    assert result is True
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="\U0001f440",
    )


@pytest.mark.asyncio
async def test_set_reaction_returns_false_without_bot(monkeypatch):
    """_set_reaction should return False when bot is not available."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    adapter._bot = None

    result = await adapter._set_reaction("123", "456", "\U0001f440")
    assert result is False


@pytest.mark.asyncio
async def test_set_reaction_handles_api_error_gracefully(monkeypatch):
    """API errors during reaction should not propagate."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    adapter._bot.set_message_reaction = AsyncMock(side_effect=RuntimeError("no perms"))

    result = await adapter._set_reaction("123", "456", "\U0001f440")
    assert result is False


# ── on_processing_start ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_processing_start_does_not_react(monkeypatch):
    """Plain message processing should not get eyes automatically."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_start(event)

    adapter._bot.set_message_reaction.assert_not_awaited()
    assert adapter._processing_reaction_visible == set()


@pytest.mark.asyncio
async def test_mark_processing_work_started_adds_eyes(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.mark_processing_work_started(event.source, event.message_id)

    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="\U0001f440",
    )
    assert ("123", "456") in adapter._processing_reaction_visible


@pytest.mark.asyncio
async def test_mark_processing_work_started_is_idempotent(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.mark_processing_work_started(event.source, event.message_id)
    await adapter.mark_processing_work_started(event.source, event.message_id)

    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="\U0001f440",
    )
    assert ("123", "456") in adapter._processing_reaction_visible


@pytest.mark.asyncio
async def test_on_processing_start_uses_configured_reaction(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.setenv("TELEGRAM_REACTION_IN_PROGRESS", "\U0001f50e")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.mark_processing_work_started(event.source, event.message_id)

    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="\U0001f50e",
    )


@pytest.mark.asyncio
async def test_on_processing_start_skipped_when_disabled(monkeypatch):
    """Processing start should not react when reactions are disabled."""
    monkeypatch.delenv("TELEGRAM_REACTIONS", raising=False)
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_start(event)

    adapter._bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_processing_start_handles_missing_ids(monkeypatch):
    """Should handle events without chat_id or message_id gracefully."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SimpleNamespace(chat_id=None),
        message_id=None,
    )

    await adapter.on_processing_start(event)

    adapter._bot.set_message_reaction.assert_not_awaited()


# ── on_processing_complete ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_processing_complete_success(monkeypatch):
    """Successful processing without visible status does nothing."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.delenv("TELEGRAM_REACTION_SUCCESS", raising=False)
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter._bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_processing_complete_failure(monkeypatch):
    """Failed processing swaps visible status to the default thumbs-down."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.delenv("TELEGRAM_REACTION_FAILURE", raising=False)
    adapter = _make_adapter()
    event = _make_event()

    await adapter.mark_processing_work_started(event.source, event.message_id)
    adapter._bot.set_message_reaction.reset_mock()

    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="\U0001f44e",
    )


@pytest.mark.asyncio
async def test_on_processing_complete_success_can_clear(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.setenv("TELEGRAM_REACTION_SUCCESS", "clear")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.mark_processing_work_started(event.source, event.message_id)
    adapter._bot.set_message_reaction.reset_mock()

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction=[],
    )


@pytest.mark.asyncio
async def test_on_processing_complete_failure_uses_configured_reaction(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.setenv("TELEGRAM_REACTION_FAILURE", "\U0001f6ab")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.mark_processing_work_started(event.source, event.message_id)
    adapter._bot.set_message_reaction.reset_mock()

    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="\U0001f6ab",
    )


@pytest.mark.asyncio
async def test_on_processing_complete_clears_when_configured_reaction_fails(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.setenv("TELEGRAM_REACTION_SUCCESS", "\u2705")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.mark_processing_work_started(event.source, event.message_id)
    adapter._bot.set_message_reaction = AsyncMock(
        side_effect=[RuntimeError("reaction is not allowed"), None]
    )

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert adapter._bot.set_message_reaction.await_args_list[0].kwargs == {
        "chat_id": 123,
        "message_id": 456,
        "reaction": "\u2705",
    }
    assert adapter._bot.set_message_reaction.await_args_list[1].kwargs == {
        "chat_id": 123,
        "message_id": 456,
        "reaction": [],
    }


@pytest.mark.asyncio
async def test_on_processing_complete_skipped_when_disabled(monkeypatch):
    """Processing complete should not react when reactions are disabled."""
    monkeypatch.delenv("TELEGRAM_REACTIONS", raising=False)
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter._bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_processing_complete_cancelled_clears_reaction(monkeypatch):
    """Cancelled processing should clear the in-progress reaction.

    Without this clear, the 👀 reaction lingers on the user's message
    indefinitely (until another agent run swaps it for a terminal reaction). On a
    ``/stop`` that ends a session, that reaction never gets cleaned up.
    """
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.mark_processing_work_started(event.source, event.message_id)
    adapter._bot.set_message_reaction.reset_mock()

    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)

    # An explicit empty reaction list clears all bot-set reactions without
    # triggering PTB's invalid Reaction_empty serialization path.
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction=[],
    )


@pytest.mark.asyncio
async def test_on_processing_complete_cancelled_skipped_when_disabled(monkeypatch):
    """Cancelled processing should not call the API when reactions are off."""
    monkeypatch.delenv("TELEGRAM_REACTIONS", raising=False)
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)

    adapter._bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_reactions_handles_api_error_gracefully(monkeypatch):
    """API errors during clear should not propagate."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    adapter._bot.set_message_reaction = AsyncMock(side_effect=RuntimeError("no perms"))

    result = await adapter._clear_reactions("123", "456")
    assert result is False


@pytest.mark.asyncio
async def test_clear_reactions_returns_false_without_bot(monkeypatch):
    """_clear_reactions should return False when bot is not available."""
    adapter = _make_adapter()
    adapter._bot = None

    result = await adapter._clear_reactions("123", "456")
    assert result is False


# ── agent control responses ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_control_reaction_sets_reaction_even_when_lifecycle_disabled(monkeypatch):
    """Explicit reaction markers are separate from lifecycle reactions."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "false")
    adapter = _make_adapter()
    event = _make_event()

    result = await adapter.handle_agent_control_response(
        event,
        AgentControlResponse(action="reaction", emoji="\U0001f60d"),
    )

    assert result.success is True
    assert result.message_id is None
    assert result.raw_response["reaction_sent"] is True
    assert getattr(event, "_agent_control_response") == "reaction"
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="\U0001f60d",
    )


@pytest.mark.asyncio
async def test_control_silent_clears_lifecycle_reaction(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = _make_event()

    result = await adapter.handle_agent_control_response(
        event,
        AgentControlResponse(action="silent"),
    )

    assert result.success is True
    assert result.raw_response["reaction_cleared"] is True
    assert getattr(event, "_agent_control_response") == "silent"
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction=[],
    )


@pytest.mark.asyncio
async def test_processing_complete_preserves_control_reaction(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.handle_agent_control_response(
        event,
        AgentControlResponse(action="reaction", emoji="\U0001f44d"),
    )
    adapter._bot.set_message_reaction.reset_mock()

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter._bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_control_response_replaces_visible_processing_reaction(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.mark_processing_work_started(event.source, event.message_id)
    adapter._bot.set_message_reaction.reset_mock()

    await adapter.handle_agent_control_response(
        event,
        AgentControlResponse(action="reaction", emoji="\U0001f44d"),
    )

    assert adapter._processing_reaction_visible == set()
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="\U0001f44d",
    )


# ── config.py bridging ───────────────────────────────────────────────


def test_config_bridges_telegram_reactions(monkeypatch, tmp_path):
    """gateway/config.py bridges telegram.reactions to TELEGRAM_REACTIONS env var."""
    import yaml
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
            "telegram": {
            "reactions": True,
            "reaction_in_progress": "\U0001f440",
            "reaction_success": "clear",
            "reaction_failure": "\U0001f44e",
            "reaction_cancelled": "clear",
        },
    }, allow_unicode=True))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Use setenv (not delenv) so monkeypatch registers cleanup even when
    # the var doesn't exist yet — load_gateway_config will overwrite it.
    monkeypatch.setenv("TELEGRAM_REACTIONS", "")
    monkeypatch.setenv("TELEGRAM_REACTION_IN_PROGRESS", "")
    monkeypatch.setenv("TELEGRAM_REACTION_SUCCESS", "")
    monkeypatch.setenv("TELEGRAM_REACTION_FAILURE", "")
    monkeypatch.setenv("TELEGRAM_REACTION_CANCELLED", "")

    from gateway.config import load_gateway_config
    load_gateway_config()

    import os
    assert os.getenv("TELEGRAM_REACTIONS") == "true"
    assert os.getenv("TELEGRAM_REACTION_IN_PROGRESS") == "\U0001f440"
    assert os.getenv("TELEGRAM_REACTION_SUCCESS") == "clear"
    assert os.getenv("TELEGRAM_REACTION_FAILURE") == "\U0001f44e"
    assert os.getenv("TELEGRAM_REACTION_CANCELLED") == "clear"


def test_config_reactions_env_takes_precedence(monkeypatch, tmp_path):
    """Env var should take precedence over config.yaml for reactions."""
    import yaml
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "telegram": {
            "reactions": True,
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_REACTIONS", "false")

    from gateway.config import load_gateway_config
    load_gateway_config()

    import os
    assert os.getenv("TELEGRAM_REACTIONS") == "false"
