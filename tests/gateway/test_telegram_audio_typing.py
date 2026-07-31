"""Regression tests for Telegram audio typing-indicator timing."""

import asyncio

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, build_session_key


class _TelegramAdapter(BasePlatformAdapter):
    def __init__(self) -> None:
        super().__init__(
            PlatformConfig(enabled=True, token="test"),
            Platform.TELEGRAM,
        )
        self.typing_calls: list[str] = []
        self.sent: list[str] = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(
        self,
        chat_id,
        content,
        reply_to=None,
        metadata=None,
    ) -> SendResult:
        self.sent.append(content)
        return SendResult(success=True, message_id="sent-1")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        self.typing_calls.append(chat_id)

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _audio_event(message_type: MessageType = MessageType.VOICE) -> MessageEvent:
    return MessageEvent(
        text="",
        message_type=message_type,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            thread_id="42",
        ),
        message_id="7",
    )


async def _record_typing(adapter, chat_id, interval=2.0, metadata=None):
    adapter.typing_calls.append(chat_id)
    await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", [MessageType.VOICE, MessageType.AUDIO])
async def test_audio_parse_only_never_starts_typing(message_type):
    adapter = _TelegramAdapter()
    event = _audio_event(message_type)

    async def parse_only(_event):
        await asyncio.sleep(0)
        return None

    adapter.set_message_handler(parse_only)
    adapter._keep_typing = lambda *args, **kwargs: _record_typing(
        adapter,
        *args,
        **kwargs,
    )

    await adapter._process_message_background(
        event,
        build_session_key(event.source),
    )

    assert adapter.typing_calls == []
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_voice_starts_typing_only_after_response_is_selected():
    adapter = _TelegramAdapter()
    event = _audio_event()
    before_response: list[str] = []

    async def answer(audio_event):
        await asyncio.sleep(0)
        before_response.extend(adapter.typing_calls)
        audio_event.mark_response_started()
        await asyncio.sleep(0)
        return "answer"

    adapter.set_message_handler(answer)
    adapter._keep_typing = lambda *args, **kwargs: _record_typing(
        adapter,
        *args,
        **kwargs,
    )

    await adapter._process_message_background(
        event,
        build_session_key(event.source),
    )

    assert before_response == []
    assert adapter.typing_calls == ["-1001"]
    assert adapter.sent == ["answer"]
