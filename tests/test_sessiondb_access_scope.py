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
