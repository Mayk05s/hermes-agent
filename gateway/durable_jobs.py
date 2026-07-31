"""Durable lifecycle registry for user-initiated gateway work.

The session store answers "which conversation is this?", but it cannot answer
"did the turn finish and was its final result delivered?".  Gateway restarts
need both answers.  This module keeps a small SQLite registry that is committed
before work starts and updated at the execution and delivery boundaries.

The delivery guarantee is intentionally at-least-once.  If the process dies
after a platform accepted a message but before ``delivered_at`` is committed,
the next gateway may re-deliver that result.  Losing a completed result is the
worse failure mode, and messaging APIs generally do not offer an idempotency
key that spans process restarts.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


ACTIVE_STATUSES = frozenset({"pending", "running", "resume_pending"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class DurableJobStore:
    """SQLite-backed gateway job registry.

    Connections are deliberately short-lived.  Gateway callbacks can arrive
    from different async tasks (and some tests exercise the store from worker
    threads), so a process-local lock plus SQLite WAL gives simple, durable
    serialization without sharing connection objects across threads.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS gateway_jobs (
                    job_id TEXT PRIMARY KEY,
                    dedupe_key TEXT UNIQUE,
                    session_key TEXT NOT NULL,
                    session_id TEXT,
                    platform TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    request_text TEXT NOT NULL,
                    request_message_id TEXT,
                    status TEXT NOT NULL,
                    result_text TEXT,
                    error_text TEXT,
                    resume_reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    delivered_at REAL,
                    delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    resume_attempts INTEGER NOT NULL DEFAULT 0,
                    last_delivery_error TEXT,
                    owner_instance TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_gateway_jobs_status
                    ON gateway_jobs(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_gateway_jobs_session
                    ON gateway_jobs(session_key, status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_gateway_jobs_delivery
                    ON gateway_jobs(delivered_at, status, completed_at);
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None

    @staticmethod
    def _source_json(source: Any) -> str:
        if source is None:
            payload: Dict[str, Any] = {}
        elif isinstance(source, dict):
            payload = source
        elif hasattr(source, "to_dict"):
            payload = source.to_dict()
        else:
            payload = {
                key: value
                for key, value in vars(source).items()
                if isinstance(value, (str, int, float, bool, type(None)))
            }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def source_dict(job: Dict[str, Any]) -> Dict[str, Any]:
        try:
            value = json.loads(str(job.get("source_json") or "{}"))
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    @staticmethod
    def make_dedupe_key(
        *,
        platform: str,
        session_key: str,
        message_id: Optional[str] = None,
        platform_update_id: Optional[int] = None,
    ) -> Optional[str]:
        if platform_update_id is not None:
            return f"{platform}:update:{platform_update_id}"
        if message_id:
            return f"{platform}:session:{session_key}:message:{message_id}"
        return None

    def create_or_get(
        self,
        *,
        session_key: str,
        platform: str,
        source: Any,
        request_text: str,
        message_id: Optional[str] = None,
        platform_update_id: Optional[int] = None,
    ) -> tuple[Dict[str, Any], bool]:
        """Create a pending job, or return the existing inbound-message job."""
        now = time.time()
        job_id = f"gw_{uuid.uuid4().hex[:16]}"
        dedupe_key = self.make_dedupe_key(
            platform=platform,
            session_key=session_key,
            message_id=message_id,
            platform_update_id=platform_update_id,
        )
        source_json = self._source_json(source)

        with self._lock, self._connect() as conn:
            created = True
            try:
                conn.execute(
                    """
                    INSERT INTO gateway_jobs (
                        job_id, dedupe_key, session_key, platform, source_json,
                        request_text, request_message_id, status, created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        job_id,
                        dedupe_key,
                        session_key,
                        platform,
                        source_json,
                        str(request_text or ""),
                        str(message_id) if message_id is not None else None,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                if not dedupe_key:
                    raise
                created = False

            if created:
                row = conn.execute(
                    "SELECT * FROM gateway_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM gateway_jobs WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()

        job = self._row(row)
        if job is None:
            raise RuntimeError("Durable gateway job insert returned no row")
        return job, created

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            return self._row(
                conn.execute(
                    "SELECT * FROM gateway_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
            )

    def bind_session(
        self,
        job_id: str,
        *,
        session_key: str,
        session_id: str,
        source: Any,
    ) -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE gateway_jobs
                   SET session_key = ?, session_id = ?, source_json = ?,
                       updated_at = ?
                 WHERE job_id = ?
                """,
                (
                    session_key,
                    session_id,
                    self._source_json(source),
                    now,
                    job_id,
                ),
            )

    def mark_running(self, job_id: str, *, owner_instance: str) -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE gateway_jobs
                   SET status = 'running',
                       started_at = COALESCE(started_at, ?),
                       updated_at = ?,
                       owner_instance = ?
                 WHERE job_id = ?
                   AND status IN ('pending', 'running', 'resume_pending')
                """,
                (now, now, owner_instance, job_id),
            )

    def mark_resume_pending(self, job_id: str, reason: str) -> bool:
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE gateway_jobs
                   SET status = 'resume_pending', resume_reason = ?,
                       updated_at = ?
                 WHERE job_id = ?
                   AND status IN ('pending', 'running', 'resume_pending')
                """,
                (str(reason or "gateway_interrupted"), now, job_id),
            )
            return cur.rowcount > 0

    def mark_session_resume_pending(
        self, session_key: str, reason: str
    ) -> list[str]:
        now = time.time()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT job_id FROM gateway_jobs
                 WHERE session_key = ?
                   AND status IN ('pending', 'running', 'resume_pending')
                """,
                (session_key,),
            ).fetchall()
            job_ids = [str(row["job_id"]) for row in rows]
            if job_ids:
                conn.execute(
                    """
                    UPDATE gateway_jobs
                       SET status = 'resume_pending', resume_reason = ?,
                           updated_at = ?
                     WHERE session_key = ?
                       AND status IN ('pending', 'running', 'resume_pending')
                    """,
                    (str(reason or "gateway_interrupted"), now, session_key),
                )
            return job_ids

    def mark_recovery_scheduled(
        self, job_id: str, *, owner_instance: str
    ) -> bool:
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE gateway_jobs
                   SET status = 'resume_pending',
                       resume_attempts = resume_attempts + 1,
                       updated_at = ?,
                       owner_instance = ?
                 WHERE job_id = ?
                   AND status IN ('pending', 'running', 'resume_pending')
                """,
                (now, owner_instance, job_id),
            )
            return cur.rowcount > 0

    def complete(
        self,
        job_id: str,
        result_text: str,
        *,
        delivered: bool = False,
    ) -> bool:
        now = time.time()
        delivered_at = now if delivered else None
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE gateway_jobs
                   SET status = 'completed', result_text = ?, error_text = NULL,
                       completed_at = COALESCE(completed_at, ?),
                       delivered_at = COALESCE(delivered_at, ?),
                       updated_at = ?
                 WHERE job_id = ?
                   AND status != 'cancelled'
                """,
                (str(result_text or ""), now, delivered_at, now, job_id),
            )
            return cur.rowcount > 0

    def fail(
        self,
        job_id: str,
        *,
        error_text: str,
        result_text: str = "",
        delivered: bool = False,
    ) -> bool:
        now = time.time()
        delivered_at = now if delivered else None
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE gateway_jobs
                   SET status = 'failed', result_text = ?, error_text = ?,
                       completed_at = COALESCE(completed_at, ?),
                       delivered_at = COALESCE(delivered_at, ?),
                       updated_at = ?
                 WHERE job_id = ?
                   AND status != 'cancelled'
                """,
                (
                    str(result_text or ""),
                    str(error_text or ""),
                    now,
                    delivered_at,
                    now,
                    job_id,
                ),
            )
            return cur.rowcount > 0

    def record_delivery(
        self,
        job_id: str,
        *,
        success: bool,
        error: Optional[str] = None,
    ) -> bool:
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE gateway_jobs
                   SET delivery_attempts = delivery_attempts + 1,
                       delivered_at = CASE
                           WHEN ? THEN COALESCE(delivered_at, ?)
                           ELSE delivered_at
                       END,
                       last_delivery_error = CASE
                           WHEN ? THEN NULL
                           ELSE ?
                       END,
                       updated_at = ?
                 WHERE job_id = ?
                   AND status IN ('completed', 'failed')
                """,
                (
                    1 if success else 0,
                    now,
                    1 if success else 0,
                    str(error or "delivery failed"),
                    now,
                    job_id,
                ),
            )
            return cur.rowcount > 0

    def cancel(self, job_id: str, reason: str = "cancelled") -> bool:
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE gateway_jobs
                   SET status = 'cancelled', error_text = ?,
                       completed_at = COALESCE(completed_at, ?),
                       updated_at = ?
                 WHERE job_id = ?
                   AND status IN ('pending', 'running', 'resume_pending')
                """,
                (str(reason or "cancelled"), now, now, job_id),
            )
            return cur.rowcount > 0

    def cancel_session(self, session_key: str, reason: str) -> list[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT job_id FROM gateway_jobs
                 WHERE session_key = ?
                   AND status IN ('pending', 'running', 'resume_pending')
                """,
                (session_key,),
            ).fetchall()
            job_ids = [str(row["job_id"]) for row in rows]
        for job_id in job_ids:
            self.cancel(job_id, reason)
        return job_ids

    def active_jobs(self) -> list[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM gateway_jobs
                 WHERE status IN ({placeholders})
                 ORDER BY created_at ASC
                """,
                tuple(sorted(ACTIVE_STATUSES)),
            ).fetchall()
            return [dict(row) for row in rows]

    def terminal_undelivered_jobs(self) -> list[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM gateway_jobs
                 WHERE status IN ('completed', 'failed')
                   AND delivered_at IS NULL
                   AND COALESCE(result_text, '') != ''
                 ORDER BY completed_at ASC, created_at ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def active_for_session(self, session_key: str) -> list[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM gateway_jobs
                 WHERE session_key = ?
                   AND status IN ({placeholders})
                 ORDER BY created_at ASC
                """,
                (session_key, *tuple(sorted(ACTIVE_STATUSES))),
            ).fetchall()
            return [dict(row) for row in rows]

    def prune(self, *, older_than_days: int = 30) -> int:
        cutoff = time.time() - max(1, int(older_than_days)) * 86400
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM gateway_jobs
                 WHERE status IN ('completed', 'failed', 'cancelled')
                   AND updated_at < ?
                """,
                (cutoff,),
            )
            return max(0, int(cur.rowcount))

    def close(self) -> None:
        """Compatibility no-op: this store does not retain open connections."""


def job_ids(jobs: Iterable[Dict[str, Any]]) -> set[str]:
    return {str(job.get("job_id") or "") for job in jobs if job.get("job_id")}
