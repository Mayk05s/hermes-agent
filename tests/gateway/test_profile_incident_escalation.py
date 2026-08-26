import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.durable_jobs import DurableJobStore
from gateway.platforms.base import SessionSource
from gateway.profile_incidents import automatic_fields, explicit_fields
from gateway.run import GatewayRunner
from tools.gateway_incident_tool import REPORT_SCHEMA

OWNER_ID = "179555559"


def _source(profile="hudeem-tripio"):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="private", user_id="private")
    source.profile_name = profile
    source.chat_type = "group"
    return source


def _runner(tmp_path, *, owners=None, personal_policy=None):
    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(success=True)),
        _pairing_owner_chat_ids=lambda: list(owners or [OWNER_ID]),
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._durable_job_store = DurableJobStore(tmp_path / "gateway_jobs.sqlite3")
    policy = personal_policy or {
        "gateway": {"incident_escalation": {"enabled": True, "owner_chat_id": OWNER_ID}}
    }
    runner._load_profile_config_for_name = lambda profile: policy if profile == "personal" else {}
    return runner, adapter


def _job(runner, profile="hudeem-tripio"):
    job, _ = runner._durable_job_store.create_or_get(
        session_key="safe-session", platform="telegram", source=_source(profile),
        request_text="PRIVATE REQUEST", message_id="1",
    )
    return job


def test_report_schema_has_only_normalized_fields_and_no_destination_or_payload():
    params = REPORT_SCHEMA["parameters"]
    assert params["additionalProperties"] is False
    assert set(params["properties"]) == {"category", "component", "status", "code"}
    assert set(params["required"]) == set(params["properties"])
    assert not ({"destination", "target", "payload", "message", "description"} & set(params["properties"]))


@pytest.mark.asyncio
async def test_explicit_incident_is_durable_deduped_and_owner_message_is_sanitized(tmp_path):
    runner, adapter = _runner(tmp_path)
    job = _job(runner)
    fields = explicit_fields({
        "category": "bug", "component": "tool", "status": "blocked", "code": "E_SAFE-1"
    })

    first = await runner._register_profile_incident(
        source_profile="hudeem-tripio", job_id=job["job_id"], fields=fields
    )
    second = await runner._register_profile_incident(
        source_profile="hudeem-tripio", job_id=job["job_id"], fields=fields
    )

    assert first["incident_id"] == second["incident_id"]
    assert first["delivery_status"] == "delivered"
    assert second["created"] is False
    adapter.send.assert_awaited_once()
    target, text = adapter.send.await_args.args
    assert target == OWNER_ID
    assert job["job_id"] in text
    assert "e_safe-1" in text
    for forbidden in ("PRIVATE REQUEST", "private", "/home/", "line=", "source_json", "result_text"):
        assert forbidden not in text

    with runner._durable_job_store._connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(gateway_profile_incidents)")}
    assert not ({"request_text", "result_text", "source_json", "chat_id", "user_id", "path", "raw_error"} & columns)


@pytest.mark.asyncio
async def test_profile_mismatch_cannot_register_against_foreign_job(tmp_path):
    runner, adapter = _runner(tmp_path)
    job = _job(runner, "hudeem-tripio")
    with pytest.raises(PermissionError):
        await runner._register_profile_incident(
            source_profile="personal", job_id=job["job_id"],
            fields=explicit_fields({"category": "bug", "component": "agent", "status": "failed", "code": "x"}),
        )
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_delivery_is_persisted_and_retry_safe(tmp_path):
    runner, adapter = _runner(tmp_path)
    adapter.send.side_effect = [RuntimeError("PRIVATE RAW ERROR"), SimpleNamespace(success=True)]
    job = _job(runner)
    result = await runner._register_profile_incident(
        source_profile="hudeem-tripio", job_id=job["job_id"],
        fields=explicit_fields({"category": "availability", "component": "delivery", "status": "failed", "code": "adapter_down"}),
    )
    assert result["delivery_status"] == "failed"
    # Make retry due now without changing sanitized payload.
    with runner._durable_job_store._connect() as conn:
        conn.execute("UPDATE gateway_profile_incidents SET next_attempt_at = 0")
    assert await runner._deliver_profile_incident(result["incident_id"])
    row = runner._durable_job_store.profile_incidents_for_job(job["job_id"])[0]
    assert row["delivery_status"] == "delivered"
    assert row["delivery_attempts"] == 2


@pytest.mark.asyncio
async def test_automatic_failure_uses_same_durable_path_without_trace_or_lines(tmp_path):
    runner, adapter = _runner(tmp_path)
    job = _job(runner)
    try:
        raise RuntimeError("/private/path Authorization Bearer secret line=412")
    except RuntimeError as exc:
        assert await runner._notify_profile_incident(_source(), job_id=job["job_id"], exception=exc)
    text = adapter.send.await_args.args[1]
    assert "code: runtimeerror" in text
    assert "line=" not in text
    assert "/private/" not in text
    assert "secret" not in text


def test_owner_lookup_is_exact_sanitized_and_non_owner_denied(tmp_path):
    runner, _ = _runner(tmp_path)
    job = _job(runner)
    runner._durable_job_store.register_profile_incident(
        source_job_id=job["job_id"], source_profile="hudeem-tripio",
        category="bug", component="storage", incident_status="observed",
        code="safe_code", origin="explicit",
    )
    rows = runner._owner_incident_lookup(job_id=job["job_id"], profile="personal", requester=OWNER_ID)
    assert len(rows) == 1
    forbidden = {"request_text", "result_text", "source_json", "session_key", "chat_id", "user_id"}
    assert not (forbidden & set(rows[0]))
    with pytest.raises(PermissionError):
        runner._owner_incident_lookup(job_id=job["job_id"], profile="hudeem-tripio", requester=OWNER_ID)
    with pytest.raises(PermissionError):
        runner._owner_incident_lookup(job_id=job["job_id"], profile="personal", requester="other")


def test_normalizers_never_retain_free_text():
    fields = automatic_fields(agent_result={"failed": True, "error": "HTTP 503 PRIVATE DETAILS"}, retryable_provider_failure=True)
    assert fields["code"] == "provider_unavailable_503"
    assert "PRIVATE" not in json.dumps(fields)
    with pytest.raises(ValueError):
        explicit_fields({"category": "free narrative", "component": "tool", "status": "failed", "code": "x"})
