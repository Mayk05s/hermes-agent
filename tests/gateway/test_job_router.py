import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.job_router import decide_job_route


def _llm_response(payload: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
    )


@pytest.mark.asyncio
async def test_semantic_router_attaches_scope_update_to_relevant_job(monkeypatch):
    call = AsyncMock(
        return_value=_llm_response(
            '{"action":"attach","job_id":"gw_menu","confidence":0.98,'
            '"reason":"recipe extends menu deliverable"}'
        )
    )
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", call)

    decision = await decide_job_route(
        message="И не забудь добавить рецепт",
        active_jobs=[
            {"job_id": "gw_report", "request_text": "Проверь отчёт", "status": "running"},
            {"job_id": "gw_menu", "request_text": "Составь меню", "status": "running"},
        ],
    )

    assert decision.action == "attach"
    assert decision.job_id == "gw_menu"


@pytest.mark.asyncio
async def test_semantic_router_keeps_unrelated_work_independent(monkeypatch):
    monkeypatch.setattr(
        "agent.auxiliary_client.async_call_llm",
        AsyncMock(
            return_value=_llm_response(
                '{"action":"new_job","job_id":null,"confidence":0.97,'
                '"reason":"unrelated deliverable"}'
            )
        ),
    )

    decision = await decide_job_route(
        message="А теперь нарисуй логотип для другого проекта",
        active_jobs=[
            {"job_id": "gw_report", "request_text": "Проверь отчёт", "status": "running"}
        ],
    )

    assert decision.action == "new_job"
    assert decision.job_id is None


@pytest.mark.asyncio
async def test_reply_to_active_job_is_deterministic_without_llm(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", call)

    decision = await decide_job_route(
        message="Добавь ещё один вариант",
        active_jobs=[
            {"job_id": "gw_target", "request_text": "Сделай варианты", "status": "running"}
        ],
        replied_job_id="gw_target",
    )

    assert decision.action == "attach"
    assert decision.job_id == "gw_target"
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_contextual_followup_selects_completed_job_as_parent(monkeypatch):
    monkeypatch.setattr(
        "agent.auxiliary_client.async_call_llm",
        AsyncMock(
            return_value=_llm_response(
                '{"action":"new_job","job_id":null,'
                '"parent_job_id":"gw_finished","confidence":0.96,'
                '"reason":"follow-up needs the completed answer context"}'
            )
        ),
    )

    decision = await decide_job_route(
        message="В этом варианте замени второй рецепт",
        active_jobs=[],
        recent_jobs=[
            {
                "job_id": "gw_finished",
                "request_text": "Составь меню с рецептами",
                "status": "completed",
            }
        ],
    )

    assert decision.action == "new_job"
    assert decision.parent_job_id == "gw_finished"


@pytest.mark.asyncio
async def test_new_attachment_never_inherits_completed_job_from_short_caption(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", call)

    decision = await decide_job_route(
        message="@TripiooBot сохрани",
        message_type="photo",
        media_count=1,
        active_jobs=[],
        recent_jobs=[
            {
                "job_id": "gw_old_trip",
                "request_text": "Когда мы прилетаем и улетаем?",
                "result_text": "Прилёт 20 августа, вылет вечером",
                "status": "completed",
            }
        ],
    )

    assert decision.action == "new_job"
    assert decision.parent_job_id is None
    assert decision.reason == "new media attachment is the primary context"
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_with_attachment_can_continue_completed_job(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", call)

    decision = await decide_job_route(
        message="сохрани и это",
        message_type="photo",
        media_count=1,
        active_jobs=[],
        recent_jobs=[{"job_id": "gw_trip", "status": "completed"}],
        replied_job_id="gw_trip",
    )

    assert decision.action == "new_job"
    assert decision.parent_job_id == "gw_trip"
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_job_classifier_receives_attachment_metadata(monkeypatch):
    call = AsyncMock(
        return_value=_llm_response(
            '{"action":"new_job","job_id":null,"confidence":0.95,'
            '"reason":"new screenshot"}'
        )
    )
    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", call)

    await decide_job_route(
        message="вот скрин",
        message_type="photo",
        media_count=1,
        active_jobs=[{"job_id": "gw_active", "status": "running"}],
    )

    payload = json.loads(call.await_args.kwargs["messages"][1]["content"])
    assert payload["message_type"] == "photo"
    assert payload["media_count"] == 1
    assert payload["has_media"] is True


@pytest.mark.asyncio
async def test_fallback_does_not_merge_other_users_by_wording_alone(monkeypatch):
    monkeypatch.setattr(
        "agent.auxiliary_client.async_call_llm",
        AsyncMock(side_effect=RuntimeError("router unavailable")),
    )

    decision = await decide_job_route(
        message="И добавь рецепт",
        active_jobs=[
            {
                "job_id": "gw_alice",
                "request_text": "Составь меню",
                "status": "running",
                "source_json": json.dumps({"user_id": "alice"}),
            }
        ],
        sender_user_id="bob",
    )

    assert decision.action == "new_job"
    assert decision.job_id is None
