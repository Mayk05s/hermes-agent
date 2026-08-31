"""Tests for Telegram text message aggregation.

When a user sends a long message, Telegram clients split it into multiple
updates.  The TelegramAdapter should buffer rapid successive text messages
from the same session and aggregate them before dispatching.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SessionSource


def _make_adapter():
    """Create a minimal TelegramAdapter for testing text batching."""
    from gateway.platforms.telegram import TelegramAdapter

    config = PlatformConfig(enabled=True, token="test-token")
    adapter = object.__new__(TelegramAdapter)
    adapter._platform = Platform.TELEGRAM
    adapter.config = config
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 0.1  # fast for tests
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._message_handler = AsyncMock()
    adapter.handle_message = AsyncMock()
    return adapter


def _make_event(text: str, chat_id: str = "12345") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm"),
    )


def _make_group_event(text: str, *, user_id: str, thread_id: str = "615") -> MessageEvent:
    raw = MagicMock()
    raw.from_user.id = int(user_id)
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1003966683704",
            chat_type="group",
            thread_id=thread_id,
        ),
        raw_message=raw,
    )


class TestTextBatching:
    @pytest.mark.asyncio
    async def test_single_message_dispatched_after_delay(self):
        adapter = _make_adapter()
        event = _make_event("hello world")

        adapter._enqueue_text_event(event)

        # Not dispatched yet
        adapter.handle_message.assert_not_called()

        # Wait for flush
        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert dispatched.text == "hello world"

    @pytest.mark.asyncio
    async def test_split_messages_aggregated(self):
        """Two rapid messages from the same chat should be merged."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("This is part one of a long"))
        await asyncio.sleep(0.02)  # small gap, within batch window
        adapter._enqueue_text_event(_make_event("message that was split by Telegram."))

        # Not dispatched yet (timer restarted)
        adapter.handle_message.assert_not_called()

        # Wait for flush
        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert "part one" in dispatched.text
        assert "split by Telegram" in dispatched.text

    @pytest.mark.asyncio
    async def test_three_way_split_aggregated(self):
        """Three rapid messages should all merge."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("chunk 1"))
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(_make_event("chunk 2"))
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(_make_event("chunk 3"))

        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        text = adapter.handle_message.call_args[0][0].text
        assert "chunk 1" in text
        assert "chunk 2" in text
        assert "chunk 3" in text

    @pytest.mark.asyncio
    async def test_different_chats_not_merged(self):
        """Messages from different chats should be separate batches."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("from user A", chat_id="111"))
        adapter._enqueue_text_event(_make_event("from user B", chat_id="222"))

        await asyncio.sleep(0.2)

        assert adapter.handle_message.call_count == 2

    @pytest.mark.asyncio
    async def test_batch_cleans_up_after_flush(self):
        """After flushing, internal state should be clean."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("test"))
        await asyncio.sleep(0.2)

        assert len(adapter._pending_text_batches) == 0
        assert len(adapter._pending_text_batch_tasks) == 0

    @pytest.mark.asyncio
    async def test_instruction_then_forward_is_one_same_sender_topic_request(self):
        adapter = _make_adapter()
        instruction = _make_group_event("@TripiooBot сохрани это", user_id="179555559")
        instruction._await_forward_followup = True
        adapter._enqueue_text_event(instruction)

        forwarded = _make_group_event("Пересланный текст", user_id="179555559")
        assert adapter._merge_pending_forwarded_text(forwarded) is True

        await asyncio.sleep(0.2)
        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert dispatched.text == "@TripiooBot сохрани это\nПересланный текст"

    def test_forward_followup_never_crosses_sender_or_topic(self):
        adapter = _make_adapter()
        instruction = _make_group_event("@TripiooBot проверь", user_id="179555559")
        instruction._await_forward_followup = True
        adapter._pending_text_batches[adapter._text_batch_key(instruction)] = instruction

        other_user = _make_group_event("чужой forward", user_id="367599252")
        other_topic = _make_group_event(
            "forward из другой темы", user_id="179555559", thread_id="796"
        )
        assert adapter._merge_pending_forwarded_text(other_user) is False
        assert adapter._merge_pending_forwarded_text(other_topic) is False
        assert instruction.text == "@TripiooBot проверь"

    def test_forward_detection_supports_modern_and_legacy_ptb_fields(self):
        adapter = _make_adapter()
        modern = MagicMock(spec=["forward_origin", "forward_date"])
        modern.forward_origin = object()
        modern.forward_date = None
        assert adapter._is_forwarded_message(modern) is True

        legacy = MagicMock(spec=["forward_origin", "forward_date"])
        legacy.forward_origin = None
        legacy.forward_date = object()
        assert adapter._is_forwarded_message(legacy) is True

        plain = MagicMock(spec=["forward_origin", "forward_date"])
        plain.forward_origin = None
        plain.forward_date = None
        assert adapter._is_forwarded_message(plain) is False
