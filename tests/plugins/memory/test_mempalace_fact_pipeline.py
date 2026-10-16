import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from gateway.memory_access import MemoryAccessContext, MemoryDestination, MemoryGrant
from gateway.session import SessionSource
from gateway.config import Platform
from plugins.memory.mempalace.fact_pipeline import FactOutbox, detected_secret, evidence_for


@pytest.fixture
def pipeline(tmp_path):
    os.chmod(tmp_path, 0o700)
    key = tmp_path / "seal.key"
    key.write_bytes(b"unit-test-seal-key-only-" * 3)
    os.chmod(key, 0o600)
    config = tmp_path / "worker.json"
    config.write_text(json.dumps({"enabled": True, "state_dir": str(tmp_path), "seal_key_file": str(key)}))
    os.chmod(config, 0o600)
    box = FactOutbox(config)
    yield box
    box.close()


def context(event="m_event1", actor="a_alice", wing="group.telegram.c_group.root.shared"):
    return MemoryAccessContext(1, "group_context:g_one", None, actor, event,
        MemoryDestination("group", "telegram", "c_group", "root"), "test", "shared", (wing,), wing, (), "sha256:test")


def evidence(ctx, text="My preferred drink is tea.", at=1):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-12345", chat_type="group", user_id="123456", message_id=ctx.source_ref, profile_name="test")
    return evidence_for(ctx, source, text, received_at=at, config_path="/test/config.yaml")


def fact(text, key="self.preferred_drink", action="set"):
    return {"text": text, "evidence": text, "key": key, "action": action}


class Client:
    def __init__(self, fail_after_write=False, malformed=False):
        self.pages = {}
        self.calls = 0
        self.fail = fail_after_write
        self.malformed = malformed

    def mcp_call(self, ctx, name, args, operation):
        assert name == "put_page" and operation == "write"
        self.calls += 1
        self.pages[(ctx.write_wing, args["slug"])] = args["content"]
        if self.fail:
            self.fail = False
            raise ConnectionError("synthetic lost response")
        obj = {"ok": True, "slug": args["slug"]} if not self.malformed else {"ok": False}
        return {"content": [{"type": "text", "text": json.dumps(obj)}]}


def run(box, receipt, ctx, client, extractor):
    return box.process_one(receipt["job_id"], extractor=extractor, route_resolver=lambda _: SimpleNamespace(context=ctx), client_factory=lambda _: client)


@pytest.mark.parametrize("text", ["мой пароль Abc12345", "пароль от почты Abc12345", "password: Abc12345", "мой API key это sk-proj-abcdefghijklmnopqrstuvwxyz", "-----BEGIN PRIVATE KEY-----"])
def test_credentials_rejected_before_queue_or_extractor(pipeline, text):
    assert detected_secret(text)
    with pytest.raises(ValueError, match="credential_source"):
        pipeline.enqueue(evidence(context(), text))
    assert pipeline.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_harmless_codeword_is_not_credential():
    assert not detected_secret("Запомни: кодовое слово синий мая.")


def test_original_only_sealed_retry_after_remote_commit_and_reopen(pipeline):
    ctx = context()
    item = evidence(ctx)
    receipt = pipeline.enqueue(item)
    client = Client(fail_after_write=True)
    seen = []
    def extract(text):
        seen.append(text)
        return [fact(text)]
    assert run(pipeline, receipt, ctx, client, extract)["status"] == "retry"
    other = FactOutbox(pipeline.config_path)
    try:
        result = run(other, receipt, ctx, client, lambda _: pytest.fail("must reuse sealed extraction"))
        assert result["committed"] is True
        assert len(client.pages) == 1
        assert seen == [item["text"]]
    finally:
        other.close()


def test_replay_event_conflict_and_exact_dedup(pipeline):
    ctx = context()
    receipt = pipeline.enqueue(evidence(ctx))
    assert pipeline.enqueue(evidence(ctx, at=2))["job_id"] == receipt["job_id"]
    with pytest.raises(ValueError, match="source_event_conflict"):
        pipeline.enqueue(evidence(ctx, text="My preferred drink is coffee."))


def test_no_facts_is_processed_not_saved(pipeline):
    ctx = context()
    receipt = pipeline.enqueue(evidence(ctx, text="Can you tell me the time?"))
    client = Client()
    result = run(pipeline, receipt, ctx, client, lambda _: [])
    assert result["status"] == "done" and result["committed"] is False and client.calls == 0


def test_malformed_remote_ack_does_not_commit(pipeline):
    ctx = context()
    receipt = pipeline.enqueue(evidence(ctx))
    result = run(pipeline, receipt, ctx, Client(malformed=True), lambda text: [fact(text)])
    assert result["committed"] is False
    assert pipeline.db.execute("SELECT count(*) FROM facts").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["payload", "output", "result"])
def test_tampered_sealed_job_never_acknowledged(pipeline, field):
    ctx = context()
    receipt = pipeline.enqueue(evidence(ctx))
    client = Client()
    if field == "result":
        assert run(pipeline, receipt, ctx, client, lambda text: [fact(text)])["committed"]
    elif field == "output":
        client.fail = True
        run(pipeline, receipt, ctx, client, lambda text: [fact(text)])
    pipeline.db.execute(f"UPDATE jobs SET {field}=? WHERE id=?", (json.dumps({"tampered": True}), receipt["job_id"]))
    pipeline.db.commit()
    result = run(pipeline, receipt, ctx, client, lambda _: pytest.fail("tampered job must not extract"))
    assert result["committed"] is False
    assert result["status"] == "blocked"


def test_current_revocation_checked_before_extraction_and_write(pipeline):
    ctx = context()
    receipt = pipeline.enqueue(evidence(ctx))
    def revoked(_):
        raise ValueError("revoked_memory_route")
    result = pipeline.process_one(receipt["job_id"], route_resolver=revoked, extractor=lambda _: pytest.fail("revoked"))
    assert result["status"] == "blocked"


def test_actor_wing_subject_separation_and_correction(pipeline):
    client = Client()
    for index, (actor, wing, key, value) in enumerate([
        ("a_alice", "group.telegram.c_one.root.shared", "alice.favorite_color", "Alice likes blue."),
        ("a_alice", "group.telegram.c_one.root.shared", "bob.favorite_color", "Bob likes red."),
        ("a_bob", "group.telegram.c_one.root.shared", "alice.favorite_color", "Alice likes pink."),
        ("a_alice", "group.telegram.c_two.root.shared", "alice.favorite_color", "Alice likes yellow."),
        ("a_alice", "group.telegram.c_one.root.shared", "alice.favorite_color", "Correction: Alice now likes green."),
    ], 1):
        ctx = context(f"m_{index}", actor, wing)
        receipt = pipeline.enqueue(evidence(ctx, value, at=index))
        assert run(pipeline, receipt, ctx, client, lambda _, v=value, k=key: [fact(v, k)])["committed"]
    assert len(client.pages) == 4
    values = list(client.pages.values())
    assert not any("likes blue" in v for v in values)
    assert any("likes green" in v for v in values)
    assert any("Bob likes red" in v for v in values)


def test_private_disclosure_cannot_be_queued(pipeline):
    ctx = context()
    ctx = replace(ctx, write_wing=None, grants=(MemoryGrant("m_event1", "disclose_private_to_group"),))
    with pytest.raises(ValueError):
        evidence(ctx)


def test_tampered_ledger_does_not_suppress_write(pipeline):
    ctx = context()
    client = Client()
    first = pipeline.enqueue(evidence(ctx))
    run(pipeline, first, ctx, client, lambda text: [fact(text)])
    pipeline.db.execute("UPDATE facts SET received_at=9999")
    pipeline.db.commit()
    new = context("m_event2")
    receipt = pipeline.enqueue(evidence(new, "I now prefer coffee.", 2))
    result = run(pipeline, receipt, new, client, lambda text: [fact(text)])
    assert result["status"] == "blocked" and not result["committed"]
