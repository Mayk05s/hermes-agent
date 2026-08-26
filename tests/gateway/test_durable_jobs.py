import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.durable_jobs import DurableJobStore
from gateway.job_router import JobRouteDecision
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import (
    GatewayRunner,
    _is_retryable_provider_failure_result,
    _is_self_contained_tts_request,
)
from gateway.session import SessionEntry
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


def test_shared_group_inbox_persists_verified_turn_author(tmp_path):
    runner, _adapter = make_restart_runner()
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    runner._durable_job_store = store
    shared_source = replace(
        make_restart_source(
            chat_id="-1004288314847",
            chat_type="group",
        ),
        user_id=None,
        user_name=None,
        profile_name="hudeem-tripio",
    )
    event = MessageEvent(
        text="[Татьяна|5482704149]\nЕщё +100",
        source=shared_source,
        message_id="7700",
        raw_message=SimpleNamespace(
            from_user=SimpleNamespace(
                id=5482704149,
                full_name="Татьяна",
                username="tatyana",
            )
        ),
    )

    inbox, created = runner._ingest_durable_inbox_event(
        event,
        shared_source,
        runner._session_key_for_source(shared_source),
    )

    assert created is True
    assert shared_source.user_id is None
    persisted_source = store.source_dict(inbox)
    assert persisted_source["user_id"] == "5482704149"
    assert persisted_source["user_name"] == "Татьяна"


def test_recovered_group_inbox_restores_author_from_gateway_attribution(tmp_path):
    runner, _adapter = make_restart_runner()
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    runner._durable_job_store = store
    shared_source = replace(
        make_restart_source(
            chat_id="-1004288314847",
            chat_type="group",
        ),
        user_id=None,
        user_name=None,
        profile_name="hudeem-tripio",
    )
    event = MessageEvent(
        text="[Татьяна|5482704149]\nИсправь дыню на 300 г",
        source=shared_source,
        message_id="7710",
    )

    inbox, _created = runner._ingest_durable_inbox_event(
        event,
        shared_source,
        runner._session_key_for_source(shared_source),
    )

    persisted_source = store.source_dict(inbox)
    assert persisted_source["user_id"] == "5482704149"
    assert persisted_source["user_name"] == "Татьяна"


def test_new_job_history_preserves_observed_topic_context(tmp_path):
    runner, _adapter = make_restart_runner()
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    runner._durable_job_store = store
    source = make_restart_source(
        chat_id="family-chat",
        chat_type="group",
        thread_id="family-menu",
    )
    public_key = runner._session_key_for_source(source)
    job, _created = store.create_or_get(
        session_key=public_key,
        platform="telegram",
        source=source,
        request_text="@TripiooBot добавь в текущий список",
        message_id="addressed-3",
    )
    public_entry = SessionEntry(
        session_key=public_key,
        session_id="public-session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    private_entry = SessionEntry(
        session_key=job["session_key"],
        session_id="private-job-session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    runner.session_store._entries[public_key] = public_entry
    runner.session_store.load_transcript.return_value = [
        {
            "role": "user",
            "content": "[Наталия|u2]\nНектарины за 5 лари",
            "message_id": "unmentioned-2",
            "observed": True,
        }
    ]
    event = MessageEvent(
        text="@TripiooBot добавь в текущий список",
        source=source,
        message_id="addressed-3",
        durable_job_id=job["job_id"],
    )

    history = runner._seed_new_job_history(event, private_entry)

    assert history == [
        {
            "role": "user",
            "content": "[Наталия|u2]\nНектарины за 5 лари",
            "message_id": "unmentioned-2",
            "observed": True,
        }
    ]
    runner.session_store.rewrite_transcript.assert_called_once_with(
        "private-job-session", history
    )


def test_self_contained_named_voice_tts_request_isolated_from_old_work(tmp_path):
    runner, _adapter = make_restart_runner()
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    runner._durable_job_store = store
    source = replace(
        make_restart_source(
            chat_id="-1003938895426",
            chat_type="group",
            thread_id="35",
        ),
        profile_name="boxmap",
    )
    thread_key = runner._session_key_for_source(source)
    public_entry = SessionEntry(
        session_key=thread_key,
        session_id="public-session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    runner.session_store._entries[thread_key] = public_entry
    runner.session_store.load_transcript.return_value = [
        {"role": "user", "content": "Опиши старый сценарий полностью"}
    ]
    inbox, _ = store.ingest_event(
        thread_key=thread_key,
        platform="telegram",
        source=source,
        request_text='Трипио, сделай озвучку голосом Charon "уже всё обыскал"',
        message_id="2542",
    )
    job, _ = store.create_job_for_event(inbox["event_id"])
    event = MessageEvent(
        text=job["request_text"],
        source=source,
        message_id="2542",
        durable_job_id=job["job_id"],
    )
    private_entry = SessionEntry(
        session_key=job["session_key"],
        session_id="private-tts-session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )

    history = runner._seed_new_job_history(event, private_entry)

    assert history == []
    runner.session_store.load_transcript.assert_not_called()
    runner.session_store.rewrite_transcript.assert_not_called()
    with store._lock, store._connect() as conn:
        seeded = dict(
            conn.execute(
                "SELECT event_type, payload_json FROM gateway_job_events "
                "WHERE job_id = ? ORDER BY seq DESC LIMIT 1",
                (job["job_id"],),
            ).fetchone()
        )
    assert seeded["event_type"] == "context_seeded"
    assert '"suppressed_reason":"self_contained_tts"' in seeded["payload_json"].replace(" ", "")


def test_self_contained_tts_detection_keeps_context_dependent_voice_requests():
    assert _is_self_contained_tts_request(
        'Трипио, сделай озвучку голоса чхарон "уже все обыскал"'
    )
    assert not _is_self_contained_tts_request(
        'Трипио, озвучь "спасибо" тем же прошлым голосом'
    )


def test_new_job_history_skips_stale_public_topic_session(tmp_path):
    runner, _adapter = make_restart_runner()
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    runner._durable_job_store = store
    source = replace(
        make_restart_source(
            chat_id="-1003938895426",
            chat_type="group",
            thread_id="35",
        ),
        profile_name="boxmap",
    )
    thread_key = runner._session_key_for_source(source)
    runner.session_store._entries[thread_key] = SessionEntry(
        session_key=thread_key,
        session_id="stale-public-session",
        created_at=datetime.now() - timedelta(days=3),
        updated_at=datetime.now() - timedelta(days=2),
    )
    runner.session_store.load_transcript.return_value = [
        {"role": "user", "content": "Продолжи старый сценарий"}
    ]
    inbox, _ = store.ingest_event(
        thread_key=thread_key,
        platform="telegram",
        source=source,
        request_text="Трипио, какая погода?",
        message_id="3000",
    )
    job, _ = store.create_job_for_event(inbox["event_id"])
    event = MessageEvent(
        text=job["request_text"],
        source=source,
        message_id="3000",
        durable_job_id=job["job_id"],
    )
    private_entry = SessionEntry(
        session_key=job["session_key"],
        session_id="private-current-session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )

    history = runner._seed_new_job_history(event, private_entry)

    assert history == []
    runner.session_store.load_transcript.assert_not_called()


def test_trusted_group_request_history_excludes_messages_outside_window(tmp_path):
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    source = replace(
        make_restart_source(
            chat_id="-1003938895426",
            chat_type="group",
            thread_id="35",
        ),
        profile_name="boxmap",
    )
    thread_key = "agent:main:profile:boxmap:telegram:group:-1003938895426:35"
    prior, _ = store.ingest_event(
        thread_key=thread_key,
        platform="telegram",
        source=source,
        request_text="Старая задача про сценарий",
        message_id="old",
    )
    current, _ = store.ingest_event(
        thread_key=thread_key,
        platform="telegram",
        source=source,
        request_text="Новая отдельная задача",
        message_id="current",
    )
    current_job, _ = store.create_job_for_event(current["event_id"])
    with store._lock, store._connect() as conn:
        conn.execute(
            "UPDATE gateway_inbox_events SET created_at = 100 WHERE event_id = ?",
            (prior["event_id"],),
        )
        conn.execute(
            "UPDATE gateway_inbox_events SET created_at = 1000 WHERE event_id = ?",
            (current["event_id"],),
        )

    history = store.trusted_group_request_history(
        job_id=current_job["job_id"],
        window_seconds=60,
    )

    assert history == []


def test_new_job_history_includes_prior_durable_messages_from_whole_group_topic(tmp_path):
    runner, _adapter = make_restart_runner()
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    runner._durable_job_store = store
    base_source = replace(
        make_restart_source(
            chat_id="-1003966683704",
            chat_type="group",
            thread_id="1636",
        ),
        profile_name="family-chat",
        scope_name="default",
        memory_scope="default",
    )
    natalia_source = replace(
        base_source,
        user_id="367599252",
        user_name="Наталия",
    )
    mikhail_source = replace(
        base_source,
        user_id="179555559",
        user_name="Mikhail",
    )
    thread_key = runner._session_key_for_source(base_source)

    prior_messages = [
        (
            natalia_source,
            "2900",
            "Масло сливочное\nКартошка\nКуриное филе",
        ),
        (natalia_source, "2901", "Нектарины за 5 лари"),
    ]
    for source, message_id, text in prior_messages:
        inbox, _ = store.ingest_event(
            thread_key=thread_key,
            platform="telegram",
            source=source,
            request_text=text,
            message_id=message_id,
        )
        prior_job, _ = store.create_job_for_event(inbox["event_id"])
        store.complete(prior_job["job_id"], "[[silent]]")

    # Same chat but a different topic must never bleed into family-menu.
    other_topic = replace(natalia_source, thread_id="359")
    other_inbox, _ = store.ingest_event(
        thread_key=runner._session_key_for_source(other_topic),
        platform="telegram",
        source=other_topic,
        request_text="Секрет из другой темы",
        message_id="other-topic",
    )
    other_job, _ = store.create_job_for_event(other_inbox["event_id"])
    store.complete(other_job["job_id"], "[[silent]]")

    current_inbox, _ = store.ingest_event(
        thread_key=thread_key,
        platform="telegram",
        source=mikhail_source,
        request_text="@TripiooBot добавить в список покупок текущий",
        message_id="2902",
    )
    current_job, _ = store.create_job_for_event(current_inbox["event_id"])
    later_inbox, _ = store.ingest_event(
        thread_key=thread_key,
        platform="telegram",
        source=mikhail_source,
        request_text="@TripiooBot ау",
        message_id="2903",
    )
    later_job, _ = store.create_job_for_event(later_inbox["event_id"])
    store.complete(later_job["job_id"], "Я здесь")
    private_entry = SessionEntry(
        session_key=current_job["session_key"],
        session_id="private-current-session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    event = MessageEvent(
        text="@TripiooBot добавить в список покупок текущий",
        source=mikhail_source,
        message_id="2902",
        durable_job_id=current_job["job_id"],
    )

    history = runner._seed_new_job_history(event, private_entry)

    assert [item.get("message_id") for item in history] == ["2900", "2901"]
    assert all(item.get("observed") is True for item in history)
    assert history[0]["content"].startswith("[Наталия|367599252]\n")
    assert "Нектарины за 5 лари" in history[1]["content"]
    assert all("Секрет из другой темы" not in item["content"] for item in history)
    assert all("добавить в список покупок текущий" not in item["content"] for item in history)
    assert all("@TripiooBot ау" not in item["content"] for item in history)
    runner.session_store.rewrite_transcript.assert_called_once_with(
        "private-current-session", history
    )


def test_child_job_merges_latest_public_topic_context_with_parent_history():
    runner, _adapter = make_restart_runner()
    store = MagicMock()
    runner._durable_job_store = store
    source = make_restart_source(
        chat_id="family-chat",
        chat_type="group",
        thread_id="family-menu",
    )
    public_key = runner._session_key_for_source(source)
    runner.session_store._entries[public_key] = SessionEntry(
        session_key=public_key,
        session_id="public-session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    child_job = {
        "job_id": "gw_child",
        "thread_key": public_key,
        "parent_job_id": "gw_parent",
    }
    parent_job = {
        "job_id": "gw_parent",
        "session_id": "parent-session",
    }
    store.get.side_effect = lambda job_id: {
        "gw_child": child_job,
        "gw_parent": parent_job,
    }.get(job_id)
    runner.session_store.load_transcript.side_effect = lambda session_id: {
        "public-session": [
            {
                "role": "user",
                "content": "[Наталия|u2]\nНектарины за 5 лари",
                "message_id": "2901",
                "observed": True,
            },
            {
                "role": "user",
                "content": "[Наталия|u2]\nЧокопай",
                "message_id": "2905",
                "observed": True,
            },
        ],
        "parent-session": [
            {
                "role": "user",
                "content": "[Наталия|u2]\nНектарины за 5 лари",
                "message_id": "2901",
                "observed": True,
            },
            {
                "role": "user",
                "content": "[Mikhail|u1]\n@TripiooBot ау",
                "message_id": "2903",
            },
            {
                "role": "assistant",
                "content": "Что выберем?",
            },
        ],
    }[session_id]
    private_entry = SessionEntry(
        session_key=f"{public_key}:job:gw_child",
        session_id="child-session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    event = MessageEvent(
        text="Тебе всё уже написали выше",
        source=source,
        durable_job_id="gw_child",
    )

    history = runner._seed_new_job_history(event, private_entry)

    assert [item.get("message_id") for item in history] == [
        "2901",
        "2905",
        "2903",
        None,
    ]
    assert history[0]["observed"] is True
    assert history[1]["observed"] is True
    runner.session_store.rewrite_transcript.assert_called_once_with(
        "child-session", history
    )
    seeded_event = store.append_job_event.call_args.args[2]
    assert seeded_event["source_session_ids"] == [
        "public-session",
        "parent-session",
    ]


@pytest.mark.asyncio
async def test_attach_race_to_completed_job_creates_child_instead_of_dropping(
    monkeypatch,
):
    runner, _adapter = make_restart_runner()
    store = MagicMock()
    runner._durable_job_store = store
    source = make_restart_source(
        chat_id="family-chat",
        chat_type="group",
        thread_id="family-menu",
    )
    thread_key = runner._session_key_for_source(source)
    inbox = {"event_id": "inbox-follow-up", "job_id": None}
    runner._ingest_durable_inbox_event = MagicMock(
        return_value=(inbox, True)
    )
    active_target = {
        "job_id": "gw_finished_between_route_and_attach",
        "status": "running",
        "branch_id": "branch-family-menu",
    }
    completed_target = {**active_target, "status": "completed"}
    child_job = {
        "job_id": "gw_child_after_attach_race",
        "session_key": f"{thread_key}:job:gw_child_after_attach_race",
        "thread_key": thread_key,
        "input_version": 1,
        "parent_job_id": active_target["job_id"],
    }
    store.active_for_thread.return_value = [active_target]
    store.recent_terminal_for_thread.return_value = []
    store.job_for_delivery_message.return_value = None
    store.get.side_effect = [active_target, completed_target]
    store.attach_event_to_job.side_effect = RuntimeError(
        "Job completed before attach"
    )
    store.create_job_for_event.return_value = (child_job, True)
    monkeypatch.setattr(
        "gateway.job_router.decide_job_route",
        AsyncMock(
            return_value=JobRouteDecision(
                action="attach",
                job_id=active_target["job_id"],
                confidence=0.99,
                reason="explicit follow-up",
            )
        ),
    )
    event = MessageEvent(
        text="добавь ещё Чокопай",
        source=source,
        message_id="follow-up-4",
    )

    job, handled = await runner._ensure_durable_job_route_locked(
        event, source, thread_key
    )

    assert job == child_job
    assert handled is False
    assert event.durable_job_id == child_job["job_id"]
    assert event.job_route_action == "new_job"
    store.create_job_for_event.assert_called_once_with(
        "inbox-follow-up",
        parent_job_id=active_target["job_id"],
        branch_id="branch-family-menu",
        routing_summary="добавь ещё Чокопай",
        confidence=0.99,
        reason="attach_race_terminal: explicit follow-up",
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_result",
    [
        {
            "final_response": (
                "Operation interrupted: waiting for model response "
                "(7.8s elapsed)."
            ),
            "completed": False,
            "interrupted": True,
        },
        {
            "final_response": (
                "Operation interrupted: waiting for model response "
                "(7.8s elapsed)."
            ),
            "completed": False,
        },
    ],
    ids=("structured-flag", "canonical-text-fallback"),
)
async def test_shutdown_interrupt_result_stays_resume_pending(
    tmp_path,
    agent_result,
):
    runner, _adapter = make_restart_runner()
    store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    runner._durable_job_store = store
    runner._durable_job_instance = "shutdown-interrupt-boot"
    runner._scheduled_durable_job_ids = set()
    runner._active_job_ids = {}
    runner._draining = False
    runner._restart_requested = False

    source = make_restart_source(chat_id="shutdown-interrupt-chat")
    session_key = runner._session_key_for_source(source)
    job, _ = store.create_or_get(
        session_key=session_key,
        platform="telegram",
        source=source,
        request_text="Заверши исходную задачу",
        message_id="shutdown-interrupt-1",
    )
    event = MessageEvent(
        text="",
        message_type=MessageType.TEXT,
        source=source,
        internal=True,
        durable_job_id=job["job_id"],
        durable_request_text="Заверши исходную задачу",
        job_execution_key=session_key,
    )
    async def _interrupted_by_shutdown(*_args, **_kwargs):
        # The turn started normally; shutdown began while it was awaiting the
        # model, matching the production drain-timeout sequence.
        runner._draining = True
        return agent_result

    runner._handle_message_with_agent = AsyncMock(
        side_effect=_interrupted_by_shutdown
    )

    response = await runner._handle_message(event)

    stored = store.get(job["job_id"])
    assert isinstance(response, dict)
    assert response["final_response"] == ""
    assert response["already_sent"] is False
    assert stored["status"] == "resume_pending"
    assert stored["resume_reason"] == "shutdown_timeout"
    assert stored["result_text"] is None
    assert stored["completed_at"] is None


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
