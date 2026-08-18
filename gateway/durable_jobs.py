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
ATTACHABLE_STATUSES = frozenset({"pending", "running", "resume_pending"})


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

                CREATE TABLE IF NOT EXISTS gateway_inbox_events (
                    event_id TEXT PRIMARY KEY,
                    dedupe_key TEXT UNIQUE,
                    thread_key TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    request_text TEXT NOT NULL,
                    request_message_id TEXT,
                    platform_update_id INTEGER,
                    reply_to_message_id TEXT,
                    message_type TEXT NOT NULL DEFAULT 'text',
                    media_json TEXT NOT NULL DEFAULT '[]',
                    event_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'received',
                    job_id TEXT,
                    route_action TEXT,
                    route_confidence REAL,
                    route_reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_gateway_inbox_status
                    ON gateway_inbox_events(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_gateway_inbox_thread
                    ON gateway_inbox_events(thread_key, created_at);

                CREATE TABLE IF NOT EXISTS gateway_job_inputs (
                    job_id TEXT NOT NULL,
                    input_version INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    request_text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(job_id, input_version)
                );

                CREATE TABLE IF NOT EXISTS gateway_job_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_gateway_job_events_job
                    ON gateway_job_events(job_id, seq);

                CREATE TABLE IF NOT EXISTS gateway_job_delivery_messages (
                    job_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(job_id, message_id)
                );

                CREATE INDEX IF NOT EXISTS idx_gateway_delivery_message
                    ON gateway_job_delivery_messages(message_id, job_id);
                """
            )
            # Online migration for registries created before semantic job
            # routing. SQLite lacks ADD COLUMN IF NOT EXISTS, so inspect first.
            existing_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(gateway_jobs)").fetchall()
            }
            migrations = {
                "thread_key": "TEXT",
                "branch_id": "TEXT",
                "parent_job_id": "TEXT",
                "input_version": "INTEGER NOT NULL DEFAULT 1",
                "result_input_version": "INTEGER",
                "routing_summary": "TEXT",
                "delivery_message_id": "TEXT",
            }
            for column, definition in migrations.items():
                if column not in existing_columns:
                    conn.execute(
                        f"ALTER TABLE gateway_jobs ADD COLUMN {column} {definition}"
                    )
            conn.execute(
                "UPDATE gateway_jobs SET thread_key = session_key "
                "WHERE thread_key IS NULL OR thread_key = ''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gateway_jobs_thread "
                "ON gateway_jobs(thread_key, status, updated_at)"
            )
            inbox_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(gateway_inbox_events)"
                ).fetchall()
            }
            for column, definition in {
                "message_type": "TEXT NOT NULL DEFAULT 'text'",
                "event_json": "TEXT NOT NULL DEFAULT '{}'",
            }.items():
                if column not in inbox_columns:
                    conn.execute(
                        f"ALTER TABLE gateway_inbox_events ADD COLUMN {column} {definition}"
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

    @staticmethod
    def _event_id() -> str:
        return f"evt_{uuid.uuid4().hex}"

    @staticmethod
    def _job_id() -> str:
        # Keep the established public prefix while retaining the full 128-bit
        # UUID entropy (the previous implementation truncated it to 64 bits).
        return f"gw_{uuid.uuid4().hex}"

    def ingest_event(
        self,
        *,
        thread_key: str,
        platform: str,
        source: Any,
        request_text: str,
        message_id: Optional[str] = None,
        platform_update_id: Optional[int] = None,
        reply_to_message_id: Optional[str] = None,
        message_type: str = "text",
        media: Optional[list[dict[str, Any]]] = None,
        event_metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[Dict[str, Any], bool]:
        """Durably accept an inbound event before semantic job routing.

        This is the loss-prevention boundary. Receiving a message does not
        mechanically create a job: the router may attach the event to an
        already-running job instead.
        """
        now = time.time()
        event_id = self._event_id()
        dedupe_key = self.make_dedupe_key(
            platform=platform,
            session_key=thread_key,
            message_id=message_id,
            platform_update_id=platform_update_id,
        )
        with self._lock, self._connect() as conn:
            created = True
            try:
                conn.execute(
                    """
                    INSERT INTO gateway_inbox_events (
                        event_id, dedupe_key, thread_key, platform, source_json,
                        request_text, request_message_id, platform_update_id,
                        reply_to_message_id, message_type, media_json, event_json,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', ?, ?)
                    """,
                    (
                        event_id,
                        dedupe_key,
                        thread_key,
                        platform,
                        self._source_json(source),
                        str(request_text or ""),
                        str(message_id) if message_id is not None else None,
                        platform_update_id,
                        str(reply_to_message_id)
                        if reply_to_message_id is not None
                        else None,
                        str(message_type or "text"),
                        json.dumps(
                            media or [],
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        ),
                        json.dumps(
                            event_metadata or {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        ),
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
                    "SELECT * FROM gateway_inbox_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM gateway_inbox_events WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
        result = self._row(row)
        if result is None:
            raise RuntimeError("Durable gateway inbox insert returned no row")
        return result, created

    def get_inbox_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            return self._row(
                conn.execute(
                    "SELECT * FROM gateway_inbox_events WHERE event_id = ?",
                    (str(event_id),),
                ).fetchone()
            )

    def enrich_inbox_event(
        self,
        event_id: str,
        *,
        request_text: str,
        event_metadata: Optional[dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Persist media-derived text before semantic job routing.

        Voice notes enter the durable inbox before STT so a process exit cannot
        lose the Telegram update or cached media path.  Once STT finishes, this
        method atomically replaces the empty raw request with the transcript;
        only then may the semantic router create or attach a job.
        """
        now = time.time()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE gateway_inbox_events
                   SET request_text = ?, event_json = ?, updated_at = ?
                 WHERE event_id = ? AND status = 'received' AND job_id IS NULL
                """,
                (
                    str(request_text or ""),
                    json.dumps(
                        event_metadata or {},
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                    now,
                    str(event_id),
                ),
            )
            row = conn.execute(
                "SELECT * FROM gateway_inbox_events WHERE event_id = ?",
                (str(event_id),),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(event_id)
        if cursor.rowcount == 0 and result.get("status") == "received":
            raise RuntimeError(
                f"Durable inbox event {event_id} could not be enriched"
            )
        return result

    def mark_inbox_context_only(
        self,
        event_id: str,
        *,
        request_text: str,
        event_metadata: Optional[dict[str, Any]] = None,
        reason: str = "transcript_only",
    ) -> Dict[str, Any]:
        """Finish an inbox event that intentionally requires no AI job."""
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE gateway_inbox_events
                   SET request_text = ?, event_json = ?, status = 'context_only',
                       route_action = 'context_only', route_reason = ?,
                       updated_at = ?
                 WHERE event_id = ? AND status = 'received' AND job_id IS NULL
                """,
                (
                    str(request_text or ""),
                    json.dumps(
                        event_metadata or {},
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                    str(reason or "transcript_only"),
                    now,
                    str(event_id),
                ),
            )
            row = conn.execute(
                "SELECT * FROM gateway_inbox_events WHERE event_id = ?",
                (str(event_id),),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(event_id)
        return result

    def unrouted_inbox_events(self) -> list[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM gateway_inbox_events
                 WHERE status = 'received'
                 ORDER BY created_at ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def create_job_for_event(
        self,
        event_id: str,
        *,
        parent_job_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        routing_summary: Optional[str] = None,
        confidence: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> tuple[Dict[str, Any], bool]:
        """Atomically route one inbox event into a new independent job."""
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            inbox = conn.execute(
                "SELECT * FROM gateway_inbox_events WHERE event_id = ?",
                (str(event_id),),
            ).fetchone()
            if inbox is None:
                raise KeyError(event_id)
            if inbox["job_id"]:
                existing = conn.execute(
                    "SELECT * FROM gateway_jobs WHERE job_id = ?",
                    (str(inbox["job_id"]),),
                ).fetchone()
                result = self._row(existing)
                if result is None:
                    raise RuntimeError("Inbox event references a missing job")
                return result, False

            job_id = self._job_id()
            resolved_branch = str(branch_id or "").strip() or (
                str(parent_job_id or "").strip() or job_id
            )
            execution_key = f"{inbox['thread_key']}:job:{job_id}"
            conn.execute(
                """
                INSERT INTO gateway_jobs (
                    job_id, dedupe_key, session_key, platform, source_json,
                    request_text, request_message_id, status, created_at,
                    updated_at, thread_key, branch_id, parent_job_id,
                    input_version, routing_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    job_id,
                    f"inbox:{event_id}",
                    execution_key,
                    inbox["platform"],
                    inbox["source_json"],
                    inbox["request_text"],
                    inbox["request_message_id"],
                    now,
                    now,
                    inbox["thread_key"],
                    resolved_branch,
                    parent_job_id,
                    str(routing_summary or inbox["request_text"] or "")[:1000],
                ),
            )
            conn.execute(
                """
                INSERT INTO gateway_job_inputs (
                    job_id, input_version, event_id, request_text, created_at
                ) VALUES (?, 1, ?, ?, ?)
                """,
                (job_id, event_id, inbox["request_text"], now),
            )
            conn.execute(
                """
                UPDATE gateway_inbox_events
                   SET status = 'routed', job_id = ?, route_action = 'new_job',
                       route_confidence = ?, route_reason = ?, updated_at = ?
                 WHERE event_id = ?
                """,
                (job_id, confidence, reason, now, event_id),
            )
            conn.execute(
                """
                INSERT INTO gateway_job_events(job_id, event_type, payload_json, created_at)
                VALUES (?, 'job_created', ?, ?)
                """,
                (
                    job_id,
                    json.dumps(
                        {"event_id": event_id, "input_version": 1},
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM gateway_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise RuntimeError("Job routing insert returned no row")
        return result, True

    def attach_event_to_job(
        self,
        event_id: str,
        job_id: str,
        *,
        confidence: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomically add an inbound scope update to an active job."""
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            inbox = conn.execute(
                "SELECT * FROM gateway_inbox_events WHERE event_id = ?",
                (str(event_id),),
            ).fetchone()
            if inbox is None:
                raise KeyError(event_id)
            if inbox["job_id"]:
                existing = conn.execute(
                    "SELECT * FROM gateway_jobs WHERE job_id = ?",
                    (str(inbox["job_id"]),),
                ).fetchone()
                result = self._row(existing)
                if result is None:
                    raise RuntimeError("Inbox event references a missing job")
                return result
            job = conn.execute(
                "SELECT * FROM gateway_jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
            if job is None:
                raise KeyError(job_id)
            job_status = str(job["status"])
            completed_but_undelivered = (
                job_status == "completed" and job["delivered_at"] is None
            )
            if (
                job_status not in ATTACHABLE_STATUSES
                and not completed_but_undelivered
            ):
                raise RuntimeError(
                    f"Job {job_id} is not accepting updates (status={job['status']})"
                )
            version = int(job["input_version"] or 1) + 1
            update_text = str(inbox["request_text"] or "")
            combined = str(job["request_text"] or "")
            if update_text:
                combined = (
                    f"{combined}\n\n[Additional user instruction]\n{update_text}"
                    if combined
                    else update_text
                )
            conn.execute(
                """
                UPDATE gateway_jobs
                   SET input_version = ?, request_text = ?, updated_at = ?,
                       status = 'resume_pending',
                       result_text = CASE WHEN status = 'completed' THEN NULL ELSE result_text END,
                       result_input_version = CASE WHEN status = 'completed' THEN NULL ELSE result_input_version END,
                       completed_at = CASE WHEN status = 'completed' THEN NULL ELSE completed_at END
                 WHERE job_id = ?
                """,
                (version, combined, now, job_id),
            )
            conn.execute(
                """
                INSERT INTO gateway_job_inputs (
                    job_id, input_version, event_id, request_text, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, version, event_id, update_text, now),
            )
            conn.execute(
                """
                UPDATE gateway_inbox_events
                   SET status = 'routed', job_id = ?, route_action = 'attach',
                       route_confidence = ?, route_reason = ?, updated_at = ?
                 WHERE event_id = ?
                """,
                (job_id, confidence, reason, now, event_id),
            )
            conn.execute(
                """
                INSERT INTO gateway_job_events(job_id, event_type, payload_json, created_at)
                VALUES (?, 'input_attached', ?, ?)
                """,
                (
                    job_id,
                    json.dumps(
                        {"event_id": event_id, "input_version": version},
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM gateway_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise RuntimeError("Attached job disappeared")
        return result

    def active_for_thread(self, thread_key: str) -> list[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in ATTACHABLE_STATUSES)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM gateway_jobs
                 WHERE thread_key = ?
                   AND (
                       status IN ({placeholders})
                       OR (status = 'completed' AND delivered_at IS NULL)
                   )
                 ORDER BY created_at ASC
                """,
                (thread_key, *tuple(sorted(ATTACHABLE_STATUSES))),
            ).fetchall()
            return [dict(row) for row in rows]

    def recent_terminal_for_thread(
        self, thread_key: str, *, limit: int = 5
    ) -> list[Dict[str, Any]]:
        """Return recent completed public branch heads for context routing."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM gateway_jobs
                 WHERE thread_key = ?
                   AND status IN ('completed', 'failed')
                   AND delivered_at IS NOT NULL
                 ORDER BY COALESCE(completed_at, updated_at) DESC
                 LIMIT ?
                """,
                (str(thread_key), max(1, min(int(limit), 20))),
            ).fetchall()
            return [dict(row) for row in rows]

    def inputs_for_job(self, job_id: str) -> list[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM gateway_job_inputs
                 WHERE job_id = ? ORDER BY input_version ASC
                """,
                (str(job_id),),
            ).fetchall()
            return [dict(row) for row in rows]

    def job_for_delivery_message(
        self, thread_key: str, message_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        if not message_id:
            return None
        with self._lock, self._connect() as conn:
            return self._row(
                conn.execute(
                    """
                    SELECT j.* FROM gateway_jobs AS j
                    LEFT JOIN gateway_job_delivery_messages AS d
                      ON d.job_id = j.job_id
                     WHERE j.thread_key = ?
                       AND (j.delivery_message_id = ? OR d.message_id = ?)
                     ORDER BY j.updated_at DESC LIMIT 1
                    """,
                    (str(thread_key), str(message_id), str(message_id)),
                ).fetchone()
            )

    def append_job_event(
        self, job_id: str, event_type: str, payload: Optional[dict[str, Any]] = None
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gateway_job_events(job_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(job_id),
                    str(event_type),
                    json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
                    time.time(),
                ),
            )

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
        job_id = self._job_id()
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
                        updated_at, thread_key, branch_id, input_version,
                        routing_summary
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, 1, ?)
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
                        session_key,
                        job_id,
                        str(request_text or "")[:1000],
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
        input_version: Optional[int] = None,
    ) -> bool:
        now = time.time()
        delivered_at = now if delivered else None
        with self._lock, self._connect() as conn:
            expected_version = input_version
            if expected_version is None:
                row = conn.execute(
                    "SELECT input_version FROM gateway_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                expected_version = int(row["input_version"] or 1) if row else 1
            cur = conn.execute(
                """
                UPDATE gateway_jobs
                   SET status = 'completed', result_text = ?, error_text = NULL,
                       completed_at = COALESCE(completed_at, ?),
                       delivered_at = COALESCE(delivered_at, ?),
                       updated_at = ?, result_input_version = ?
                 WHERE job_id = ?
                   AND status IN ('pending', 'running', 'resume_pending')
                   AND input_version = ?
                """,
                (
                    str(result_text or ""),
                    now,
                    delivered_at,
                    now,
                    expected_version,
                    job_id,
                    expected_version,
                ),
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
                   AND status IN ('pending', 'running', 'resume_pending')
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
        message_id: Optional[str] = None,
        message_ids: Optional[Iterable[str]] = None,
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
                       delivery_message_id = CASE
                           WHEN ? THEN COALESCE(delivery_message_id, ?)
                           ELSE delivery_message_id
                       END,
                       updated_at = ?
                 WHERE job_id = ?
                   AND status IN ('completed', 'failed')
                   AND (result_input_version IS NULL OR result_input_version = input_version)
                """,
                (
                    1 if success else 0,
                    now,
                    1 if success else 0,
                    str(error or "delivery failed"),
                    1 if success and message_id else 0,
                    str(message_id) if message_id is not None else None,
                    now,
                    job_id,
                ),
            )
            if cur.rowcount > 0 and success:
                resolved_message_ids = {
                    str(value)
                    for value in (message_ids or [])
                    if value is not None and str(value)
                }
                if message_id is not None and str(message_id):
                    resolved_message_ids.add(str(message_id))
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO gateway_job_delivery_messages(
                        job_id, message_id, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    [
                        (str(job_id), resolved_message_id, now)
                        for resolved_message_id in resolved_message_ids
                    ],
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
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT job_id FROM gateway_jobs
                 WHERE status IN ('completed', 'failed', 'cancelled')
                   AND updated_at < ?
                """,
                (cutoff,),
            ).fetchall()
            stale_ids = [str(row["job_id"]) for row in rows]
            if not stale_ids:
                return 0
            placeholders = ",".join("?" for _ in stale_ids)
            conn.execute(
                f"DELETE FROM gateway_job_events WHERE job_id IN ({placeholders})",
                stale_ids,
            )
            conn.execute(
                f"DELETE FROM gateway_job_inputs WHERE job_id IN ({placeholders})",
                stale_ids,
            )
            conn.execute(
                f"DELETE FROM gateway_job_delivery_messages WHERE job_id IN ({placeholders})",
                stale_ids,
            )
            conn.execute(
                f"DELETE FROM gateway_inbox_events WHERE job_id IN ({placeholders})",
                stale_ids,
            )
            cur = conn.execute(
                f"DELETE FROM gateway_jobs WHERE job_id IN ({placeholders})",
                stale_ids,
            )
            return max(0, int(cur.rowcount))

    def close(self) -> None:
        """Compatibility no-op: this store does not retain open connections."""


def job_ids(jobs: Iterable[Dict[str, Any]]) -> set[str]:
    return {str(job.get("job_id") or "") for job in jobs if job.get("job_id")}
