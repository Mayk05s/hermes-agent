#!/usr/bin/env python3
"""Safely copy one chat's access-scoped sessions into a profile state.db.

The command is dry-run by default. It selects sessions only when the JSON
``access_scope.origin`` exactly matches platform, chat_id and profile_name.
Writes are one ``BEGIN IMMEDIATE`` transaction. Existing identical sessions
are skipped; an existing non-identical session aborts the whole import.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote


class ImportConflict(RuntimeError):
    """Destination already contains a different session with the same id."""


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _scope_matches(raw: Any, *, platform: str, chat_id: str, profile: str) -> bool:
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False
    origin = payload.get("origin") if isinstance(payload, dict) else None
    if not isinstance(origin, dict):
        return False
    return (
        str(origin.get("platform") or "").strip().lower() == platform
        and str(origin.get("chat_id") or "").strip() == chat_id
        and str(origin.get("profile_name") or "").strip() == profile
    )


def _comparable_messages(conn: sqlite3.Connection, session_id: str) -> list[tuple[Any, ...]]:
    columns = [name for name in _columns(conn, "messages") if name != "id"]
    sql = f"SELECT {', '.join(columns)} FROM messages WHERE session_id = ? ORDER BY id"
    return [tuple(row[name] for name in columns) for row in conn.execute(sql, (session_id,))]


def _session_is_identical(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    session_id: str,
) -> bool:
    src_columns = _columns(source, "sessions")
    dst_columns = set(_columns(destination, "sessions"))
    common = [name for name in src_columns if name in dst_columns]
    select = ", ".join(common)
    src = source.execute(f"SELECT {select} FROM sessions WHERE id = ?", (session_id,)).fetchone()
    dst = destination.execute(f"SELECT {select} FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if src is None or dst is None or tuple(src[name] for name in common) != tuple(dst[name] for name in common):
        return False
    return _comparable_messages(source, session_id) == _comparable_messages(destination, session_id)


def import_history(
    *,
    source_db: Path,
    profile_home: Path,
    platform: str,
    chat_id: str,
    profile: str,
    apply: bool = False,
) -> dict[str, int]:
    """Import exactly matched sessions, or return a dry-run summary."""
    source_db = Path(source_db).expanduser().resolve(strict=True)
    try:
        profile_home = Path(profile_home).expanduser().resolve(strict=True)
        destination_db = (profile_home / "state.db").resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("profile home and its state.db must already exist") from exc
    if destination_db.parent != profile_home:
        raise ValueError("destination must be profile home's state.db")
    if source_db == destination_db:
        raise ValueError("source and destination databases must differ")
    platform = str(platform).strip().lower()
    chat_id = str(chat_id).strip()
    profile = str(profile).strip()
    if not platform or not chat_id or not profile:
        raise ValueError("platform, chat_id and profile are required")

    source = _connect_readonly(source_db)
    destination = sqlite3.connect(destination_db, timeout=30)
    destination.row_factory = sqlite3.Row
    try:
        source_sessions = [
            row
            for row in source.execute("SELECT * FROM sessions ORDER BY started_at, id")
            if _scope_matches(
                row["access_scope"], platform=platform, chat_id=chat_id, profile=profile
            )
        ]
        matched_messages = sum(
            int(
                source.execute(
                    "SELECT count(*) FROM messages WHERE session_id = ?", (row["id"],)
                ).fetchone()[0]
            )
            for row in source_sessions
        )
        summary = {
            "matched_sessions": len(source_sessions),
            "matched_messages": matched_messages,
            "imported_sessions": 0,
            "imported_messages": 0,
            "skipped_sessions": 0,
        }
        if not apply:
            return summary

        destination.execute("BEGIN IMMEDIATE")
        try:
            src_session_columns = _columns(source, "sessions")
            dst_session_columns = set(_columns(destination, "sessions"))
            session_columns = [name for name in src_session_columns if name in dst_session_columns]
            src_message_columns = _columns(source, "messages")
            dst_message_columns = set(_columns(destination, "messages"))
            message_columns = [
                name for name in src_message_columns if name != "id" and name in dst_message_columns
            ]

            for session in source_sessions:
                session_id = str(session["id"])
                exists = destination.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if exists:
                    if not _session_is_identical(source, destination, session_id):
                        raise ImportConflict(
                            f"session {session_id!r} already exists with different data"
                        )
                    summary["skipped_sessions"] += 1
                    continue

                placeholders = ", ".join("?" for _ in session_columns)
                destination.execute(
                    f"INSERT INTO sessions ({', '.join(session_columns)}) VALUES ({placeholders})",
                    tuple(session[name] for name in session_columns),
                )
                message_placeholders = ", ".join("?" for _ in message_columns)
                message_sql = (
                    f"INSERT INTO messages ({', '.join(message_columns)}) "
                    f"VALUES ({message_placeholders})"
                )
                messages = source.execute(
                    "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
                ).fetchall()
                destination.executemany(
                    message_sql,
                    [tuple(message[name] for name in message_columns) for message in messages],
                )
                summary["imported_sessions"] += 1
                summary["imported_messages"] += len(messages)

            integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"destination integrity_check failed: {integrity}")
            destination.commit()
        except BaseException:
            destination.rollback()
            raise
        return summary
    finally:
        source.close()
        destination.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--profile-home", required=True, type=Path)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--apply", action="store_true", help="commit the import (default: dry-run)")
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = import_history(
        source_db=args.source_db,
        profile_home=args.profile_home,
        platform=args.platform,
        chat_id=args.chat_id,
        profile=args.profile,
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
