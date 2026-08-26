import asyncio
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.codex_topic_bridge import (
    CodexTopicBridge,
    CodexTopicRunResult,
    DirectCodexTopicRoute,
    direct_codex_route_for_source,
)
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.telegram import TelegramAdapter
from gateway.session import SessionSource


OWNER_ID = "179555559"
CHAT_ID = "-1003735932411"
THREAD_ID = "518"


def _group_topics(*, owners=None, cwd="/home/hermes"):
    return [
        {
            "chat_id": int(CHAT_ID),
            "topics": [
                {
                    "name": "system",
                    "thread_id": int(THREAD_ID),
                    "skill": "telegram_system/telegram_system_context",
                    "direct_codex": {
                        "enabled": True,
                        "owner_user_ids": owners if owners is not None else [OWNER_ID],
                        "cwd": cwd,
                        "title_prefix": "Tripio / system",
                    },
                }
            ],
        }
    ]


def _source(user_id=OWNER_ID):
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=CHAT_ID,
        chat_type="group",
        user_id=str(user_id),
        thread_id=THREAD_ID,
        message_id="42",
    )


def _event(text="проверь код", *, user_id=OWNER_ID):
    return MessageEvent(
        text=text,
        message_type=MessageType.COMMAND if text.startswith("/") else MessageType.TEXT,
        source=_source(user_id),
        message_id="42",
    )


def _adapter(fake_bridge=None):
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(
        enabled=True,
        token="***",
        extra={"group_topics": _group_topics()},
    )
    adapter._codex_topic_bridge = fake_bridge
    adapter._direct_codex_tasks = set()
    adapter._message_handler = AsyncMock()
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, error=None))
    adapter.send_typing = AsyncMock()
    adapter._bot = SimpleNamespace(id=999, username="TripiooBot")
    return adapter


def _message(user_id=OWNER_ID):
    return SimpleNamespace(
        chat=SimpleNamespace(id=int(CHAT_ID), type="supergroup", is_forum=True),
        from_user=SimpleNamespace(id=int(user_id)),
        message_thread_id=int(THREAD_ID),
    )


def test_direct_route_is_explicit_and_owner_only():
    route = direct_codex_route_for_source(_group_topics(), _source())
    assert route is not None
    assert route.key == f"telegram:{CHAT_ID}:{THREAD_ID}"
    assert route.authorizes(OWNER_ID)
    assert not route.authorizes("999")
    assert route.cwd == "/home/hermes"
    assert route.model is None


@pytest.mark.parametrize(
    "owners,cwd",
    [([], "/home/hermes"), ([OWNER_ID], "relative/path"), (None, "")],
)
def test_direct_route_fails_closed_without_owner_or_absolute_cwd(owners, cwd):
    route = direct_codex_route_for_source(
        _group_topics(owners=owners, cwd=cwd),
        _source(),
    )
    assert route is not None
    assert not route.authorizes(OWNER_ID)


def test_direct_topic_bypasses_mentions_only_for_owner_and_is_never_observed():
    adapter = _adapter()
    adapter._is_group_chat = lambda _message: True
    assert adapter._should_process_message(_message(OWNER_ID)) is True
    assert adapter._should_process_message(_message("999")) is False
    assert adapter._should_observe_unmentioned_group_message(_message("999")) is False


@pytest.mark.asyncio
async def test_owner_event_runs_codex_without_calling_hermes():
    fake_bridge = SimpleNamespace(
        run_turn=AsyncMock(
            return_value=CodexTopicRunResult(
                thread_id="thr_test",
                text="готово",
                created=False,
                title="Tripio / system",
            )
        )
    )
    adapter = _adapter(fake_bridge)
    event = _event()

    await adapter.handle_message(event)
    await asyncio.gather(*list(adapter._direct_codex_tasks))

    fake_bridge.run_turn.assert_awaited_once()
    adapter._message_handler.assert_not_awaited()
    assert any("готово" in call.args[1] for call in adapter.send.await_args_list)


@pytest.mark.asyncio
async def test_owner_raw_telegram_id_survives_cleared_attribution_source():
    fake_bridge = SimpleNamespace(
        run_turn=AsyncMock(
            return_value=CodexTopicRunResult(
                thread_id="thr_test",
                text="готово",
                created=False,
                title="Tripio / system",
            )
        )
    )
    adapter = _adapter(fake_bridge)
    event = _event()
    event.source.user_id = None
    event.raw_message = _message(OWNER_ID)

    await adapter.handle_message(event)
    await asyncio.gather(*list(adapter._direct_codex_tasks))

    fake_bridge.run_turn.assert_awaited_once()
    adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_telegram_id_cannot_be_overridden_by_normalized_source():
    fake_bridge = SimpleNamespace(run_turn=AsyncMock())
    adapter = _adapter(fake_bridge)
    event = _event(user_id=OWNER_ID)
    event.raw_message = _message("999")

    await adapter.handle_message(event)

    fake_bridge.run_turn.assert_not_awaited()
    adapter._message_handler.assert_not_awaited()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_unauthorized_event_reaches_neither_codex_nor_hermes():
    fake_bridge = SimpleNamespace(run_turn=AsyncMock())
    adapter = _adapter(fake_bridge)

    await adapter.handle_message(_event(user_id="999"))

    fake_bridge.run_turn.assert_not_awaited()
    adapter._message_handler.assert_not_awaited()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_resets_mapping_but_keeps_old_codex_task(tmp_path):
    route = DirectCodexTopicRoute(
        chat_id=CHAT_ID,
        thread_id=THREAD_ID,
        owner_user_ids=frozenset({OWNER_ID}),
        cwd="/home/hermes",
    )
    state_path = tmp_path / "codex_topic_bridge.json"
    bridge = CodexTopicBridge(state_path=state_path, codex_binary="codex")
    bridge._state["routes"][route.key] = {
        "thread_id": "thr_old",
        "title": "old task",
    }
    bridge._save_state()

    assert await bridge.reset(route) == "thr_old"
    assert (await bridge.status(route)).get("thread_id") is None
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_prompt_keeps_reply_context_and_routes_images_separately():
    event = _event("исправь это")
    event.reply_to_text = "старое сообщение"
    event.media_urls = ["/tmp/screenshot.png", "/tmp/report.txt"]
    event.media_types = ["image/png", "text/plain"]

    prompt, images, attachments = TelegramAdapter._direct_codex_prompt_and_media(event)

    assert "исправь это" in prompt
    assert "старое сообщение" in prompt
    assert images == ["/tmp/screenshot.png"]
    assert attachments == ["/tmp/report.txt"]
