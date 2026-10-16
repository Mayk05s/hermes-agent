from types import SimpleNamespace

import pytest

from gateway.miniapp_reply_contract import MiniappReplyCollector, apply_miniapp_reply_contract
from gateway.platforms.base import MessageEvent, MessageType, Platform, SessionSource
from tests.gateway.test_shopping_inline_button_contract import _adapter, _Button, _Markup


@pytest.fixture(autouse=True)
def telegram_types(monkeypatch):
    from gateway.platforms import telegram
    monkeypatch.setattr(telegram, "InlineKeyboardButton", _Button)
    monkeypatch.setattr(telegram, "InlineKeyboardMarkup", _Markup)


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", ["179555559", "-1003966683704"])
async def test_buttons_bypass_disabled_shortcuts_and_stay_on_last_chunk(chat):
    calls = []
    adapter = _adapter(calls)
    adapter.MAX_MESSAGE_LENGTH = 40
    result = await adapter.send(chat, "Задачи сохранены. " * 8, metadata={
        "thread_id": "359", "telegram_reply_to_message_id": "101", "miniapp_start_params": ["planning", "nutrition_history", "menu", "fitness", "planning"]
    })
    assert result.success
    assert len(calls) > 1
    assert all("reply_markup" not in call for call in calls[:-1])
    buttons = [row[0] for row in calls[-1]["reply_markup"].inline_keyboard]
    assert [button.url.split("startapp=")[1] for button in buttons] == ["planning", "nutrition_history", "menu", "fitness"]
    assert all(call["message_thread_id"] == 359 for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, "", "[[audio_as_voice]]\nMEDIA:/tmp/speech.ogg"])
async def test_already_streamed_or_media_only_reply_gets_button_carrier(response):
    from unittest.mock import AsyncMock, patch
    from gateway.platforms.base import SendResult
    calls = []
    adapter = _adapter(calls)
    adapter.send_typing = AsyncMock()
    adapter.send_voice = AsyncMock(return_value=SendResult(success=True, message_id="43"))
    adapter._should_auto_tts_for_chat = lambda _: False
    event = MessageEvent(text="Добавь муку", message_type=MessageType.TEXT,
                         source=SessionSource(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group", thread_id="359"))

    async def handler(event):
        event.outbound_metadata["miniapp_start_params"] = ["shopping_buy_exact"]
        return response

    adapter.set_message_handler(handler)
    with patch("gateway.platforms.base.Path.exists", return_value=True):
        await adapter._process_message_background(event, "session-one")
    assert len(calls) == 1
    button = calls[0]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Открыть список"
    assert button.url.endswith("startapp=shopping_buy_exact")


@pytest.mark.asyncio
async def test_final_edit_includes_mandatory_keyboard():
    from unittest.mock import AsyncMock
    calls = []
    adapter = _adapter(calls)
    adapter._bot.edit_message_text = AsyncMock()
    sent = await adapter.edit_message("179555559", "42", "Готово", finalize=True,
                                     metadata={"miniapp_start_params": ["menu"]})
    assert sent.success
    button = adapter._bot.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.url.endswith("startapp=menu")


@pytest.mark.asyncio
async def test_invalid_metadata_cannot_create_external_or_unknown_buttons():
    calls = []
    adapter = _adapter(calls)
    await adapter.send("179555559", "Готово", metadata={"miniapp_start_params": [
        "https://evil.example/", "menu&redirect=evil", "unrecognized", {}, "a" * 65
    ]})
    assert "reply_markup" not in calls[0]

@pytest.mark.asyncio
async def test_stale_durable_action_only_completion_is_suppressed():
    calls = []
    adapter = _adapter(calls)
    adapter._durable_job_delivery_validator = lambda event: False
    event = MessageEvent(text="Измени список", message_type=MessageType.TEXT,
                         source=SessionSource(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group"))
    event.durable_job_id = "stale-job"

    async def handler(event):
        event.outbound_metadata["miniapp_start_params"] = ["shopping_buy_exact"]
        return None

    adapter.set_message_handler(handler)
    await adapter._process_message_background(event, "session-stale")
    assert calls == []
    assert "miniapp_start_params" not in event.outbound_metadata


@pytest.mark.asyncio
async def test_button_metadata_survives_gateway_store_reopen_and_redelivery(tmp_path):
    import json
    from unittest.mock import AsyncMock
    from gateway.durable_jobs import DurableJobStore
    from gateway.platforms.base import SendResult
    from tests.gateway.test_durable_jobs import _new_job
    from tests.gateway.restart_test_helpers import make_restart_runner

    path = tmp_path / "jobs.sqlite3"
    store = DurableJobStore(path)
    job, _ = _new_job(store)
    runner, adapter = make_restart_runner()
    runner._durable_job_store = store
    event = MessageEvent(text="Создай список", source=SessionSource(platform=Platform.TELEGRAM, chat_id="job-chat"))
    event.durable_job_id = job["job_id"]
    event.outbound_metadata["miniapp_start_params"] = ["shopping_buy_exact", "menu"]
    assert runner._complete_durable_job(event, "Готово")

    store = DurableJobStore(path)
    runner._durable_job_store = store
    saved = store.get(job["job_id"])
    assert json.loads(saved["result_metadata_json"]) == event.outbound_metadata
    assert saved["delivered_at"] is None
    adapter._send_with_retry = AsyncMock(return_value=SendResult(success=True, message_id="delivered-1"))
    assert await runner._redeliver_durable_job(saved)
    metadata = adapter._send_with_retry.call_args.kwargs["metadata"]
    assert metadata["miniapp_start_params"] == ["shopping_buy_exact", "menu"]
    assert store.get(job["job_id"])["delivered_at"] is not None

@pytest.mark.asyncio
async def test_multiple_shopping_buttons_name_their_lists():
    import base64
    calls = []
    adapter = _adapter(calls)
    params = ["shopping_buy_" + base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")
              for name in ["Покупки", "На море"]]
    await adapter.send("-1001", "Списки обновлены", metadata={"miniapp_start_params": params})
    buttons = [row[0] for row in calls[0]["reply_markup"].inline_keyboard]
    assert [button.text for button in buttons] == ["Открыть «Покупки»", "Открыть «На море»"]
