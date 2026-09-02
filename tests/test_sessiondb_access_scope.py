"""SessionDB access-scope filtering tests."""

import sqlite3

from hermes_state import SCHEMA_SQL, SessionDB


ALICE_SCOPE = "agent:main:telegram:dm:alice"
BOB_SCOPE = "agent:main:telegram:dm:bob"


def test_sessiondb_scope_filter_limits_browse_search_and_scroll(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("alice", "telegram", access_scope=ALICE_SCOPE)
        alice_msg = db.append_message("alice", "user", "shared deployment keyword 秘密项目")
        db.create_session("bob", "telegram", access_scope=BOB_SCOPE)
        bob_msg = db.append_message("bob", "user", "shared deployment keyword secret 秘密项目")

        browsed = db.list_sessions_rich(scope_filter=ALICE_SCOPE)
        assert [s["id"] for s in browsed] == ["alice"]

        hits = db.search_messages("deployment", scope_filter=ALICE_SCOPE)
        assert {h["session_id"] for h in hits} == {"alice"}

        trigram_hits = db.search_messages("秘密项目", scope_filter=ALICE_SCOPE)
        assert {h["session_id"] for h in trigram_hits} == {"alice"}

        like_hits = db.search_messages("秘密", scope_filter=ALICE_SCOPE)
        assert {h["session_id"] for h in like_hits} == {"alice"}

        assert db.session_matches_scope("alice", ALICE_SCOPE) is True
        assert db.session_matches_scope("bob", ALICE_SCOPE) is False
        assert db.message_matches_scope(alice_msg, ALICE_SCOPE) is True
        assert db.message_matches_scope(bob_msg, ALICE_SCOPE) is False

        alice_window = db.get_messages_around(
            "alice", alice_msg, scope_filter=ALICE_SCOPE
        )
        assert [m["id"] for m in alice_window["window"]] == [alice_msg]
        assert "secret" not in str(alice_window)

        denied_window = db.get_messages_around(
            "bob", bob_msg, scope_filter=ALICE_SCOPE
        )
        assert denied_window == {
            "window": [],
            "messages_before": 0,
            "messages_after": 0,
        }

        denied_view = db.get_anchored_view(
            "bob", bob_msg, scope_filter=ALICE_SCOPE
        )
        assert denied_view == {
            "window": [],
            "messages_before": 0,
            "messages_after": 0,
            "bookend_start": [],
            "bookend_end": [],
        }
        assert "secret" not in str(denied_view)
    finally:
        db.close()


def test_sessiondb_reconciles_access_scope_column_and_index_on_legacy_db(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        legacy_schema = SCHEMA_SQL.replace("    access_scope TEXT,\n", "")
        conn.executescript(legacy_schema)
        conn.execute("UPDATE schema_version SET version = 1")
        conn.commit()
    finally:
        conn.close()

    db = SessionDB(db_path=db_path)
    try:
        db.create_session("alice", "telegram", access_scope=ALICE_SCOPE)
        assert db._conn is not None
        columns = {row[1] for row in db._conn.execute("PRAGMA table_info(sessions)").fetchall()}
        indexes = {row[1] for row in db._conn.execute("PRAGMA index_list(sessions)").fetchall()}
        assert "access_scope" in columns
        assert "idx_sessions_access_scope" in indexes
        assert db.session_matches_scope("alice", ALICE_SCOPE) is True
    finally:
        db.close()


def test_compression_projection_cannot_cross_access_scope(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("alice-root", "telegram", access_scope=ALICE_SCOPE)
        db.append_message("alice-root", "user", "alice root")
        db.end_session("alice-root", "compression")
        root = db.get_session("alice-root")

        db.create_session(
            "bob-child",
            "telegram",
            parent_session_id="alice-root",
            access_scope=BOB_SCOPE,
        )
        assert db._conn is not None
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (root["ended_at"], "bob-child"),
        )
        db._conn.commit()
        db.append_message("bob-child", "user", "bob private canary")

        sessions = db.list_sessions_rich(scope_filter=ALICE_SCOPE)
        assert [row["id"] for row in sessions] == ["alice-root"]
        assert "bob private canary" not in str(sessions)
    finally:
        db.close()
