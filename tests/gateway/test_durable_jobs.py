import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.durable_jobs import DurableJobStore
from gateway.job_router import JobRouteDecision
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner, _is_retryable_provider_failure_result
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


def test_inbox_event_is_not_a_job_until_semantic_route(tmp_path):
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    source = make_restart_source(chat_id="semantic-chat", thread_id="topic-1")
    thread_key = "agent:main:telegram:dm:semantic-chat:topic-1"

    inbox, created = store.ingest_event(
        thread_key=thread_key,
        platform="telegram",
        source=source,
        request_text="Подготовь меню",
        message_id="semantic-1",
    )

    assert created is True
    assert inbox["job_id"] is None
    assert store.active_jobs() == []
    job, job_created = store.create_job_for_event(inbox["event_id"])
    assert job_created is True
    assert job["thread_key"] == thread_key
    assert job["session_key"].endswith(f":job:{job['job_id']}")
    assert job["input_version"] == 1


def test_voice_inbox_is_enriched_before_job_creation(tmp_path):
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    source = make_restart_source(chat_id="voice-chat", thread_id="homeassistant")
    thread_key = "agent:main:telegram:group:voice-chat:homeassistant"
    inbox, _ = store.ingest_event(
        thread_key=thread_key,
        platform="telegram",
        source=source,
        request_text="",
        message_id="voice-1",
        message_type="voice",
        media=[{"path": "/tmp/voice.ogg", "type": "audio/ogg"}],
    )

    enriched = store.enrich_inbox_event(
        inbox["event_id"],
        request_text="Расшифрованная команда Home Assistant",
        event_metadata={"telegram_voice_preprocessed": True},
    )
    job, _ = store.create_job_for_event(enriched["event_id"])

    assert enriched["status"] == "received"
    assert job["request_text"] == "Расшифрованная команда Home Assistant"
    assert store.inputs_for_job(job["job_id"])[0]["request_text"] == (
        "Расшифрованная команда Home Assistant"
    )


def test_transcript_only_inbox_never_becomes_recoverable_job(tmp_path):
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    source = make_restart_source(chat_id="voice-chat", thread_id="family")
    inbox, _ = store.ingest_event(
        thread_key="agent:main:telegram:group:voice-chat:family",
        platform="telegram",
        source=source,
        request_text="",
        message_id="voice-context-1",
        message_type="voice",
    )

    finished = store.mark_inbox_context_only(
        inbox["event_id"],
        request_text="Фоновая расшифровка",
        event_metadata={"telegram_voice_transcript_only": True},
    )

    assert finished["status"] == "context_only"
    assert finished["route_action"] == "context_only"
    assert store.unrouted_inbox_events() == []
    assert store.active_jobs() == []


def test_scope_update_increments_version_and_fences_stale_result(tmp_path):
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    source = make_restart_source(chat_id="scope-chat", thread_id="topic-2")
    thread_key = "agent:main:telegram:dm:scope-chat:topic-2"
    first, _ = store.ingest_event(
        thread_key=thread_key,
        platform="telegram",
        source=source,
        request_text="Составь меню на неделю",
        message_id="scope-1",
    )
    job, _ = store.create_job_for_event(first["event_id"])
    store.mark_running(job["job_id"], owner_instance="boot")
    update, _ = store.ingest_event(
        thread_key=thread_key,
        platform="telegram",
        source=source,
        request_text="И не забудь добавить рецепт",
        message_id="scope-2",
    )

    attached = store.attach_event_to_job(update["event_id"], job["job_id"])

    assert attached["input_version"] == 2
    assert "И не забудь добавить рецепт" in attached["request_text"]
    assert [row["input_version"] for row in store.inputs_for_job(job["job_id"])] == [1, 2]
    assert store.complete(job["job_id"], "Старый ответ", input_version=1) is False
    assert store.get(job["job_id"])["status"] == "resume_pending"
    assert store.complete(job["job_id"], "Меню и рецепт", input_version=2) is True


def test_scope_update_reopens_completed_result_until_delivery(tmp_path):
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    source = make_restart_source(chat_id="delivery-fence", thread_id="topic-2b")
    thread_key = "agent:main:telegram:dm:delivery-fence:topic-2b"
    first, _ = store.ingest_event(
        thread_key=thread_key,
        platform="telegram",
        source=source,
        request_text="Составь меню",
        message_id="delivery-fence-1",
    )
    job, _ = store.create_job_for_event(first["event_id"])
    store.mark_running(job["job_id"], owner_instance="boot")
    assert store.complete(job["job_id"], "Меню", input_version=1) is True
    assert [row["job_id"] for row in store.active_for_thread(thread_key)] == [
        job["job_id"]
    ]
    update, _ = store.ingest_event(
        thread_key=thread_key,
        platform="telegram",
        source=source,
        request_text="И добавь рецепт",
        message_id="delivery-fence-2",
    )

    reopened = store.attach_event_to_job(update["event_id"], job["job_id"])

    assert reopened["status"] == "resume_pending"
    assert reopened["input_version"] == 2
    assert reopened["result_text"] is None
    assert reopened["completed_at"] is None


def test_independent_events_in_same_thread_get_parallel_execution_keys(tmp_path):
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    source = make_restart_source(chat_id="parallel-chat", thread_id="topic-3")
    thread_key = "agent:main:telegram:dm:parallel-chat:topic-3"
    jobs = []
    for index, text in enumerate(("Проверь отчёт", "Нарисуй логотип"), start=1):
        inbox, _ = store.ingest_event(
            thread_key=thread_key,
            platform="telegram",
            source=source,
            request_text=text,
            message_id=f"parallel-{index}",
        )
        job, _ = store.create_job_for_event(inbox["event_id"])
        jobs.append(job)

    assert jobs[0]["job_id"] != jobs[1]["job_id"]
    assert jobs[0]["session_key"] != jobs[1]["session_key"]
    assert {job["thread_key"] for job in jobs} == {thread_key}


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


@pytest.mark.parametrize(
    "result",
    [
        {
            "failed": True,
            "completed": False,
            "final_response": "API call failed after 3 retries: Connection error.",
            "error": "Connection error.",
        },
        {
            "failed": True,
            "completed": False,
            "final_response": "Rate limited after 3 retries",
            "error": "HTTP 429 too many requests",
        },
        {
            "failed": True,
            "completed": False,
            "final_response": "API call failed after 3 retries: HTTP 503",
            "error": "Service unavailable",
        },
    ],
)
def test_retryable_provider_failures_keep_durable_job_active(result):
    assert _is_retryable_provider_failure_result(result) is True


@pytest.mark.parametrize(
    "result",
    [
        {
            "failed": True,
            "final_response": "Provider authentication failed: invalid API key",
            "error": "invalid API key",
        },
        {
            "failed": True,
            "final_response": "Billing or credits exhausted",
            "error": "insufficient_quota",
        },
        {
            "failed": True,
            "compression_exhausted": True,
            "final_response": "API call failed after 3 retries",
            "error": "maximum context length exceeded",
        },
        {
            "failed": False,
            "final_response": "API call failed after 3 retries",
            "error": "Connection error.",
        },
    ],
)
def test_permanent_or_successful_provider_results_do_not_auto_retry(result):
    assert _is_retryable_provider_failure_result(result) is False


@pytest.mark.asyncio
async def test_provider_outage_defers_completion_and_keeps_same_job(tmp_path):
    runner, _adapter = make_restart_runner()
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    runner._durable_job_store = store
    runner._durable_job_instance = "provider-retry-boot"
    runner._scheduled_durable_job_ids = set()
    runner._provider_retry_job_ids = set()
    runner._active_job_ids = {}

    source = make_restart_source(
        chat_id="provider-retry-chat",
        thread_id="home-assistant",
    )
    session_key = runner._session_key_for_source(source)
    job, _ = store.create_or_get(
        session_key=session_key,
        platform="telegram",
        source=source,
        request_text="Проверь ошибку Home Assistant",
        message_id="provider-retry-1",
    )
    event = MessageEvent(
        text="",
        message_type=MessageType.TEXT,
        source=source,
        internal=True,
        durable_job_id=job["job_id"],
        durable_recovery=True,
        durable_request_text="Проверь ошибку Home Assistant",
        job_execution_key=session_key,
    )

    async def _provider_failed(event_arg, *_args, **_kwargs):
        event_arg.durable_defer_completion = True
        event_arg.durable_resume_reason = "provider_unavailable"
        # Prove that the defer fence wins even if a warning string leaks up.
        return "The model provider failed after retries"

    runner._handle_message_with_agent = AsyncMock(side_effect=_provider_failed)
    runner._schedule_durable_job_provider_retry = MagicMock(return_value=True)

    response = await runner._handle_message(event)

    stored = store.get(job["job_id"])
    assert response is None
    assert stored["job_id"] == job["job_id"]
    assert stored["status"] == "resume_pending"
    assert stored["resume_reason"] == "provider_unavailable"
    assert stored["completed_at"] is None
    runner._schedule_durable_job_provider_retry.assert_called_once_with(job["job_id"])


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
    assert event.job_thread_key == session_key
    assert event.job_execution_key == session_key
    assert event.job_input_version == 1
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
        message_id="1",
        message_ids=["1"],
    )


@pytest.mark.asyncio
async def test_adapter_suppresses_response_rejected_by_job_version_fence():
    runner, adapter = make_restart_runner()
    event = MessageEvent(
        text="do work",
        message_type=MessageType.TEXT,
        source=make_restart_source(chat_id="stale-chat"),
        durable_job_id="gw_stale",
    )
    session_key = runner._session_key_for_source(event.source)
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter.set_message_handler(AsyncMock(return_value="Устаревший ответ"))
    validator = AsyncMock(return_value=False)
    receipt = AsyncMock()
    adapter.set_durable_job_delivery_validator(validator)
    adapter.set_durable_job_delivery_handler(receipt)

    await adapter._process_message_background(event, session_key)

    validator.assert_awaited_once_with(event)
    assert adapter.sent == []
    receipt.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_runner_attaches_contextual_update_without_second_turn(tmp_path, monkeypatch):
    runner, _adapter = make_restart_runner()
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    runner._durable_job_store = store
    runner._durable_job_instance = "boot"
    runner._job_consumed_input_versions = {}
    source = make_restart_source(chat_id="recipe-chat", thread_id="topic-4")
    thread_key = runner._session_key_for_source(source)
    first = MessageEvent(
        text="Составь меню на неделю",
        message_type=MessageType.TEXT,
        source=source,
        message_id="recipe-1",
    )
    first_job, handled = await runner._ensure_durable_job_route(
        first, source, thread_key
    )
    assert handled is False
    store.mark_running(first_job["job_id"], owner_instance="boot")
    running_agent = MagicMock()
    running_agent.steer.return_value = True
    runner._running_agents[first_job["session_key"]] = running_agent
    monkeypatch.setattr(
        "gateway.job_router.decide_job_route",
        AsyncMock(
            return_value=JobRouteDecision(
                "attach",
                first_job["job_id"],
                0.99,
                "recipe is a scope addition",
            )
        ),
    )
    update = MessageEvent(
        text="И не забудь добавить рецепт",
        message_type=MessageType.TEXT,
        source=source,
        message_id="recipe-2",
    )

    attached_job, handled = await runner._ensure_durable_job_route(
        update, source, thread_key
    )

    assert handled is True
    assert attached_job["job_id"] == first_job["job_id"]
    assert attached_job["input_version"] == 2
    running_agent.steer.assert_called_once_with(
        "И не забудь добавить рецепт",
        input_version=2,
    )
    assert first_job["job_id"] not in runner._job_consumed_input_versions


@pytest.mark.asyncio
async def test_prerouted_busy_event_claims_job_but_fresh_redelivery_is_suppressed(
    tmp_path,
):
    runner, _adapter = make_restart_runner()
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    runner._durable_job_store = store
    runner._durable_job_instance = "boot"
    source = make_restart_source(chat_id="busy-chat", thread_id="topic-busy")
    thread_key = runner._session_key_for_source(source)
    event = MessageEvent(
        text="Трипио, озвучь спасибо",
        message_type=MessageType.TEXT,
        source=source,
        message_id="busy-1",
        platform_update_id=2001,
    )

    job, handled = await runner._ensure_durable_job_route(
        event, source, thread_key
    )
    assert handled is False
    assert event.job_route_action == "new_job"
    assert store.get(job["job_id"])["status"] == "pending"

    # The busy-session handler sends this same, explicitly routed event to a
    # job-specific adapter lane. Its second routing pass must continue into
    # _prepare_durable_job() instead of mistaking itself for a redelivery.
    same_job, handled = await runner._ensure_durable_job_route(
        event, source, thread_key
    )
    assert same_job["job_id"] == job["job_id"]
    assert handled is False

    # A genuinely fresh copy of the same Telegram update must remain deduped.
    duplicate = MessageEvent(
        text="Трипио, озвучь спасибо",
        message_type=MessageType.TEXT,
        source=source,
        message_id="busy-1",
        platform_update_id=2001,
    )
    duplicate_job, handled = await runner._ensure_durable_job_route(
        duplicate, source, thread_key
    )
    assert duplicate_job["job_id"] == job["job_id"]
    assert handled is True


@pytest.mark.asyncio
async def test_stop_cancels_all_parallel_jobs_in_public_thread(tmp_path):
    runner, _adapter = make_restart_runner()
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    runner._durable_job_store = store
    source = make_restart_source(chat_id="stop-chat", thread_id="topic-stop")
    thread_key = runner._session_key_for_source(source)
    jobs = []
    agents = []
    for index in range(2):
        inbox, _ = store.ingest_event(
            thread_key=thread_key,
            platform="telegram",
            source=source,
            request_text=f"Задача {index}",
            message_id=f"stop-{index}",
        )
        job, _ = store.create_job_for_event(inbox["event_id"])
        store.mark_running(job["job_id"], owner_instance="boot")
        agent = MagicMock()
        runner._running_agents[job["session_key"]] = agent
        jobs.append(job)
        agents.append(agent)
    event = MessageEvent(
        text="/stop",
        message_type=MessageType.TEXT,
        source=source,
    )

    await runner._handle_stop_command(event)

    assert all(store.get(job["job_id"])["status"] == "cancelled" for job in jobs)
    assert all(job["session_key"] not in runner._running_agents for job in jobs)
    assert all(agent.interrupt.called for agent in agents)
