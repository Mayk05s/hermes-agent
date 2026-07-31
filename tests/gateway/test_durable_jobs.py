import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.durable_jobs import DurableJobStore
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import (
    GatewayRunner,
    _prepend_durable_job_registration_note,
)
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)


def _new_job(store: DurableJobStore, *, message_id: str = "m1"):
    source = make_restart_source(chat_id="job-chat", thread_id="topic-7")
    return store.create_or_get(
        session_key="agent:main:telegram:dm:job-chat:topic-7",
        platform="telegram",
        source=source,
        request_text="Исправь задачу и пришли итог",
        message_id=message_id,
        platform_update_id=123,
    )


def test_current_turn_reuses_gateway_job_instead_of_creating_registry_cron():
    original = "[Mikhail|179555559]\nСгенерируй картинку"

    enriched = _prepend_durable_job_registration_note(
        original,
        "gw_existing123",
    )

    assert "already registered as durable gateway job gw_existing123" in enriched
    assert "Do not call cronjob merely to obtain a Job ID" in enriched
    assert "Use cronjob only when the user explicitly requests" in enriched
    assert enriched.endswith(original)


def test_durable_registration_note_skips_non_durable_and_recovery_turns():
    original = "Продолжи задачу"

    assert _prepend_durable_job_registration_note(original, None) == original
    assert (
        _prepend_durable_job_registration_note(
            original,
            "gw_resume123",
            durable_recovery=True,
        )
        == original
    )


def test_job_id_and_lifecycle_survive_store_reopen(tmp_path):
    path = tmp_path / "gateway_jobs.sqlite3"
    store = DurableJobStore(path)
    job, created = _new_job(store)

    assert created is True
    assert job["job_id"].startswith("gw_")
    assert job["status"] == "pending"

    store.mark_running(job["job_id"], owner_instance="boot-1")
    store.mark_resume_pending(job["job_id"], "restart_timeout")

    reopened = DurableJobStore(path)
    recovered = reopened.get(job["job_id"])
    assert recovered["status"] == "resume_pending"
    assert recovered["resume_reason"] == "restart_timeout"

    reopened.complete(job["job_id"], "Готово")
    terminal = reopened.get(job["job_id"])
    assert terminal["status"] == "completed"
    assert terminal["delivered_at"] is None
    assert [row["job_id"] for row in reopened.terminal_undelivered_jobs()] == [
        job["job_id"]
    ]

    reopened.record_delivery(job["job_id"], success=True)
    delivered = reopened.get(job["job_id"])
    assert delivered["delivered_at"] is not None
    assert delivered["delivery_attempts"] == 1
    assert reopened.terminal_undelivered_jobs() == []


def test_redelivered_platform_update_reuses_terminal_job(tmp_path):
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    first, created = _new_job(store)
    assert created is True
    store.complete(first["job_id"], "Итог", delivered=True)

    second, created_again = _new_job(store)
    assert created_again is False
    assert second["job_id"] == first["job_id"]
    assert second["status"] == "completed"


def test_cancelled_job_is_not_recovered(tmp_path):
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    job, _ = _new_job(store)

    assert store.cancel(job["job_id"], "Stop requested") is True
    assert store.get(job["job_id"])["status"] == "cancelled"
    assert store.active_jobs() == []


def test_runner_assigns_job_before_work_and_suppresses_redelivery(tmp_path):
    runner, _adapter = make_restart_runner()
    runner._durable_job_store = DurableJobStore(
        tmp_path / "gateway_jobs.sqlite3"
    )
    runner._durable_job_instance = "boot-1"
    runner._scheduled_durable_job_ids = set()
    runner._active_job_ids = {}

    source = make_restart_source(chat_id="assigned-chat")
    event = MessageEvent(
        text="Выполни задачу",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m-assigned",
        platform_update_id=707,
        durable_request_text="Выполни задачу",
    )
    session_key = runner._session_key_for_source(source)

    job_id, suppress = runner._prepare_durable_job(
        event,
        source,
        session_key,
    )

    assert suppress is False
    assert event.durable_job_id == job_id
    assert runner._active_job_ids[session_key] == job_id
    assert runner._durable_job_store.get(job_id)["status"] == "running"

    runner._durable_job_store.complete(job_id, "Готово", delivered=True)
    redelivered_event = MessageEvent(
        text="Выполни задачу",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m-assigned",
        platform_update_id=707,
        durable_request_text="Выполни задачу",
    )
    same_job_id, suppress = runner._prepare_durable_job(
        redelivered_event,
        source,
        session_key,
    )

    assert same_job_id == job_id
    assert suppress is True


@pytest.mark.asyncio
async def test_startup_recovery_reuses_job_id_and_original_request(tmp_path):
    runner, adapter = make_restart_runner()
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    source = make_restart_source(chat_id="resume-chat", thread_id="topic-1")
    session_key = "agent:main:telegram:dm:resume-chat:topic-1"
    job, _ = store.create_or_get(
        session_key=session_key,
        platform="telegram",
        source=source,
        request_text="Заверши исходную задачу",
        message_id="m-resume",
        platform_update_id=999,
    )
    store.mark_running(job["job_id"], owner_instance="old-boot")

    runner._durable_job_store = store
    runner._durable_job_instance = "new-boot"
    runner._scheduled_durable_job_ids = set()
    runner._active_job_ids = {}
    runner.session_store.mark_resume_pending.return_value = True
    adapter.handle_message = AsyncMock()

    scheduled, claimed = runner._schedule_durable_jobs()
    await asyncio.sleep(0)

    assert scheduled == 1
    assert claimed == {session_key}
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert isinstance(event, MessageEvent)
    assert event.internal is True
    assert event.durable_recovery is True
    assert event.durable_job_id == job["job_id"]
    assert event.durable_request_text == "Заверши исходную задачу"
    assert event.text == ""

    stored = store.get(job["job_id"])
    assert stored["status"] == "resume_pending"
    assert stored["resume_attempts"] == 1
    assert stored["owner_instance"] == "new-boot"

    scheduled_again, _ = runner._schedule_durable_jobs()
    assert scheduled_again == 0


@pytest.mark.asyncio
async def test_adapter_persists_successful_delivery_receipt():
    runner, adapter = make_restart_runner()
    event = MessageEvent(
        text="do work",
        message_type=MessageType.TEXT,
        source=make_restart_source(chat_id="receipt-chat"),
        message_id="m-receipt",
        durable_job_id="gw_receipt",
    )
    session_key = runner._session_key_for_source(event.source)
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter.set_message_handler(
        AsyncMock(return_value="Готово")
    )
    receipt = AsyncMock()
    adapter.set_durable_job_delivery_handler(receipt)

    await adapter._process_message_background(event, session_key)

    receipt.assert_awaited_once_with(
        event,
        attempted=True,
        succeeded=True,
        error=None,
    )


@pytest.mark.asyncio
async def test_completed_undelivered_result_is_sent_without_agent_rerun(tmp_path):
    runner, adapter = make_restart_runner()
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    source = make_restart_source(chat_id="delivery-chat", thread_id="topic-9")
    job, _ = store.create_or_get(
        session_key="agent:main:telegram:dm:delivery-chat:topic-9",
        platform=Platform.TELEGRAM.value,
        source=source,
        request_text="Сделай",
        message_id="m-done",
        platform_update_id=1001,
    )
    store.complete(job["job_id"], "Сохранённый итог")

    runner._durable_job_store = store
    runner._durable_job_instance = "boot"
    runner._scheduled_durable_job_ids = set()
    runner._active_job_ids = {}
    runner._thread_metadata_for_source = lambda _source, _reply=None: {
        "thread_id": _source.thread_id
    }

    scheduled, claimed = runner._schedule_durable_jobs()
    assert scheduled == 1
    assert claimed == set()

    for _ in range(20):
        if store.get(job["job_id"])["delivered_at"] is not None:
            break
        await asyncio.sleep(0)

    assert store.get(job["job_id"])["delivered_at"] is not None
    assert adapter.sent == [
        "Сохранённый итог"
    ]
