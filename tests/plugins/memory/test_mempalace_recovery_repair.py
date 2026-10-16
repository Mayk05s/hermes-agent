"""Source-bound recovery/retry contracts; hermetic, no live model/backend."""
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from plugins.memory.mempalace.fact_pipeline import (
    ExtractionValidationError, FactOutbox, canonical, digest, sealed_admission,
)
from tests.plugins.memory.test_mempalace_fact_pipeline import Client, context, evidence, fact, pipeline, run


def signed(box, item):
    return box.enqueue(item, admission=sealed_admission(box.config_path, item))


def test_signed_revisions_a_b_a_and_delayed_replay(pipeline):
    ctx, remote = context(), Client()
    first = evidence(ctx, "My preferred drink is tea.", 100)
    second = evidence(ctx, "My preferred drink is coffee.", 200)
    third = evidence(ctx, "My preferred drink is tea.", 300)
    jobs = []
    for item in (first, third, second):
        job = signed(pipeline, item)
        jobs.append(job)
        result = run(pipeline, job, ctx, remote, lambda text: [fact(text)])
        assert result["status"] == "done"
    assert jobs[0]["job_id"] == digest([ctx.source_ref, ctx.actor_ref, ctx.write_wing, "normal"])
    assert len({job["job_id"] for job in jobs}) == 3
    assert pipeline.db.execute("SELECT received_at FROM facts").fetchone()[0] == 300
    assert len(remote.pages) == 1 and "tea." in next(iter(remote.pages.values()))
    assert run(pipeline, jobs[2], ctx, remote, lambda _: pytest.fail("repeat inference"))["committed"] is False
    for item, job in zip((first, third, second), jobs):
        assert signed(pipeline, item)["job_id"] == job["job_id"]
    assert pipeline.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 3


def test_revert_to_base_text_is_distinct_revision_and_reuses_output(pipeline):
    ctx, remote = context(), Client()
    for text, stamp in [("My preferred drink is tea.", 100), ("My preferred drink is coffee.", 200)]:
        run(pipeline, signed(pipeline, evidence(ctx, text, stamp)), ctx, remote, lambda value: [fact(value)])
    receipt = signed(pipeline, evidence(ctx, "My preferred drink is tea.", 300))
    assert receipt["revision"] is True
    result = run(pipeline, receipt, ctx, remote, lambda _: pytest.fail("unchanged exact source output is cached"))
    assert result["committed"] and len(remote.pages) == 1
    assert "tea." in next(iter(remote.pages.values()))


def test_same_text_revision_reuses_base_output_finished_after_enqueue(pipeline):
    ctx, remote = context(), Client()
    first = signed(pipeline, evidence(ctx, at=100))
    reaffirmation = signed(pipeline, evidence(ctx, at=200))
    assert pipeline.db.execute("SELECT output FROM jobs WHERE id=?", (reaffirmation["job_id"],)).fetchone()[0] is None
    assert run(pipeline, first, ctx, remote, lambda value: [fact(value)])["committed"]
    assert run(pipeline, reaffirmation, ctx, remote, lambda _: pytest.fail("late available output must be reused"))["committed"]
    assert len(remote.pages) == 1
    assert pipeline.db.execute("SELECT received_at FROM facts").fetchone()[0] == 200


def test_same_text_revision_reuses_sibling_completed_after_enqueue(pipeline):
    ctx, remote = context(), Client()
    first = signed(pipeline, evidence(ctx, at=100))
    second = signed(pipeline, evidence(ctx, "My preferred drink is coffee.", 200))
    third = signed(pipeline, evidence(ctx, "My preferred drink is coffee.", 300))
    run(pipeline, first, ctx, remote, lambda value: [fact(value)])
    run(pipeline, second, ctx, remote, lambda value: [fact(value)])
    assert run(pipeline, third, ctx, remote, lambda _: pytest.fail("late available sibling output must be reused"))["committed"]
    assert len(remote.pages) == 1 and pipeline.db.execute("SELECT received_at FROM facts").fetchone()[0] == 300


def test_historical_alias_output_cannot_resurrect_removed_duplicate(pipeline):
    ctx, remote = context(), Client()
    source = evidence(ctx, at=100)
    first = signed(pipeline, source)
    run(pipeline, first, ctx, remote, lambda text: [fact(text)])
    child = signed(pipeline, evidence(ctx, at=200))
    # Reproduce a historical, separately validated extraction from before the
    # late-cache fix. Its immutable receipt is retained after narrow cleanup.
    alias = [fact(source["text"], "self.drink_preference")]
    pipeline.db.execute("UPDATE jobs SET output=?,output_mac=? WHERE id=?",
                        (canonical(alias).decode(), pipeline.seal({"job": child["job_id"], "output": alias}), child["job_id"]))
    pipeline.db.commit()
    run(pipeline, child, ctx, remote, lambda _: pytest.fail("historical output is sealed"))
    duplicate = pipeline.db.execute("SELECT id,slug FROM facts WHERE fact_key='self.drink_preference'").fetchone()
    del remote.pages[(ctx.write_wing, duplicate["slug"])]
    pipeline.db.execute("DELETE FROM facts WHERE id=?", (duplicate["id"],))
    pipeline.db.commit()
    latest = signed(pipeline, evidence(ctx, at=300))
    assert run(pipeline, latest, ctx, remote, lambda _: pytest.fail("canonical original output must be reused"))["committed"]
    assert len(remote.pages) == 1
    assert [row[0] for row in pipeline.db.execute("SELECT fact_key FROM facts")] == ["self.preferred_drink"]
    assert json.loads(pipeline.db.execute("SELECT output FROM jobs WHERE id=?", (child["job_id"],)).fetchone()[0]) == alias


def test_equal_timestamp_conflict_across_distinct_source_ids_cannot_overwrite(pipeline):
    first, second, remote = context("m_first"), context("m_second"), Client()
    run(pipeline, signed(pipeline, evidence(first, at=100)), first, remote, lambda value: [fact(value)])
    job = signed(pipeline, evidence(second, "My preferred drink is coffee.", 100))
    result = run(pipeline, job, second, remote, lambda value: [fact(value)])
    assert result["status"] == "blocked" and result["code"] == "source_event_timestamp_conflict"
    assert "tea." in next(iter(remote.pages.values())) and remote.calls == 1


@pytest.mark.parametrize("change", ["actor", "source", "scope", "purpose", "seal", "equal_time"])
def test_revision_cannot_replace_immutable_source_or_forge_seal(pipeline, change):
    ctx = context()
    original = evidence(ctx, at=100)
    signed(pipeline, original)
    revised = evidence(ctx, "My preferred drink is coffee.", 200)
    if change == "actor":
        revised["source"]["user_id"] = "654321"
    elif change == "source":
        revised["source"]["chat_id"] = "-9999"
    elif change == "scope":
        revised["context"]["read_wings"].append("private.forbidden")
    elif change == "purpose":
        revised["config_path"] = "/other/config.yaml"
    elif change == "equal_time":
        revised["received_at"] = 100
    admission = sealed_admission(pipeline.config_path, revised)
    if change == "seal":
        admission["mac"] = "0" * 64
    with pytest.raises(ValueError, match="source_event|tampered"):
        pipeline.enqueue(revised, admission=admission)
    assert pipeline.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_revision_receipt_and_cached_output_tamper_fail_closed(pipeline):
    ctx, remote = context(), Client()
    run(pipeline, signed(pipeline, evidence(ctx, at=100)), ctx, remote, lambda value: [fact(value)])
    newer = signed(pipeline, evidence(ctx, at=200))
    pipeline.db.execute("UPDATE admission_revisions SET mac='bad'")
    pipeline.db.commit()
    with pytest.raises(ValueError, match="tampered_revision"):
        signed(pipeline, evidence(ctx, at=300))
    assert pipeline.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2


def inbox_for(box, tmp_path, rows):
    path = tmp_path / "inbox.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE gateway_inbox_events(event_json TEXT,created_at REAL)")
        db.executemany("INSERT INTO gateway_inbox_events VALUES(?,?)", [(row, 1000) for row in rows])
    box.config["inbox_database"] = str(path)


def test_recovery_skips_invalid_seal_conflict_and_json_but_retains_audit(pipeline, tmp_path, monkeypatch):
    ctx = context()
    signed(pipeline, evidence(ctx, at=100))
    conflict = sealed_admission(pipeline.config_path, evidence(ctx, "My favorite drink is coffee.", 100))
    valid = sealed_admission(pipeline.config_path, evidence(context("m_second"), "My favorite season is autumn.", 200))
    tampered = dict(valid, mac="0" * 64)
    inbox_for(pipeline, tmp_path, ["{", json.dumps({"authenticated_memory_admissions": [conflict, tampered, valid]})])
    monkeypatch.setattr(pipeline, "_current_route", lambda _: SimpleNamespace(context=ctx))
    assert pipeline.recover_ingress() == 1
    assert pipeline.db.execute("SELECT cursor FROM recovery_cursor").fetchone()[0] == 2
    assert pipeline.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    records = [json.loads(row[0]) for row in pipeline.db.execute("SELECT receipt FROM recovery_dispositions")]
    assert {row["code"] for row in records} == {"invalid_ingress_metadata", "source_event_timestamp_conflict", "tampered_admission", "admitted"}
    assert not any("text" in row for row in records)
    assert pipeline.recover_ingress() == 0


@pytest.mark.parametrize("field,value", [("source", []), ("source", {"platform": "not_a_platform"}), ("context", []), ("text", None), ("received_at", "yesterday")])
def test_fully_sealed_malformed_packet_gets_disposition_before_route(pipeline, tmp_path, monkeypatch, field, value):
    malformed = evidence(context(), at=100)
    malformed[field] = value
    admission = {"evidence": malformed, "mac": pipeline.seal(malformed), "config": pipeline.config_path}
    inbox_for(pipeline, tmp_path, [json.dumps({"authenticated_memory_admissions": [admission]})])
    monkeypatch.setattr(pipeline, "_current_route", lambda _: pytest.fail("invalid shape must not reach routing"))
    assert pipeline.recover_ingress() == 0
    assert pipeline.db.execute("SELECT cursor FROM recovery_cursor").fetchone()[0] == 1
    record = json.loads(pipeline.db.execute("SELECT receipt FROM recovery_dispositions").fetchone()[0])
    assert record["code"] == "invalid_admission_shape"
    assert pipeline.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("error", [OSError("temporary config read"), sqlite3.OperationalError("database locked"), ValueError("policy_temporarily_invalid")])
def test_transient_route_errors_do_not_advance_cursor(pipeline, tmp_path, monkeypatch, error):
    item = sealed_admission(pipeline.config_path, evidence(context(), at=100))
    inbox_for(pipeline, tmp_path, [json.dumps({"authenticated_memory_admissions": [item]})])
    def failing(_):
        raise error
    monkeypatch.setattr(pipeline, "_current_route", failing)
    with pytest.raises(type(error)):
        pipeline.recover_ingress()
    assert pipeline.db.execute("SELECT count(*) FROM recovery_cursor").fetchone()[0] == 0
    assert pipeline.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_validation_retry_diagnostics_are_sealed_and_bounded(pipeline):
    ctx, remote = context(), Client()
    receipt = signed(pipeline, evidence(ctx))
    for expected in ("retry", "retry", "blocked"):
        result = run(pipeline, receipt, ctx, remote, lambda _: [fact("Unrelated invented assertion.")])
        assert result["status"] == expected and result["committed"] is False
    assert remote.calls == 0
    for row in pipeline.db.execute("SELECT receipt,mac FROM extraction_failures"):
        record = json.loads(row[0])
        assert row[1] == pipeline.seal(record)
        assert record["reason"] == "evidence_not_exact"
        assert "Unrelated" not in row[0]


def test_retry_can_resolve_to_no_facts_but_not_saved(pipeline):
    ctx, remote = context(), Client()
    receipt = signed(pipeline, evidence(ctx))
    assert run(pipeline, receipt, ctx, remote, lambda value: [fact(value, "bad-key")])["status"] == "retry"
    result = run(pipeline, receipt, ctx, remote, lambda _: [])
    assert result["status"] == "done" and result["facts"] == 0 and result["committed"] is False


def test_legacy_repair_exact_signed_plan_preserves_budget_and_is_idempotent(pipeline, monkeypatch):
    ctx = context()
    original = evidence(ctx)
    job = signed(pipeline, original)
    pipeline.db.execute("UPDATE jobs SET state='blocked',attempts=1,error_code='invalid_fact_key'")
    pipeline.db.commit()
    repair = {"kind": "bounded_validation_retry_v1", "config": pipeline.config_path,
              "jobs": [{"job_id": job["job_id"], "evidence_hash": digest(original), "error_code": "invalid_fact_key", "attempts": 1}]}
    plan = {"repair": repair, "mac": pipeline.seal(repair)}
    monkeypatch.setattr(pipeline, "_current_route", lambda _: SimpleNamespace(context=ctx))
    assert pipeline.requeue_validation_jobs(plan)["applied"] is True
    assert pipeline.requeue_validation_jobs(plan)["applied"] is False
    assert pipeline.db.execute("SELECT attempts FROM jobs").fetchone()[0] == 1
    remote = Client()
    assert run(pipeline, job, ctx, remote, lambda value: [fact(value, "bad-key")])["status"] == "retry"
    assert run(pipeline, job, ctx, remote, lambda value: [fact(value, "bad-key")])["status"] == "blocked"
    assert pipeline.db.execute("SELECT count(*) FROM extraction_failures").fetchone()[0] == 3
    assert pipeline.db.execute("SELECT payload FROM jobs").fetchone()[0] == canonical(original).decode()
