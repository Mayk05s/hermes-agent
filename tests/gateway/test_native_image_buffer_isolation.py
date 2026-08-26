from typing import Any

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


@pytest.mark.asyncio
async def test_telegram_photo_reaches_durable_job_model_request(
    monkeypatch, tmp_path
):
    from tests.gateway.test_run_cleanup_progress import (
        CleanupCaptureAdapter,
        _install_fakes,
        _make_runner as make_agent_runner,
    )

    class CapturingAgent:
        received_message: Any = None

        def __init__(self, **kwargs):
            self.tools = []

        def run_conversation(
            self, message, conversation_history=None, task_id=None, **kwargs
        ):
            type(self).received_message = message
            return {"final_response": "done", "messages": [], "api_calls": 1}

    image_path = tmp_path / "telegram-photo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nphoto-bytes")
    source = _source("chat-a")
    base_key = build_session_key(source)
    job_key = f"{base_key}:job:gw_image"
    caption = "Сохрани этот чек"
    event = _image_event(source, str(image_path))
    event.text = caption
    event.job_execution_key = job_key
    runner = make_agent_runner(CleanupCaptureAdapter())
    runner._decide_image_input_mode = lambda: "native"
    _install_fakes(monkeypatch, CapturingAgent, cleanup_on=False)

    prepared = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
        session_key=event.job_execution_key,
    )
    result = await runner._run_agent(
        message=prepared,
        context_prompt="",
        history=[],
        source=source,
        session_id="durable-image-session",
        session_key=job_key,
    )

    assert result["final_response"] == "done"
    assert runner._consume_pending_native_image_paths(base_key) == []
    assert isinstance(CapturingAgent.received_message, list)
    text_parts = [
        part.get("text", "")
        for part in CapturingAgent.received_message
        if part.get("type") == "text"
    ]
    image_parts = [
        part
        for part in CapturingAgent.received_message
        if part.get("type") == "image_url"
    ]
    assert any(caption in text for text in text_parts)
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


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
