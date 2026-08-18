"""Regression coverage for Telegram TTS directive leakage."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import SendResult
from gateway.platforms.telegram import TelegramAdapter


@pytest.mark.asyncio
async def test_direct_send_routes_directive_only_tts_response_to_native_voice():
    """A raw internal TTS envelope must never become Telegram message text."""
    adapter = object.__new__(TelegramAdapter)
    adapter._bot = MagicMock()
    adapter._send_path_degraded = False
    adapter.send_voice = AsyncMock(
        return_value=SendResult(success=True, message_id="voice-1")
    )

    content = (
        "[[audio_as_voice]]\n"
        "MEDIA:/home/hermes/.hermes/audio_cache/tts_20260804_142955.ogg"
    )
    metadata = {"thread_id": "35"}

    result = await adapter.send(
        chat_id="-1003938895426",
        content=content,
        reply_to="123",
        metadata=metadata,
    )

    assert result.success is True
    assert result.message_id == "voice-1"
    adapter.send_voice.assert_awaited_once_with(
        chat_id="-1003938895426",
        audio_path="/home/hermes/.hermes/audio_cache/tts_20260804_142955.ogg",
        reply_to="123",
        metadata=metadata,
    )
    adapter._bot.send_message.assert_not_called()
