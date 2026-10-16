"""Legacy refresh must not consume migrated or ambiguous scoped traffic."""
import json
import sqlite3

import pytest

from hermes_cli import mempalace


def _policy(mode="default_dm_only", routes=()):
    return {"mode": mode, "routes": list(routes)}


def _session(**changes):
    origin = {
        "platform": "telegram", "profile_name": "default", "chat_type": "dm",
        "chat_id": "123456", "user_id": "123456", "thread_id": "",
        "memory_scope": "default",
    }
    origin.update(changes.pop("origin", {}))
    row = {
        "id": "dm_session", "source": "telegram", "user_id": None,
        "parent_session_id": None, "access_scope": json.dumps({"origin": origin}),
    }
    row.update(changes)
    return row


def _configure(home, mode, routes=()):
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(json.dumps({
        "memory": {"legacy_extraction": {"mode": mode}, "trusted_provider_routes": list(routes)},
    }))


def test_retired_refresh_never_opens_legacy_sources_even_forced(tmp_path, monkeypatch):
    _configure(tmp_path, "retired")
    def forbidden(*args, **kwargs):
        pytest.fail("retired extraction opened a legacy source or model")
    for name in ("_markdown_sources", "_acquire_refresh_lock", "_llm_event_model_fields", "_connect_state_readonly"):
        monkeypatch.setattr(mempalace, name, forbidden)
    refresh = mempalace.refresh_if_due(profile="personal", profile_home=tmp_path, force=True)
    consolidate = mempalace.consolidate_profile(profile="personal", profile_home=tmp_path, force=True)
    assert refresh["reason"] == consolidate["reason"] == "migrated_to_scoped_memory"
    assert refresh["refreshed"] is False
    assert consolidate["processed_messages"] == 0


def test_unconfigured_installation_retains_legacy_mode(tmp_path):
    assert mempalace._legacy_refresh_policy(tmp_path)["mode"] == "legacy"
    assert mempalace._legacy_session_allowed(_session(access_scope=""), "default", {}, None)


def test_retired_status_marks_preserved_error_as_historical(tmp_path, monkeypatch):
    _configure(tmp_path, "retired")
    monkeypatch.setattr(mempalace, "_read_refresh_state", lambda *args: {
        "consolidator": {"auto_enabled": True, "phase": "error", "last_error": "old unsupported model"},
    })
    monkeypatch.setattr(mempalace, "_profile_route_lookup", lambda: ("default", {}))
    status = mempalace.consolidator_status(profile_home=tmp_path, state_db_path=tmp_path / "absent.db")
    assert status["auto_enabled"] is False
    assert status["phase"] == "retired"
    assert status["legacy_error_is_historical"] is True
    assert status["last_error"] == "old unsupported model"


def test_invalid_mode_fails_before_legacy_work(tmp_path):
    _configure(tmp_path, "unknown")
    with pytest.raises(ValueError, match="invalid_legacy_extraction_mode"):
        mempalace.refresh_if_due(profile_home=tmp_path, force=True)


@pytest.mark.parametrize("field,value", [
    ("legacy_extraction", False), ("legacy_extraction", []), ("legacy_extraction", ""),
    ("trusted_provider_routes", False), ("trusted_provider_routes", ""),
    ("trusted_provider_route", False),
])
def test_falsy_malformed_policy_does_not_reenable_legacy(tmp_path, field, value):
    (tmp_path / "config.yaml").write_text(json.dumps({"memory": {field: value}}))
    with pytest.raises(ValueError, match="invalid_legacy_extraction"):
        mempalace._legacy_refresh_policy(tmp_path)


def test_false_root_configuration_is_not_legacy_compatibility(tmp_path):
    (tmp_path / "config.yaml").write_text("false\n")
    with pytest.raises(ValueError, match="invalid_legacy_extraction_config"):
        mempalace._legacy_refresh_policy(tmp_path)


def test_explicit_root_dm_retains_compatibility():
    assert mempalace._legacy_session_allowed(_session(), "default", {}, _policy())


@pytest.mark.parametrize("changes", [
    {"access_scope": ""},
    {"parent_session_id": "parent"},
    {"source": "subagent"},
    {"origin": {"chat_type": "group", "chat_id": "-100123"}},
    {"origin": {"chat_type": "dm", "chat_id": "-100123", "user_id": "-100123"}},
    {"origin": {"profile_name": "personal"}},
    {"origin": {"profile_name": ""}},
    {"origin": {"user_id": ""}},
    {"origin": {"chat_id": "654321"}},
    {"origin": {"thread_id": "12"}},
    {"origin": {"memory_scope": "hudeem-tripio-user-123456"}},
    {"origin": {"memory_scope": "nutrition"}},
])
def test_unproven_or_migrated_session_is_excluded(changes):
    assert not mempalace._legacy_session_allowed(_session(**changes), "default", {}, _policy())


def test_conflicting_session_index_cannot_relabel_group_as_dm():
    row = _session(origin={"chat_type": "group", "chat_id": "-100123"})
    indexed = {row["id"]: {"chat_type": "dm", "chat_id": "123456", "profile_name": "default"}}
    assert not mempalace._legacy_session_allowed(row, "default", indexed, _policy())


def test_enabled_scoped_dm_route_excludes_legacy_double_write():
    route = {"enabled": True, "match": {
        "platform": "telegram", "chat_id": "123456", "user_id": "123456",
        "profile_name": "default", "chat_type": "dm", "thread_id": "",
    }}
    assert not mempalace._legacy_session_allowed(_session(), "default", {}, _policy(routes=[route]))
    route["enabled"] = False
    assert mempalace._legacy_session_allowed(_session(), "default", {}, _policy(routes=[route]))


@pytest.mark.parametrize("message", [
    {"role": "assistant", "content": "The old assistant claimed a fact.", "tool_name": None},
    {"role": "user", "content": "A tool result", "tool_name": "delegate_task"},
    {"role": "user", "content": "<memory-context>private recalled fact</memory-context>", "tool_name": None},
    {"role": "user", "content": "[System note: recalled private context]", "tool_name": None},
    {"role": "user", "content": "[Replying to: another participant]\nquery", "tool_name": None},
    {"role": "user", "content": "мой пароль от почты imaginary-secret", "tool_name": None},
    {"role": "user", "content": "токен доступа imaginary-token", "tool_name": None},
])
def test_internal_or_assistant_content_is_not_legacy_fact_source(message):
    assert not mempalace._legacy_message_allowed(message, _policy())


def test_default_mode_extractor_receives_only_proved_dm_user_input(tmp_path, monkeypatch):
    _configure(tmp_path, "default_dm_only")
    db_path = tmp_path / "state.db"
    rows = [
        _session(),
        _session(id="group", origin={"chat_type": "group", "chat_id": "-100123"}),
        _session(id="specialist", parent_session_id="dm_session", access_scope=""),
        _session(id="unknown", access_scope=""),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,user_id TEXT,access_scope TEXT,title TEXT,started_at REAL,ended_at REAL,message_count INTEGER,cwd TEXT,parent_session_id TEXT)")
        conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,tool_name TEXT,timestamp REAL,active INTEGER)")
        for number, row in enumerate(rows, 1):
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?)", (
                row["id"], row["source"], row["user_id"], row["access_scope"], "", 1., None, 2, "", row["parent_session_id"],
            ))
            conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)", (number, row["id"], "user", f"assertion-{row['id']}", None, 1., 1))
        conn.execute("INSERT INTO messages VALUES(5,'dm_session','assistant','assistant-private-content',NULL,1,1)")
        conn.execute("INSERT INTO messages VALUES(6,'dm_session','user','<memory-context>injected</memory-context>',NULL,1,1)")
    received = []
    monkeypatch.setattr(mempalace, "_profile_route_lookup", lambda: ("default", {}))
    monkeypatch.setattr(mempalace, "_llm_event_model_fields", lambda *args: {})
    def extract(**kwargs):
        received.extend(kwargs["batch_messages"])
        return {"entities": [], "facts": [], "relations": [], "contradictions": []}
    monkeypatch.setattr(mempalace, "_extract_llm_payload", extract)
    result = mempalace.consolidate_profile(
        profile_home=tmp_path, state_db_path=db_path, dry_run=True,
        force=True, backup=False, auto_clean=False, workers=1,
    )
    assert [item["content"] for item in received] == ["assertion-dm_session"]
    assert result["processed_messages"] == 1


def test_default_compatibility_refresh_does_not_scan_markdown(tmp_path, monkeypatch):
    _configure(tmp_path, "default_dm_only")
    monkeypatch.setattr(mempalace, "_markdown_sources", lambda *args: pytest.fail("unscoped markdown opened"))
    monkeypatch.setattr(mempalace, "consolidate_profile", lambda **kwargs: {"processed_messages": 0})
    result = mempalace.refresh_if_due(profile_home=tmp_path, force=True)
    assert result["files"] == 0
    assert result["markdown"] is None


def test_legacy_llm_call_pins_no_tools_and_no_fallback(monkeypatch):
    from agent import auxiliary_client
    captured = {}
    def call(**kwargs):
        captured.update(kwargs)
        return "{}"
    monkeypatch.setattr(auxiliary_client, "call_llm", call)
    assert mempalace._call_history_llm([{"role": "user", "content": "fictional fact"}]) == "{}"
    assert captured["allow_fallback"] is False
    assert captured["no_tools"] is True
