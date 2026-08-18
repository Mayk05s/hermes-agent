"""Tests for Telegram text message aggregation.

When a user sends a long message, Telegram clients split it into multiple
updates.  The TelegramAdapter should buffer rapid successive text messages
from the same session and aggregate them before dispatching.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SessionSource
from gateway.session import build_session_key


def _make_adapter():
    """Create a minimal TelegramAdapter for testing text batching."""
    from gateway.platforms.telegram import TelegramAdapter

    config = PlatformConfig(enabled=True, token="test-token")
    adapter = object.__new__(TelegramAdapter)
    adapter._platform = Platform.TELEGRAM
    adapter.config = config
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._held_forward_events = {}
    adapter._held_forward_tasks = {}
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


def _make_group_event(text: str, *, user_id: str, thread_id: str = "2") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1003966683704",
            chat_type="group",
            thread_id=thread_id,
        ),
        raw_message=SimpleNamespace(from_user=SimpleNamespace(id=int(user_id))),
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
        instruction = _make_group_event(
            "@TripiooBot переведи в доллар", user_id="179555559"
        )
        instruction._await_forward_followup = True
        adapter._enqueue_text_event(instruction)

        forwarded = _make_group_event("180 лари за ночь", user_id="179555559")
        assert adapter._merge_pending_forwarded_text(forwarded) is True

        await asyncio.sleep(0.2)
        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert dispatched.text == "@TripiooBot переведи в доллар\n180 лари за ночь"

    @pytest.mark.asyncio
    async def test_reverse_update_order_forward_then_instruction_is_one_request(self):
        adapter = _make_adapter()
        forwarded = _make_group_event("180 лари за ночь", user_id="179555559")
        adapter._hold_forward_for_instruction(forwarded)

        instruction = _make_group_event(
            "@TripiooBot переведи в доллар", user_id="179555559"
        )
        instruction._await_forward_followup = True
        adapter._enqueue_text_event(instruction)
        held = adapter._pop_held_forward(instruction)
        assert held is forwarded
        assert adapter._merge_pending_forwarded_text(held) is True

        await asyncio.sleep(0.2)
        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert dispatched.text == "@TripiooBot переведи в доллар\n180 лари за ночь"

    @pytest.mark.asyncio
    async def test_reverse_order_forward_never_crosses_sender_or_topic(self):
        adapter = _make_adapter()
        forwarded = _make_group_event("чужой forward", user_id="367599252")
        adapter._hold_forward_for_instruction(forwarded)

        other_sender = _make_group_event("@TripiooBot проверь", user_id="179555559")
        other_topic = _make_group_event(
            "@TripiooBot проверь", user_id="367599252", thread_id="796"
        )
        assert adapter._pop_held_forward(other_sender) is None
        assert adapter._pop_held_forward(other_topic) is None
        assert len(adapter._held_forward_events) == 1

        tasks = list(adapter._held_forward_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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

    def test_forward_detection_supports_modern_and_legacy_fields(self):
        from gateway.platforms.telegram import TelegramAdapter

        modern = SimpleNamespace(forward_origin=object(), forward_date=None)
        legacy = SimpleNamespace(forward_origin=None, forward_date=object())
        plain = SimpleNamespace(forward_origin=None, forward_date=None)

        assert TelegramAdapter._is_forwarded_message(modern) is True
        assert TelegramAdapter._is_forwarded_message(legacy) is True
        assert TelegramAdapter._is_forwarded_message(plain) is False

    @pytest.mark.asyncio
    async def test_dm_topic_batching_recovers_thread_before_keying(self):
        """DM-topic text batches should use the recovered topic lane."""
        adapter = _make_adapter()
        adapter.set_topic_recovery_fn(
            lambda source: "222" if str(source.thread_id or "") == "1" else None
        )
        event = MessageEvent(
            text="hello from DM topic",
            message_type=MessageType.TEXT,
            source=SessionSource(
                platform=Platform.TELEGRAM,
                chat_id="12345",
                chat_type="dm",
                user_id="user-1",
                thread_id="1",
            ),
        )

        adapter._enqueue_text_event(event)

        def _key(thread_id: str) -> str:
            return build_session_key(
                SimpleNamespace(
                    platform=Platform.TELEGRAM,
                    chat_id="12345",
                    chat_type="dm",
                    thread_id=thread_id,
                ),
                group_sessions_per_user=True,
                thread_sessions_per_user=False,
            )

        assert _key("222") in adapter._pending_text_batches
        assert _key("1") not in adapter._pending_text_batches
        assert event.source.thread_id == "222"

        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert dispatched.source.thread_id == "222"
