"""TelegramAdapter send-path health gating after reconnect storms.

After sustained Bad Gateway / TimedOut reconnect cycles, the PTB httpx client
can enter a wedged state where ``bot.send_message()`` returns a valid Message
but nothing reaches the recipient.  ``_send_path_degraded`` short-circuits
``send()`` so cron's live-adapter branch falls through to standalone HTTP.
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.platforms.telegram import TelegramAdapter  # noqa: E402


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    return adapter


@pytest.mark.asyncio
async def test_send_succeeds_when_path_healthy():
    """Healthy adapter delivers normally; send_message is called."""
    adapter = _make_adapter()
    assert adapter._send_path_degraded is False

    result = await adapter.send("123", "hello")

    assert result.success is True
    adapter._bot.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_send_short_circuits_when_path_degraded():
    """Degraded adapter returns failure WITHOUT calling send_message,
    so cron's live-adapter branch falls through to standalone HTTP."""
    adapter = _make_adapter()
    adapter._send_path_degraded = True

    result = await adapter.send("123", "hello")

    assert result.success is False
    assert result.error == "send_path_degraded"
    assert result.retryable is True
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["[[silent]]", "*(silent)*"])
async def test_send_consumes_silence_controls_fail_closed(content):
    """Direct/streaming bypasses can never publish a control as text."""
    adapter = _make_adapter()

    result = await adapter.send("123", content)

    assert result.success is True
    assert result.message_id is None
    assert result.raw_response["control_response"] == "silent"
    assert result.raw_response["delivered"] is False
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_converts_reaction_control_to_native_reaction():
    adapter = _make_adapter()
    adapter._bot.set_message_reaction = AsyncMock(return_value=True)

    result = await adapter.send("123", "[[reaction:👍]]", reply_to="456")

    assert result.success is True
    assert result.message_id is None
    assert result.raw_response["control_response"] == "reaction"
    assert result.raw_response["reaction_sent"] is True
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="👍",
    )
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_strips_inline_reaction_control_before_text_delivery():
    adapter = _make_adapter()
    adapter._bot.set_message_reaction = AsyncMock(return_value=True)

    result = await adapter.send(
        "123",
        "[[reaction:👍]]\nГотово",
        reply_to="456",
    )

    assert result.success is True
    adapter._bot.set_message_reaction.assert_awaited_once()
    adapter._bot.send_message.assert_awaited_once()
    sent_text = adapter._bot.send_message.await_args.kwargs["text"]
    assert "reaction" not in sent_text
    assert "Готово" in sent_text


@pytest.mark.asyncio
async def test_degraded_send_drops_control_bearing_text_before_fallback():
    """A caller must not retry the original raw marker through HTTP fallback."""
    adapter = _make_adapter()
    adapter._send_path_degraded = True

    result = await adapter.send("123", "[[reaction:👍]]\nГотово", reply_to="456")

    assert result.success is True
    assert result.raw_response["control_response"] == "reaction"
    assert result.raw_response["delivered"] is False
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_preserves_control_marker_mentioned_in_prose():
    adapter = _make_adapter()
    text = "Use `[[silent]]` in the protocol documentation."

    result = await adapter.send("123", text)

    assert result.success is True
    adapter._bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconnect_storm_sets_and_heartbeat_clears_flag(monkeypatch):
    """_handle_polling_network_error sets the flag; a successful heartbeat
    probe in _verify_polling_after_reconnect clears it."""
    adapter = _make_adapter()
    adapter._app = MagicMock()
    adapter._app.updater = MagicMock()
    adapter._app.updater.running = True
    adapter._app.updater.stop = AsyncMock()
    adapter._app.updater.start_polling = AsyncMock()
    adapter._app.bot = MagicMock()
    adapter._app.bot.get_me = AsyncMock(return_value=MagicMock())
    adapter._polling_error_callback_ref = AsyncMock()
    monkeypatch.setattr(
        "gateway.platforms.telegram.Update", MagicMock(ALL_TYPES=[])
    )

    await adapter._handle_polling_network_error(OSError("Bad Gateway"))
    assert adapter._send_path_degraded is True

    with patch("gateway.platforms.telegram.asyncio.sleep", new_callable=AsyncMock):
        await adapter._verify_polling_after_reconnect()
    assert adapter._send_path_degraded is False
