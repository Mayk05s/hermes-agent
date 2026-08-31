import json
import sqlite3
from pathlib import Path

import pytest

from scripts.import_profile_chat_history import ImportConflict, import_history


def _make_db(path: Path) -> None:
    from hermes_state import SessionDB

    SessionDB(db_path=path).close()


def _seed(db_path: Path, *, session_id: str, chat_id: str, profile: str, text: str) -> None:
    access_scope = json.dumps(
        {
            "session_key": (
                f"agent:main:profile:{profile}:scope:{profile}:"
                f"telegram:group:{chat_id}"
            ),
            "origin": {
                "platform": "telegram",
                "chat_id": chat_id,
                "profile_name": profile,
                "scope_name": profile,
                "memory_scope": profile,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, access_scope, message_count) "
        "VALUES (?, 'telegram', 123.0, ?, 1)",
        (session_id, access_scope),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, observed, active) "
        "VALUES (?, 'user', ?, 124.0, 1, 1)",
        (session_id, text),
    )
    conn.commit()
    conn.close()


def test_import_is_exact_scoped_transactional_and_idempotent(tmp_path):
    source = tmp_path / "root.db"
    profile_home = tmp_path / "profiles" / "hudeem-tripio"
    profile_home.mkdir(parents=True)
    destination = profile_home / "state.db"
    _make_db(source)
    _make_db(destination)
    _seed(source, session_id="wanted", chat_id="-5526305849", profile="hudeem-tripio", text="ok")
    _seed(source, session_id="wrong-chat", chat_id="-1", profile="hudeem-tripio", text="no")
    _seed(source, session_id="wrong-profile", chat_id="-5526305849", profile="personal", text="no")

    preview = import_history(
        source_db=source,
        profile_home=profile_home,
        platform="telegram",
        chat_id="-5526305849",
        profile="hudeem-tripio",
        apply=False,
    )
    assert preview == {"matched_sessions": 1, "matched_messages": 1, "imported_sessions": 0, "imported_messages": 0, "skipped_sessions": 0}

    result = import_history(
        source_db=source,
        profile_home=profile_home,
        platform="telegram",
        chat_id="-5526305849",
        profile="hudeem-tripio",
        apply=True,
    )
    assert result["imported_sessions"] == 1
    assert result["imported_messages"] == 1

    again = import_history(
        source_db=source,
        profile_home=profile_home,
        platform="telegram",
        chat_id="-5526305849",
        profile="hudeem-tripio",
        apply=True,
    )
    assert again["imported_sessions"] == 0
    assert again["skipped_sessions"] == 1

    conn = sqlite3.connect(destination)
    assert conn.execute("SELECT group_concat(id) FROM sessions").fetchone()[0] == "wanted"
    assert conn.execute("SELECT content FROM messages").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_existing_nonidentical_session_fails_closed_without_partial_import(tmp_path):
    source = tmp_path / "root.db"
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    destination = profile_home / "state.db"
    _make_db(source)
    _make_db(destination)
    _seed(source, session_id="conflict", chat_id="-5526305849", profile="hudeem-tripio", text="source")
    _seed(source, session_id="new", chat_id="-5526305849", profile="hudeem-tripio", text="new")
    _seed(destination, session_id="conflict", chat_id="-5526305849", profile="hudeem-tripio", text="different")

    with pytest.raises(ImportConflict):
        import_history(
            source_db=source,
            profile_home=profile_home,
            platform="telegram",
            chat_id="-5526305849",
            profile="hudeem-tripio",
            apply=True,
        )

    conn = sqlite3.connect(destination)
    assert conn.execute("SELECT count(*) FROM sessions WHERE id='new'").fetchone()[0] == 0
    conn.close()


def test_destination_must_be_profile_home_state_db(tmp_path):
    source = tmp_path / "source.db"
    _make_db(source)
    with pytest.raises(ValueError, match="profile home"):
        import_history(
            source_db=source,
            profile_home=tmp_path / "missing-profile-home",
            platform="telegram",
            chat_id="-1",
            profile="p",
            apply=True,
        )
