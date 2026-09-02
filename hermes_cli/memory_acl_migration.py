"""Offline, read-only planner for the MemPalace ACL migration.

This module deliberately has no apply mode.  It inventories a verified,
operator-created snapshot and emits a content-free deterministic manifest for
later review.  It never opens the live Hermes root, connects to MemPalace, or
changes a source database.

SQLite snapshots must be checkpointed before capture.  A database accompanied
by ``-wal`` or ``-shm`` is rejected rather than risking an inconsistent read.
Rollback for any future migration is backup restore plus disabling the new
memory policy; destructive SQL is never part of the generated plan.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
from dataclasses import dataclass
import errno
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
RECEIPT_FORMAT = "hermes-memory-snapshot-receipt-v1"
SERVICE_EXPORT_FORMAT = "mempalace-offline-export-v1"
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_BYTES = 16 * 1024 * 1024
MAX_MEMORY_FILE_BYTES = 16 * 1024 * 1024
MAX_SQLITE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXPORT_BYTES = 512 * 1024 * 1024
MAX_ROW_BYTES = 4 * 1024 * 1024
MAX_ROWS_PER_TABLE = 5_000_000
MAX_SOURCES = 4096
MAX_RECEIPT_FILES = 100_000

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PRINCIPAL_RE = re.compile(r"^p_[a-z2-7]{16,52}$")
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PLATFORM_RE = re.compile(r"^[a-z0-9_-]+$")
_OPAQUE_CHAT_RE = re.compile(r"^c_[a-z2-7]{26}$")
_OPAQUE_THREAD_RE = re.compile(r"^t_[a-z2-7]{26}$")
_BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SOURCE_KINDS = frozenset({
    "memory_file",
    "session_db",
    "legacy_kg_db",
    "mempalace_export",
})
_SERVICE_RECORD_KINDS = frozenset({"drawer", "document", "kg_entity", "kg_triple"})


class MigrationPlanError(ValueError):
    """The snapshot or mapping cannot be planned safely."""


@dataclass(frozen=True)
class _InventoryRecord:
    source_namespace: str
    source_path: str
    kind: str
    legacy_id: str
    content_sha256: str
    provenance_sha256: str
    target_wing: str | None
    quarantine_reason: str | None
    locator: Mapping[str, Any]

    def as_manifest_record(self) -> dict[str, Any]:
        action = "quarantine" if self.quarantine_reason else "migrate"
        legacy_id_sha256 = hashlib.sha256(self.legacy_id.encode("utf-8")).hexdigest()
        source_path_sha256 = _manifest_ref("source-path", self.source_path)
        source_locator = dict(self.locator)
        identity = {
            "source_namespace_sha256": _manifest_ref(
                "source-namespace", self.source_namespace
            ),
            "kind": self.kind,
            "legacy_id_sha256": legacy_id_sha256,
            "content_sha256": self.content_sha256,
            "provenance_sha256": self.provenance_sha256,
            "target_wing": self.target_wing,
            "action": action,
            "quarantine_reason": self.quarantine_reason,
        }
        return {
            **identity,
            "source_path_sha256": source_path_sha256,
            "source_locator": source_locator,
            "idempotency_key": _sha256_canonical([
                "hermes-memory-migration-record-v1",
                identity,
            ]),
        }


def _pairs_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MigrationPlanError("JSON object contains a duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    del value
    raise MigrationPlanError("non-finite JSON number is not allowed")


def _decode_json(raw: bytes, *, label: str, max_bytes: int) -> Any:
    if len(raw) > max_bytes:
        raise MigrationPlanError(f"{label} exceeds the size limit")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationPlanError(f"invalid {label}") from exc


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MigrationPlanError(f"{label} must be an object")
    return value


def _list(value: Any, label: str, *, maximum: int) -> list[Any]:
    if not isinstance(value, list):
        raise MigrationPlanError(f"{label} must be an array")
    if len(value) > maximum:
        raise MigrationPlanError(f"{label} has too many entries")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    label: str,
) -> None:
    keys = set(value)
    if any(not isinstance(key, str) for key in value):
        raise MigrationPlanError(f"{label} keys must be strings")
    missing = sorted(required - keys)
    extra = sorted(keys - required - optional)
    if missing or extra:
        raise MigrationPlanError(f"{label} has an invalid key set")


def _string(value: Any, label: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MigrationPlanError(f"{label} must be a non-empty normalized string")
    if len(value.encode("utf-8")) > maximum:
        raise MigrationPlanError(f"{label} is too long")
    return value


def _sha256(value: Any, label: str) -> str:
    text = _string(value, label, maximum=64)
    if not _SHA256_RE.fullmatch(text):
        raise MigrationPlanError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _canonical_value(value: Any) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MigrationPlanError("non-finite database value is not supported")
        return value
    if isinstance(value, bytes):
        return {"$bytes_base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise MigrationPlanError("object keys must be strings")
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise MigrationPlanError(f"unsupported value type: {type(value).__name__}")


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            _canonical_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MigrationPlanError("value cannot be represented canonically") from exc


def _sha256_canonical(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _manifest_ref(kind: str, value: str) -> str:
    return _sha256_canonical(["hermes-memory-migration-ref-v1", kind, value])


def _relative_path(value: Any, label: str) -> str:
    text = _string(value, label, maximum=1024)
    try:
        pure = PurePosixPath(text)
    except (TypeError, ValueError) as exc:
        raise MigrationPlanError(f"{label} is invalid") from exc
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise MigrationPlanError(f"{label} must be a normalized snapshot-relative path")
    normalized = pure.as_posix()
    if normalized != text or "\\" in text:
        raise MigrationPlanError(f"{label} must use normalized POSIX separators")
    return normalized


def _known_live_roots() -> set[Path]:
    roots: set[Path] = set()
    try:
        from hermes_constants import get_default_hermes_root, get_hermes_home

        roots.add(get_default_hermes_root().expanduser().resolve(strict=False))
        roots.add(get_hermes_home().expanduser().resolve(strict=False))
    except Exception as exc:
        raise MigrationPlanError("cannot resolve the live Hermes root") from exc
    configured = os.environ.get("HERMES_HOME")
    if configured:
        roots.add(Path(configured).expanduser().resolve(strict=False))
    return roots


def _snapshot_root(value: Any) -> Path:
    text = _string(value, "snapshot_root", maximum=4096)
    original = Path(text).expanduser()
    if not original.is_absolute():
        raise MigrationPlanError("snapshot_root must be absolute")
    if original.is_symlink():
        raise MigrationPlanError("snapshot_root must not be a symlink")
    try:
        root = original.resolve(strict=True)
    except OSError as exc:
        raise MigrationPlanError("snapshot_root does not exist") from exc
    if not root.is_dir():
        raise MigrationPlanError("snapshot_root must be a directory")
    root_info = root.stat()
    if (
        root_info.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(root_info.st_mode) & 0o077
    ):
        raise MigrationPlanError(
            "snapshot_root must be owner-controlled and inaccessible to group/other"
        )
    live_roots = _known_live_roots()
    if any(
        root == live or root.is_relative_to(live) or live.is_relative_to(root)
        for live in live_roots
    ):
        raise MigrationPlanError(
            "snapshot_root must be disjoint from every live Hermes root"
        )
    return root


def _open_snapshot_fd(root: Path, relative: str, label: str) -> tuple[int, Path]:
    """Open a snapshot file without following any path component symlinks."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise MigrationPlanError("secure snapshot file opening is unavailable")
    parts = PurePosixPath(relative).parts
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptors: list[int] = []
    try:
        current_fd = os.open(root, directory_flags)
        descriptors.append(current_fd)
        root_info = os.fstat(current_fd)
        if (
            root_info.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(root_info.st_mode) & 0o077
        ):
            raise MigrationPlanError(
                f"{label} path must use owner-controlled private directories"
            )
        for part in parts[:-1]:
            current_fd = os.open(part, directory_flags, dir_fd=current_fd)
            descriptors.append(current_fd)
            directory_info = os.fstat(current_fd)
            if (
                directory_info.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(directory_info.st_mode) & 0o077
            ):
                raise MigrationPlanError(
                    f"{label} path must use owner-controlled private directories"
                )
        file_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise MigrationPlanError(f"{label} must be a regular file")
            if info.st_uid not in {0, os.geteuid()} or info.st_nlink != 1:
                raise MigrationPlanError(
                    f"{label} must be owner-controlled and not hard-linked"
                )
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise MigrationPlanError(f"{label} must be inaccessible to group/other")
        except BaseException:
            os.close(file_fd)
            raise
        return file_fd, root.joinpath(*parts)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise MigrationPlanError(f"{label} must not traverse symlinks") from exc
        raise MigrationPlanError(f"cannot securely open {label}") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _fd_bytes(fd: int, *, maximum: int, label: str) -> tuple[bytes, str, int]:
    before = os.fstat(fd)
    if before.st_size > maximum:
        raise MigrationPlanError(f"{label} exceeds the size limit")
    chunks: list[bytes] = []
    offset = 0
    while offset < before.st_size:
        chunk = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
        if not chunk:
            raise MigrationPlanError(f"{label} changed while being read")
        chunks.append(chunk)
        offset += len(chunk)
    after = os.fstat(fd)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_after != identity_before:
        raise MigrationPlanError(f"{label} changed while being read")
    raw = b"".join(chunks)
    return raw, hashlib.sha256(raw).hexdigest(), before.st_size


def _fd_hash(fd: int, *, maximum: int, label: str) -> tuple[str, int]:
    before = os.fstat(fd)
    if before.st_size > maximum:
        raise MigrationPlanError(f"{label} exceeds the size limit")
    digest = hashlib.sha256()
    offset = 0
    while offset < before.st_size:
        chunk = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
        if not chunk:
            raise MigrationPlanError(f"{label} changed while being read")
        digest.update(chunk)
        offset += len(chunk)
    after = os.fstat(fd)
    if (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ):
        raise MigrationPlanError(f"{label} changed while being read")
    return digest.hexdigest(), before.st_size


def _verify_snapshot_path_identity(
    root: Path, relative: str, expected_fd: int, label: str
) -> None:
    current_fd, _ = _open_snapshot_fd(root, relative, label)
    try:
        expected = os.fstat(expected_fd)
        current = os.fstat(current_fd)
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise MigrationPlanError(f"{label} was replaced during planning")
    finally:
        os.close(current_fd)


def _target_wing(value: Any, label: str) -> tuple[str, str]:
    wing = _string(value, label, maximum=128)
    parts = wing.split(".")
    if len(parts) == 3 and parts[0] == "private":
        if not _PRINCIPAL_RE.fullmatch(parts[1]) or not _COMPONENT_RE.fullmatch(
            parts[2]
        ):
            raise MigrationPlanError(f"{label} is not a valid private wing")
        return wing, "private"
    if len(parts) == 5 and parts[0] == "group":
        platform, chat, thread, zone = parts[1:]
        if (
            not _PLATFORM_RE.fullmatch(platform)
            or not _OPAQUE_CHAT_RE.fullmatch(chat)
            or not (thread == "root" or _OPAQUE_THREAD_RE.fullmatch(thread))
            or not _COMPONENT_RE.fullmatch(zone)
        ):
            raise MigrationPlanError(f"{label} is not a valid opaque group wing")
        return wing, "group"
    raise MigrationPlanError(f"{label} must be an exact private or group wing")


def _source_namespace(value: Any, label: str) -> str:
    namespace = _string(value, label, maximum=128)
    if not _NAMESPACE_RE.fullmatch(namespace):
        raise MigrationPlanError(f"{label} has unsafe characters")
    return namespace


def _ensure_checkpointed_sqlite(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        if Path(f"{path}{suffix}").exists():
            raise MigrationPlanError(
                "SQLite snapshot has WAL sidecars; checkpoint it before capture"
            )


def _connect_sqlite(path: Path, fd: int) -> sqlite3.Connection:
    _ensure_checkpointed_sqlite(path)
    try:
        conn = sqlite3.connect(f"file:/proc/self/fd/{fd}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn
    except sqlite3.Error as exc:
        raise MigrationPlanError("cannot open SQLite snapshot read-only") from exc


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error as exc:
        raise MigrationPlanError(f"cannot inspect SQLite table {table}") from exc
    return [str(row[1]) for row in rows]


def _require_columns(
    conn: sqlite3.Connection, table: str, required: set[str]
) -> list[str]:
    columns = _table_columns(conn, table)
    missing = sorted(required - set(columns))
    if missing:
        raise MigrationPlanError(
            f"SQLite table {table} is missing columns: {', '.join(missing)}"
        )
    return columns


def _bounded_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    columns: list[str],
    order_by: str,
) -> Iterable[sqlite3.Row]:
    try:
        count = int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        if count > MAX_ROWS_PER_TABLE:
            raise MigrationPlanError(f"SQLite table {table} exceeds the row limit")
        sql = f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order_by}"
        yield from conn.execute(sql)
    except sqlite3.Error as exc:
        raise MigrationPlanError(f"cannot read SQLite table {table}") from exc


def _row_payload(row: sqlite3.Row, columns: list[str]) -> dict[str, Any]:
    payload = {column: _canonical_value(row[column]) for column in columns}
    if len(_canonical_bytes(payload)) > MAX_ROW_BYTES:
        raise MigrationPlanError("SQLite row exceeds the row size limit")
    return payload


def _record(
    *,
    namespace: str,
    source_path: str,
    kind: str,
    legacy_id: str,
    payload: Any,
    provenance: Any,
    target_wing: str | None,
    quarantine_reason: str | None,
    locator: Mapping[str, Any],
) -> _InventoryRecord:
    legacy_id = _string(legacy_id, f"{kind} legacy id", maximum=1024)
    return _InventoryRecord(
        source_namespace=namespace,
        source_path=source_path,
        kind=kind,
        legacy_id=legacy_id,
        content_sha256=_sha256_canonical(payload),
        provenance_sha256=_sha256_canonical(provenance),
        target_wing=target_wing,
        quarantine_reason=quarantine_reason,
        locator=dict(locator),
    )


def _parse_session_scope_kind(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    parts = raw.split(":")
    if len(parts) < 4 or parts[:2] != ["agent", "main"]:
        return None
    index = 2
    if len(parts) > 6 and parts[2] == "profile":
        if not parts[3] or parts[4] != "scope" or not parts[5]:
            return None
        index = 6
    if len(parts) <= index + 2 or not parts[index] or not parts[index + 2]:
        return None
    chat_type = parts[index + 1].strip().lower()
    if chat_type in {"dm", "private"}:
        return "private"
    if chat_type in {"group", "supergroup", "channel", "forum", "thread"}:
        return "group"
    return None


def _inventory_memory_file(
    *,
    source: Mapping[str, Any],
    namespace: str,
    relative: str,
    path: Path,
    fd: int,
) -> list[_InventoryRecord]:
    _exact_keys(
        source,
        required={"kind", "source_namespace", "path", "target_wing"},
        label=f"source {namespace}",
    )
    if path.name not in {"MEMORY.md", "USER.md"}:
        raise MigrationPlanError("memory_file path must end in MEMORY.md or USER.md")
    target, target_kind = _target_wing(source["target_wing"], "target_wing")
    if target_kind != "private":
        raise MigrationPlanError("MEMORY.md and USER.md may target private wings only")
    digest, _ = _fd_hash(fd, maximum=MAX_MEMORY_FILE_BYTES, label="memory file")
    return [
        _InventoryRecord(
            source_namespace=namespace,
            source_path=relative,
            kind="legacy_memory_file",
            legacy_id=path.name,
            content_sha256=digest,
            provenance_sha256=_sha256_canonical({"file_kind": path.name}),
            target_wing=target,
            quarantine_reason=None,
            locator={"file": path.name},
        )
    ]


def _parse_scope_map(value: Any, label: str) -> dict[str, tuple[str, str]]:
    mapping = _mapping(value, label)
    if len(mapping) > MAX_ROWS_PER_TABLE:
        raise MigrationPlanError(f"{label} has too many entries")
    parsed: dict[str, tuple[str, str]] = {}
    for raw_scope, raw_rule in mapping.items():
        scope = _string(raw_scope, f"{label} key", maximum=4096)
        rule = _mapping(raw_rule, f"{label}[scope]")
        _exact_keys(
            rule,
            required={"source_kind", "target_wing"},
            label=f"{label}[scope]",
        )
        source_kind = _string(rule["source_kind"], "source_kind", maximum=16)
        if source_kind not in {"private", "group"}:
            raise MigrationPlanError("session source_kind must be private or group")
        parsed_scope_kind = _parse_session_scope_kind(scope)
        if parsed_scope_kind != source_kind:
            raise MigrationPlanError(
                "session source_kind must match the exact legacy access_scope"
            )
        wing, target_kind = _target_wing(rule["target_wing"], "target_wing")
        if target_kind != source_kind:
            raise MigrationPlanError(
                "session mapping cannot cross private/group boundaries"
            )
        parsed[scope] = (source_kind, wing)
    return parsed


def _inventory_session_db(
    *,
    source: Mapping[str, Any],
    namespace: str,
    relative: str,
    path: Path,
    fd: int,
) -> list[_InventoryRecord]:
    _exact_keys(
        source,
        required={"kind", "source_namespace", "path", "access_scope_map"},
        label=f"source {namespace}",
    )
    scope_map = _parse_scope_map(source["access_scope_map"], "access_scope_map")
    conn = _connect_sqlite(path, fd)
    records: list[_InventoryRecord] = []
    try:
        session_columns = _require_columns(conn, "sessions", {"id", "access_scope"})
        message_columns = _require_columns(conn, "messages", {"id", "session_id"})
        decisions: dict[str, tuple[str | None, str | None]] = {}
        for ordinal, row in enumerate(
            _bounded_rows(
                conn, table="sessions", columns=session_columns, order_by="id"
            )
        ):
            payload = _row_payload(row, session_columns)
            session_id = _string(row["id"], "session id", maximum=1024)
            raw_scope = row["access_scope"]
            mapping = scope_map.get(raw_scope) if isinstance(raw_scope, str) else None
            if raw_scope is None or raw_scope == "":
                target, reason = None, "legacy_null_access_scope"
            elif mapping is None:
                target, reason = None, "unmapped_access_scope"
            else:
                target, reason = mapping[1], None
            decisions[session_id] = (target, reason)
            records.append(
                _record(
                    namespace=namespace,
                    source_path=relative,
                    kind="session",
                    legacy_id=session_id,
                    payload=payload,
                    provenance={
                        "access_scope_sha256": _sha256_canonical(raw_scope),
                    },
                    target_wing=target,
                    quarantine_reason=reason,
                    locator={"table": "sessions", "ordinal": ordinal},
                )
            )
        for ordinal, row in enumerate(
            _bounded_rows(
                conn, table="messages", columns=message_columns, order_by="id"
            )
        ):
            payload = _row_payload(row, message_columns)
            session_id = str(row["session_id"])
            message_id = str(row["id"])
            if session_id not in decisions:
                target, reason = None, "orphan_session"
            else:
                target, reason = decisions[session_id]
            records.append(
                _record(
                    namespace=namespace,
                    source_path=relative,
                    kind="message",
                    legacy_id=f"{session_id}/{message_id}",
                    payload=payload,
                    provenance={
                        "session_id": session_id,
                    },
                    target_wing=target,
                    quarantine_reason=reason,
                    locator={
                        "table": "messages",
                        "ordinal": ordinal,
                    },
                )
            )
    finally:
        conn.close()
    return records


def _inventory_legacy_kg(
    *,
    source: Mapping[str, Any],
    namespace: str,
    relative: str,
    path: Path,
    fd: int,
) -> list[_InventoryRecord]:
    _exact_keys(
        source,
        required={"kind", "source_namespace", "path", "source_kind", "target_wing"},
        label=f"source {namespace}",
    )
    source_kind = _string(source["source_kind"], "source_kind", maximum=16)
    if source_kind not in {"private", "group"}:
        raise MigrationPlanError("legacy KG source_kind must be private or group")
    target, target_kind = _target_wing(source["target_wing"], "target_wing")
    if target_kind != source_kind:
        raise MigrationPlanError(
            "legacy KG mapping cannot cross private/group boundaries"
        )
    conn = _connect_sqlite(path, fd)
    records: list[_InventoryRecord] = []
    try:
        entity_columns = _require_columns(conn, "entities", {"id", "name", "type"})
        triple_columns = _require_columns(
            conn, "triples", {"id", "subject", "predicate", "object"}
        )
        for table, columns, record_kind in (
            ("entities", entity_columns, "kg_entity"),
            ("triples", triple_columns, "kg_triple"),
        ):
            for ordinal, row in enumerate(
                _bounded_rows(conn, table=table, columns=columns, order_by="id")
            ):
                complete_row = _row_payload(row, columns)
                legacy_id = _string(row["id"], f"{record_kind} id", maximum=1024)
                if record_kind == "kg_triple":
                    semantic_names = {
                        "subject",
                        "predicate",
                        "object",
                        "valid_from",
                        "valid_to",
                        "valid_until",
                    }
                else:
                    semantic_names = {"name", "type", "properties"}
                payload = {
                    key: value
                    for key, value in complete_row.items()
                    if key in semantic_names
                }
                provenance_fields = {
                    key: value
                    for key, value in complete_row.items()
                    if key != "id" and key not in semantic_names
                }
                records.append(
                    _record(
                        namespace=namespace,
                        source_path=relative,
                        kind=record_kind,
                        legacy_id=legacy_id,
                        payload=payload,
                        provenance={
                            "table": table,
                            "fields": provenance_fields,
                        },
                        target_wing=target,
                        quarantine_reason=None,
                        locator={"table": table, "ordinal": ordinal},
                    )
                )
    finally:
        conn.close()
    return records


def _parse_wing_map(value: Any) -> dict[str, tuple[str, str]]:
    mapping = _mapping(value, "wing_map")
    if len(mapping) > MAX_ROWS_PER_TABLE:
        raise MigrationPlanError("wing_map has too many entries")
    parsed: dict[str, tuple[str, str]] = {}
    for old_wing, raw_rule in mapping.items():
        old = _string(old_wing, "wing_map key", maximum=128)
        rule = _mapping(raw_rule, "wing_map rule")
        _exact_keys(
            rule,
            required={"source_kind", "target_wing"},
            label="wing_map rule",
        )
        source_kind = _string(rule["source_kind"], "source_kind", maximum=16)
        if source_kind not in {"private", "group"}:
            raise MigrationPlanError("wing source_kind must be private or group")
        target, target_kind = _target_wing(rule["target_wing"], "target_wing")
        if target_kind != source_kind:
            raise MigrationPlanError(
                "service wing mapping cannot cross private/group boundaries"
            )
        parsed[old] = (source_kind, target)
    return parsed


def _inventory_service_export(
    *,
    source: Mapping[str, Any],
    namespace: str,
    relative: str,
    path: Path,
    fd: int,
) -> list[_InventoryRecord]:
    _exact_keys(
        source,
        required={"kind", "source_namespace", "path", "wing_map"},
        label=f"source {namespace}",
    )
    wing_map = _parse_wing_map(source["wing_map"])
    del path
    raw, _, _ = _fd_bytes(fd, maximum=MAX_EXPORT_BYTES, label="MemPalace export")
    document = _mapping(
        _decode_json(raw, max_bytes=MAX_EXPORT_BYTES, label="MemPalace export"),
        "MemPalace export",
    )
    _exact_keys(document, required={"format", "records"}, label="MemPalace export")
    if document["format"] != SERVICE_EXPORT_FORMAT:
        raise MigrationPlanError("unsupported MemPalace export format")
    raw_records = _list(
        document["records"], "MemPalace export records", maximum=MAX_ROWS_PER_TABLE
    )
    records: list[_InventoryRecord] = []
    for index, raw in enumerate(raw_records):
        item = _mapping(raw, f"MemPalace export record {index}")
        _exact_keys(
            item,
            required={"kind", "id", "wing", "payload", "provenance"},
            label=f"MemPalace export record {index}",
        )
        kind = _string(item["kind"], "service record kind", maximum=32)
        if kind not in _SERVICE_RECORD_KINDS:
            raise MigrationPlanError("unsupported MemPalace export record kind")
        legacy_id = _string(item["id"], "service record id", maximum=1024)
        wing = item["wing"]
        if wing is None or wing == "":
            target, reason = None, "legacy_null_wing"
        elif not isinstance(wing, str) or wing not in wing_map:
            target, reason = None, "unmapped_legacy_wing"
        else:
            target, reason = wing_map[wing][1], None
        payload = _canonical_value(item["payload"])
        provenance = _canonical_value(item["provenance"])
        if len(_canonical_bytes(payload)) > MAX_ROW_BYTES:
            raise MigrationPlanError(
                "MemPalace export record exceeds the row size limit"
            )
        records.append(
            _record(
                namespace=namespace,
                source_path=relative,
                kind=kind,
                legacy_id=legacy_id,
                payload=payload,
                provenance={
                    "old_wing_sha256": _sha256_canonical(wing),
                    "record": provenance,
                },
                target_wing=target,
                quarantine_reason=reason,
                locator={"record_index": index, "kind": kind},
            )
        )
    return records


def _load_receipt(
    root: Path, value: Any
) -> tuple[dict[str, tuple[str, int]], dict[str, str]]:
    config = _mapping(value, "backup_receipt")
    _exact_keys(
        config,
        required={"path", "sha256"},
        label="backup_receipt",
    )
    relative = _relative_path(config["path"], "backup_receipt.path")
    if not relative.startswith("metadata/"):
        raise MigrationPlanError("backup receipt must be under snapshot metadata/")
    expected = _sha256(config["sha256"], "backup_receipt.sha256")
    receipt_fd, _ = _open_snapshot_fd(root, relative, "backup receipt")
    try:
        raw, actual, _ = _fd_bytes(
            receipt_fd, maximum=MAX_RECEIPT_BYTES, label="backup receipt"
        )
        if actual != expected:
            raise MigrationPlanError("backup receipt checksum mismatch")
        receipt = _mapping(
            _decode_json(raw, max_bytes=MAX_RECEIPT_BYTES, label="backup receipt"),
            "backup receipt",
        )
        _verify_snapshot_path_identity(root, relative, receipt_fd, "backup receipt")
    finally:
        os.close(receipt_fd)
    _exact_keys(
        receipt,
        required={
            "format",
            "backup_id",
            "snapshot_complete",
            "sqlite_consistency",
            "files",
        },
        label="backup receipt",
    )
    if receipt["format"] != RECEIPT_FORMAT:
        raise MigrationPlanError("unsupported backup receipt format")
    backup_id = _string(receipt["backup_id"], "backup_id", maximum=128)
    if not _BACKUP_ID_RE.fullmatch(backup_id):
        raise MigrationPlanError("backup_id has unsafe characters")
    if receipt["snapshot_complete"] is not True:
        raise MigrationPlanError("backup receipt must mark the snapshot complete")
    if receipt["sqlite_consistency"] != "checkpointed-no-wal":
        raise MigrationPlanError(
            "backup receipt must attest checkpointed-no-wal SQLite"
        )
    files: dict[str, tuple[str, int]] = {}
    for index, raw in enumerate(
        _list(receipt["files"], "backup receipt files", maximum=MAX_RECEIPT_FILES)
    ):
        item = _mapping(raw, f"backup receipt files[{index}]")
        _exact_keys(
            item,
            required={"path", "sha256", "size"},
            label=f"backup receipt files[{index}]",
        )
        file_path = _relative_path(item["path"], "backup file path")
        digest = _sha256(item["sha256"], "backup file sha256")
        size = item["size"]
        if type(size) is not int or size < 0 or size > MAX_SQLITE_BYTES:
            raise MigrationPlanError("backup file size is invalid")
        if file_path in files:
            raise MigrationPlanError("backup receipt contains duplicate file paths")
        files[file_path] = (digest, size)
    return files, {
        "id_sha256": _manifest_ref("backup-id", backup_id),
        "receipt_path_sha256": _manifest_ref("receipt-path", relative),
        "receipt_sha256": actual,
    }


def _verify_source_file(
    *,
    root: Path,
    relative: str,
    kind: str,
    receipt_files: Mapping[str, tuple[str, int]],
) -> tuple[int, Path, str, int, tuple[int, ...]]:
    if relative not in receipt_files:
        raise MigrationPlanError("source is not covered by the verified backup receipt")
    fd, path = _open_snapshot_fd(root, relative, "source file")
    maximum = {
        "memory_file": MAX_MEMORY_FILE_BYTES,
        "session_db": MAX_SQLITE_BYTES,
        "legacy_kg_db": MAX_SQLITE_BYTES,
        "mempalace_export": MAX_EXPORT_BYTES,
    }[kind]
    try:
        digest, size = _fd_hash(fd, maximum=maximum, label="source file")
        expected_digest, expected_size = receipt_files[relative]
        if digest != expected_digest or size != expected_size:
            raise MigrationPlanError(
                "source file does not match the verified backup receipt"
            )
    except BaseException:
        os.close(fd)
        raise
    info = os.fstat(fd)
    identity = (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
    )
    return fd, path, digest, size, identity


def _reverify_source_file(
    *,
    root: Path,
    relative: str,
    path: Path,
    fd: int,
    digest: str,
    size: int,
    identity: tuple[int, ...],
    kind: str,
) -> None:
    maximum = {
        "memory_file": MAX_MEMORY_FILE_BYTES,
        "session_db": MAX_SQLITE_BYTES,
        "legacy_kg_db": MAX_SQLITE_BYTES,
        "mempalace_export": MAX_EXPORT_BYTES,
    }[kind]
    current_digest, current_size = _fd_hash(fd, maximum=maximum, label="source file")
    if current_digest != digest or current_size != size:
        raise MigrationPlanError("source file changed during planning")
    current = os.fstat(fd)
    current_identity = (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
        current.st_mode,
        current.st_nlink,
        current.st_uid,
    )
    if current_identity != identity:
        raise MigrationPlanError("source file metadata changed during planning")
    if kind in {"session_db", "legacy_kg_db"}:
        _ensure_checkpointed_sqlite(path)
    _verify_snapshot_path_identity(root, relative, fd, "source file")


def _collisions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(record["kind"], record["legacy_id_sha256"])].append(record)
    result: list[dict[str, Any]] = []
    for (kind, legacy_id_sha256), members in sorted(grouped.items()):
        if len(members) < 2:
            continue
        content_digests = {member["content_sha256"] for member in members}
        provenance_digests = {member["provenance_sha256"] for member in members}
        targets = {member["target_wing"] for member in members}
        if len(content_digests) > 1:
            classification = "semantic_conflict"
        elif len(targets) > 1:
            classification = "destination_conflict"
        elif len(provenance_digests) > 1:
            classification = "provenance_conflict"
        else:
            classification = "identical_duplicate"
        result.append({
            "kind": kind,
            "legacy_id_sha256": legacy_id_sha256,
            "classification": classification,
            "requires_operator_resolution": True,
            "members": sorted(
                (
                    {
                        "source_namespace_sha256": member["source_namespace_sha256"],
                        "content_sha256": member["content_sha256"],
                        "provenance_sha256": member["provenance_sha256"],
                        "target_wing": member["target_wing"],
                    }
                    for member in members
                ),
                key=lambda item: (
                    item["source_namespace_sha256"],
                    item["content_sha256"],
                    item["provenance_sha256"],
                    item["target_wing"] or "",
                ),
            ),
        })
    return result


def _load_migration_config(config_path: Path) -> Mapping[str, Any]:
    original = Path(config_path).expanduser()
    if not original.is_absolute():
        original = Path.cwd() / original
    if original.is_symlink():
        raise MigrationPlanError("migration config must not be a symlink")
    try:
        parent = original.parent.resolve(strict=True)
        parent_info = parent.stat()
    except OSError as exc:
        raise MigrationPlanError("cannot resolve migration config parent") from exc
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(parent_info.st_mode) & 0o077
    ):
        raise MigrationPlanError(
            "migration config parent must be owner-controlled and private"
        )
    config_file = parent / original.name
    try:
        fd = os.open(config_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise MigrationPlanError("cannot securely open migration config") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise MigrationPlanError(
                "migration config must be an owner-controlled private file"
            )
        raw, _, _ = _fd_bytes(fd, maximum=MAX_CONFIG_BYTES, label="migration config")
    finally:
        os.close(fd)
    return _mapping(
        _decode_json(raw, max_bytes=MAX_CONFIG_BYTES, label="migration config"),
        "migration config",
    )


def _build_migration_manifest(
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], Path]:
    _exact_keys(
        config,
        required={"schema_version", "snapshot_root", "backup_receipt", "sources"},
        label="migration config",
    )
    if (
        config["schema_version"] != SCHEMA_VERSION
        or type(config["schema_version"]) is not int
    ):
        raise MigrationPlanError("unsupported migration config schema_version")
    root = _snapshot_root(config["snapshot_root"])
    receipt_files, backup = _load_receipt(root, config["backup_receipt"])
    raw_sources = _list(config["sources"], "sources", maximum=MAX_SOURCES)
    if not raw_sources:
        raise MigrationPlanError("sources must not be empty")

    seen_namespaces: set[str] = set()
    seen_paths: set[str] = set()
    inventory: list[_InventoryRecord] = []
    source_summaries: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_sources):
        source = _mapping(raw, f"sources[{index}]")
        kind = _string(source.get("kind"), f"sources[{index}].kind", maximum=32)
        if kind not in _SOURCE_KINDS:
            raise MigrationPlanError(f"sources[{index}].kind is unsupported")
        namespace = _source_namespace(
            source.get("source_namespace"), f"sources[{index}].source_namespace"
        )
        relative = _relative_path(source.get("path"), f"sources[{index}].path")
        if namespace in seen_namespaces:
            raise MigrationPlanError("source_namespace values must be unique")
        if relative in seen_paths:
            raise MigrationPlanError(
                "a snapshot source path may be inventoried only once"
            )
        seen_namespaces.add(namespace)
        seen_paths.add(relative)
        (
            source_fd,
            path,
            verified_digest,
            verified_size,
            verified_identity,
        ) = _verify_source_file(
            root=root, relative=relative, kind=kind, receipt_files=receipt_files
        )
        try:
            before = len(inventory)
            if kind == "memory_file":
                inventory.extend(
                    _inventory_memory_file(
                        source=source,
                        namespace=namespace,
                        relative=relative,
                        path=path,
                        fd=source_fd,
                    )
                )
            elif kind == "session_db":
                inventory.extend(
                    _inventory_session_db(
                        source=source,
                        namespace=namespace,
                        relative=relative,
                        path=path,
                        fd=source_fd,
                    )
                )
            elif kind == "legacy_kg_db":
                inventory.extend(
                    _inventory_legacy_kg(
                        source=source,
                        namespace=namespace,
                        relative=relative,
                        path=path,
                        fd=source_fd,
                    )
                )
            else:
                inventory.extend(
                    _inventory_service_export(
                        source=source,
                        namespace=namespace,
                        relative=relative,
                        path=path,
                        fd=source_fd,
                    )
                )
            _reverify_source_file(
                root=root,
                relative=relative,
                path=path,
                fd=source_fd,
                digest=verified_digest,
                size=verified_size,
                identity=verified_identity,
                kind=kind,
            )
        finally:
            os.close(source_fd)
        namespace_ref = _manifest_ref("source-namespace", namespace)
        source_summaries.append({
            "kind": kind,
            "source_namespace_sha256": namespace_ref,
            "source_path_sha256": _manifest_ref("source-path", relative),
            "record_count": len(inventory) - before,
        })

    records = sorted(
        (record.as_manifest_record() for record in inventory),
        key=lambda item: (
            item["source_namespace_sha256"],
            item["kind"],
            item["legacy_id_sha256"],
            item["content_sha256"],
        ),
    )
    counts = Counter(
        (record["action"], record["kind"], record["target_wing"] or "quarantine")
        for record in records
    )
    summary = [
        {"action": key[0], "kind": key[1], "destination": key[2], "count": count}
        for key, count in sorted(counts.items())
    ]
    config_identity = {
        "schema_version": SCHEMA_VERSION,
        "backup": backup,
        "sources": sorted(
            source_summaries, key=lambda item: item["source_namespace_sha256"]
        ),
        "records": [record["idempotency_key"] for record in records],
    }
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry-run-only",
        "migration_id": _sha256_canonical([
            "hermes-memory-migration-v1",
            config_identity,
        ]),
        "backup": backup,
        "sources": sorted(
            source_summaries, key=lambda item: item["source_namespace_sha256"]
        ),
        "records": records,
        "counts": summary,
        "collisions": _collisions(records),
        "future_apply_preconditions": [
            "independent manifest review accepted",
            "backup receipt and every source checksum reverified",
            "migration_id and per-record idempotency keys unchanged",
            "every collision group explicitly resolved without automatic dedupe",
            "MemPalace runtime ACL and sealed ingest pipeline accepted",
            "explicit operator approval obtained",
        ],
        "rollback": {
            "strategy": "disable-memory-migration-and-restore-verified-backup",
            "backup_id_sha256": backup["id_sha256"],
            "destructive_sql_allowed": False,
        },
    }
    manifest["manifest_sha256"] = _sha256_canonical(manifest)
    return manifest, root


def _plan_migration(config_path: Path) -> tuple[dict[str, Any], Path]:
    return _build_migration_manifest(_load_migration_config(config_path))


def build_migration_manifest(config_path: Path) -> dict[str, Any]:
    """Inventory a verified offline snapshot and return a deterministic plan."""
    manifest, _ = _plan_migration(config_path)
    return manifest


def manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    return _canonical_bytes(manifest) + b"\n"


def write_manifest(
    path: Path, manifest: Mapping[str, Any], *, snapshot_root: Path
) -> str:
    """Create a 0600 manifest or accept an existing byte-identical file."""
    output = Path(path).expanduser()
    if not output.is_absolute():
        output = (Path.cwd() / output).resolve(strict=False)
    parent = output.parent.resolve(strict=True)
    output = parent / output.name
    root = Path(snapshot_root).resolve(strict=True)
    if output == root or output.is_relative_to(root):
        raise MigrationPlanError("manifest output must be outside snapshot_root")
    parent_info = parent.stat()
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(parent_info.st_mode) & 0o077
    ):
        raise MigrationPlanError(
            "manifest output parent must be owner-controlled and private"
        )
    payload = manifest_bytes(manifest)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        parent_fd = os.open(parent, directory_flags)
    except OSError as exc:
        raise MigrationPlanError("cannot securely open manifest output parent") from exc
    try:
        opened_parent_info = os.fstat(parent_fd)
        if (
            (opened_parent_info.st_dev, opened_parent_info.st_ino)
            != (parent_info.st_dev, parent_info.st_ino)
            or opened_parent_info.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(opened_parent_info.st_mode) & 0o077
        ):
            raise MigrationPlanError("manifest output parent changed or is not private")
        try:
            fd = os.open(
                output.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | file_flags,
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            try:
                existing_fd = os.open(
                    output.name, os.O_RDONLY | file_flags, dir_fd=parent_fd
                )
            except OSError as exc:
                raise MigrationPlanError(
                    "cannot securely open existing manifest output"
                ) from exc
            try:
                info = os.fstat(existing_fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid not in {0, os.geteuid()}
                    or info.st_nlink != 1
                ):
                    raise MigrationPlanError(
                        "existing manifest output is not a trusted single-link file"
                    )
                if info.st_size != len(payload):
                    raise MigrationPlanError(
                        "refusing to overwrite a different manifest"
                    )
                current, _, _ = _fd_bytes(
                    existing_fd,
                    maximum=len(payload),
                    label="existing manifest output",
                )
                if current != payload:
                    raise MigrationPlanError(
                        "refusing to overwrite a different manifest"
                    )
                os.fchmod(existing_fd, 0o600)
                os.fsync(existing_fd)
                return "unchanged"
            finally:
                os.close(existing_fd)
        except OSError as exc:
            raise MigrationPlanError("cannot create manifest output") from exc
        created_info = os.fstat(fd)
        try:
            if (
                not stat.S_ISREG(created_info.st_mode)
                or created_info.st_uid != os.geteuid()
                or created_info.st_nlink != 1
            ):
                raise MigrationPlanError("new manifest output is not a trusted file")
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fchmod(handle.fileno(), 0o600)
                os.fsync(handle.fileno())
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                current = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (
                    created_info.st_dev,
                    created_info.st_ino,
                ):
                    os.unlink(output.name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        os.fsync(parent_fd)
        return "created"
    finally:
        os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a read-only MemPalace ACL migration manifest"
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest, root = _plan_migration(args.config)
        result = write_manifest(args.output, manifest, snapshot_root=root)
    except (MigrationPlanError, OSError):
        print("migration plan denied", file=sys.stderr)
        return 2
    migrated = sum(1 for item in manifest["records"] if item["action"] == "migrate")
    quarantined = len(manifest["records"]) - migrated
    print(
        f"dry-run manifest {result}: records={len(manifest['records'])} "
        f"migrate={migrated} quarantine={quarantined}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
