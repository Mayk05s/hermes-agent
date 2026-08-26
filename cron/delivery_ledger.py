"""Durable idempotency ledger for scheduled cron deliveries.

The cron job store answers *when* a job should run.  This ledger records the
delivery side of one concrete scheduled occurrence (a "slot") so a gateway
restart cannot turn the same occurrence into a second chat message.

There is an unavoidable ambiguity with messaging APIs that do not accept an
idempotency key: a process can die after the remote service accepted a message
but before the local success commit.  We fail closed in that case.  A target
left in ``sending`` becomes ``uncertain`` on recovery and is not sent again.
Only a target with a positively observed error (``failed``) is retryable.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


LEDGER_FILENAME = "delivery_ledger.sqlite3"


def canonical_scheduled_for(value: str) -> str:
    """Canonicalize an ISO timestamp so equivalent instants share a slot."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("scheduled_for is required for a durable delivery slot")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def stable_slot_key(job_id: str, scheduled_for: str) -> str:
    """Return a deterministic key for one job's scheduled occurrence."""
    canonical = canonical_scheduled_for(scheduled_for)
    material = f"{str(job_id)}\0{canonical}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def stable_target_key(target: dict[str, Any]) -> str:
    """Return a deterministic key for one concrete delivery target."""
    material = json.dumps(
        {
            "platform": str(target.get("platform") or "").lower(),
            "chat_id": str(target.get("chat_id") or ""),
            "thread_id": (
                None
                if target.get("thread_id") is None
                else str(target.get("thread_id"))
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _secure_path(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


class DeliveryLedger:
    """Small SQLite outbox scoped to one Hermes cron directory."""

    def __init__(self, cron_dir: Path):
        self.cron_dir = Path(cron_dir)
        self.path = self.cron_dir / LEDGER_FILENAME

    def _connect(self) -> sqlite3.Connection:
        self.cron_dir.mkdir(parents=True, exist_ok=True)
        _secure_path(self.cron_dir, 0o700)
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS delivery_slots (
                slot_key TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                scheduled_for TEXT NOT NULL,
                success INTEGER NOT NULL,
                error TEXT,
                delivery_content TEXT NOT NULL,
                should_deliver INTEGER NOT NULL,
                targets_json TEXT NOT NULL,
                receipts_json TEXT NOT NULL DEFAULT '[]',
                state TEXT NOT NULL DEFAULT 'prepared',
                completion_note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS delivery_targets (
                slot_key TEXT NOT NULL,
                target_key TEXT NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                thread_id TEXT,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (slot_key, target_key),
                FOREIGN KEY (slot_key) REFERENCES delivery_slots(slot_key)
                    ON DELETE CASCADE
            );
            """
        )
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(delivery_slots)")
        }
        if "receipts_json" not in columns:
            connection.execute(
                "ALTER TABLE delivery_slots ADD COLUMN receipts_json TEXT NOT NULL DEFAULT '[]'"
            )
        connection.commit()
        _secure_path(self.path, 0o600)
        return connection

    @staticmethod
    def _slot_from_row(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        result = dict(row)
        result["success"] = bool(result["success"])
        result["should_deliver"] = bool(result["should_deliver"])
        try:
            result["targets"] = json.loads(result.pop("targets_json"))
        except (TypeError, ValueError):
            result["targets"] = []
            result.pop("targets_json", None)
        try:
            result["receipts"] = json.loads(result.pop("receipts_json"))
        except (TypeError, ValueError):
            result["receipts"] = []
            result.pop("receipts_json", None)
        return result

    def get_slot(self, slot_key: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_slots WHERE slot_key = ?", (slot_key,)
            ).fetchone()
        return self._slot_from_row(row)

    def prepare_slot(
        self,
        *,
        slot_key: str,
        job_id: str,
        scheduled_for: str,
        success: bool,
        error: Optional[str],
        delivery_content: str,
        should_deliver: bool,
        targets: Iterable[dict[str, Any]],
        receipts: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """Persist an immutable delivery payload before the first send.

        ``INSERT OR IGNORE`` is deliberate: if a restart races with recovery,
        the first payload prepared for a slot remains the source of truth.
        """
        canonical = canonical_scheduled_for(scheduled_for)
        normalized_targets = [
            {
                "platform": str(target.get("platform") or ""),
                "chat_id": str(target.get("chat_id") or ""),
                "thread_id": (
                    None
                    if target.get("thread_id") is None
                    else str(target.get("thread_id"))
                ),
            }
            for target in targets
        ]
        normalized_receipts = [
            json.loads(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            for receipt in receipts
            if isinstance(receipt, dict)
        ]
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO delivery_slots (
                    slot_key, job_id, scheduled_for, success, error,
                    delivery_content, should_deliver, targets_json,
                    receipts_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                """,
                (
                    slot_key,
                    str(job_id),
                    canonical,
                    int(bool(success)),
                    error,
                    str(delivery_content),
                    int(bool(should_deliver)),
                    json.dumps(normalized_targets, ensure_ascii=False, sort_keys=True),
                    json.dumps(normalized_receipts, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM delivery_slots WHERE slot_key = ?", (slot_key,)
            ).fetchone()
            connection.commit()
        slot = self._slot_from_row(row)
        if slot is None:  # pragma: no cover - defensive against filesystem loss
            raise RuntimeError(f"Failed to prepare cron delivery slot {slot_key}")
        return slot

    def begin_target(self, slot_key: str, target: dict[str, Any]) -> str:
        """Claim a target for sending and return its resulting state.

        ``sending`` from a previous process is not retried: the remote outcome
        is unknowable, so it is atomically converted to ``uncertain``.
        ``failed`` is a positively observed failure and is safe to claim again.
        """
        target_key = stable_target_key(target)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT state FROM delivery_targets
                WHERE slot_key = ? AND target_key = ?
                """,
                (slot_key, target_key),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO delivery_targets (
                        slot_key, target_key, platform, chat_id, thread_id,
                        state, attempts, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'sending', 1, ?)
                    """,
                    (
                        slot_key,
                        target_key,
                        str(target.get("platform") or ""),
                        str(target.get("chat_id") or ""),
                        None
                        if target.get("thread_id") is None
                        else str(target.get("thread_id")),
                        now,
                    ),
                )
                state = "sending"
            elif row["state"] == "failed":
                connection.execute(
                    """
                    UPDATE delivery_targets
                    SET state = 'sending', attempts = attempts + 1,
                        last_error = NULL, updated_at = ?
                    WHERE slot_key = ? AND target_key = ?
                    """,
                    (now, slot_key, target_key),
                )
                state = "sending"
            elif row["state"] == "sending":
                connection.execute(
                    """
                    UPDATE delivery_targets
                    SET state = 'uncertain',
                        last_error = 'previous send was interrupted; duplicate suppressed',
                        updated_at = ?
                    WHERE slot_key = ? AND target_key = ?
                    """,
                    (now, slot_key, target_key),
                )
                state = "uncertain"
            else:
                state = str(row["state"])
            connection.commit()
        return state

    def finish_target(
        self,
        slot_key: str,
        target: dict[str, Any],
        *,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        target_key = stable_target_key(target)
        state = "delivered" if success else "failed"
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE delivery_targets
                SET state = ?, last_error = ?, updated_at = ?
                WHERE slot_key = ? AND target_key = ?
                """,
                (state, None if success else error, now, slot_key, target_key),
            )
            connection.commit()

    def mark_target_uncertain(
        self,
        slot_key: str,
        target: dict[str, Any],
        error: str,
    ) -> None:
        """Record an ambiguous transport outcome that must not be retried."""
        target_key = stable_target_key(target)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE delivery_targets
                SET state = 'uncertain', last_error = ?, updated_at = ?
                WHERE slot_key = ? AND target_key = ?
                """,
                (str(error), now, slot_key, target_key),
            )
            connection.commit()

    def failed_target_errors(self, slot_key: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT platform, chat_id, last_error
                FROM delivery_targets
                WHERE slot_key = ? AND state = 'failed'
                ORDER BY platform, chat_id
                """,
                (slot_key,),
            ).fetchall()
        return [
            str(row["last_error"] or f"delivery to {row['platform']}:{row['chat_id']} failed")
            for row in rows
        ]

    def uncertain_target_notes(self, slot_key: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT platform, chat_id, last_error FROM delivery_targets
                WHERE slot_key = ? AND state = 'uncertain'
                ORDER BY platform, chat_id
                """,
                (slot_key,),
            ).fetchall()
        return [
            f"delivery outcome uncertain for {row['platform']}:{row['chat_id']}; "
            f"duplicate suppressed ({row['last_error'] or 'send interrupted'})"
            for row in rows
        ]

    def mark_slot_completed(self, slot_key: str, note: Optional[str] = None) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE delivery_slots
                SET state = 'completed', completion_note = ?, updated_at = ?
                WHERE slot_key = ?
                """,
                (note, now, slot_key),
            )
            connection.commit()

    def delete_slot(self, slot_key: str) -> None:
        """Remove a slot after jobs.json has durably advanced past it."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM delivery_targets WHERE slot_key = ?", (slot_key,)
            )
            connection.execute(
                "DELETE FROM delivery_slots WHERE slot_key = ?", (slot_key,)
            )
            connection.commit()
