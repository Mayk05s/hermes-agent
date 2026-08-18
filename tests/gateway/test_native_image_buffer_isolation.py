import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import (
    GatewayRunner,
    _persisted_user_message_with_image_refs,
    _wrap_current_message_with_attachment_guard,
)
from gateway.session import SessionSource, build_session_key


def _make_runner() -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    runner._decide_image_input_mode = lambda: "native"
    return runner


def _source(chat_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="private",
        user_name=f"user-{chat_id}",
    )


def _image_event(source: SessionSource, path: str) -> MessageEvent:
    return MessageEvent(
        text="see image",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=[path],
        media_types=["image/png"],
    )


@pytest.mark.asyncio
async def test_native_image_buffer_isolated_per_session():
    runner = _make_runner()
    source_a = _source("chat-a")
    source_b = _source("chat-b")

    await runner._prepare_inbound_message_text(
        event=_image_event(source_a, "/tmp/a.png"),
        source=source_a,
        history=[],
    )
    await runner._prepare_inbound_message_text(
        event=_image_event(source_b, "/tmp/b.png"),
        source=source_b,
        history=[],
    )

    assert runner._consume_pending_native_image_paths(build_session_key(source_a)) == ["/tmp/a.png"]
    assert runner._consume_pending_native_image_paths(build_session_key(source_b)) == ["/tmp/b.png"]


@pytest.mark.asyncio
async def test_native_image_buffer_not_cleared_by_other_sessions_without_images():
    runner = _make_runner()
    source_a = _source("chat-a")
    source_b = _source("chat-b")

    await runner._prepare_inbound_message_text(
        event=_image_event(source_a, "/tmp/a.png"),
        source=source_a,
        history=[],
    )
    await runner._prepare_inbound_message_text(
        event=MessageEvent(text="plain text", source=source_b),
        source=source_b,
        history=[],
    )

    assert runner._consume_pending_native_image_paths(build_session_key(source_a)) == ["/tmp/a.png"]
    assert runner._consume_pending_native_image_paths(build_session_key(source_b)) == []


def test_persisted_image_message_keeps_path_without_image_bytes():
    persisted = _persisted_user_message_with_image_refs(
        "@TripiooBot сохрани",
        ["/tmp/booking.jpg"],
    )

    assert persisted == (
        "@TripiooBot сохрани\n\n[Image attached at: /tmp/booking.jpg]"
    )
    assert "base64" not in persisted


def test_attachment_guard_precedes_current_caption_and_preserves_image_part():
    image_part = {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,abc"},
    }
    wrapped = _wrap_current_message_with_attachment_guard(
        [{"type": "text", "text": "сохрани"}, image_part]
    )

    assert wrapped[0]["text"].startswith("[Current message attachment")
    assert wrapped[0]["text"].endswith("сохрани")
    assert wrapped[1] == image_part
