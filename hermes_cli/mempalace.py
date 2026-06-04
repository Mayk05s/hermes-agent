"""Profile-scoped MemPalace storage helpers.

Hermes stores MemPalace data as a local SQLite-backed knowledge graph:

    <HERMES_HOME>/mempalace/mcp-storage/<palace>/mempalace/knowledge_graph.sqlite3

The helpers in this module are intentionally dependency-light so both the
dashboard plugin and the runtime memory provider can share the same code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


PALACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TITLE_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")
_TERM_RE = re.compile(r"[\wЀ-ӿ-]+", re.UNICODE)
_ACCESS_SCOPE_RE = re.compile(
    r"^agent:[^:]+:(?P<platform>[^:]+):(?P<chat_type>[^:]+):(?P<chat_id>[^:]+)(?::(?P<thread_id>[^:]+))?"
)
_MENTION_RE = re.compile(r"\[([^\]\|\n]{2,80})(?:\|[^\]\n]+)?\]")
_CODE_ENTITY_RE = re.compile(r"`([^`\n]{2,80})`|(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)")
_NAMED_ENTITY_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9_.-]{2,}(?:\s+[A-Z][A-Za-z0-9_.-]{2,}){0,2}|"
    r"[А-ЯЁ][А-Яа-яЁё0-9_.-]{2,}(?:\s+[А-ЯЁ][А-Яа-яЁё0-9_.-]{2,}){0,2})\b"
)
_HISTORY_IMPORTANCE_RE = re.compile(
    r"(?:"
    r"важн|запомн|помни|нужно|надо|хочу|долж|нельзя|не\s+делай|предпочита|"
    r"люблю|ненавиж|ошиб|исправ|проблем|план|задач|проект|реализ|сделай|"
    r"удали|пересоб|обнов|используй|профайл|памят|memory|mempalace|"
    r"important|remember|prefer|must|should|need|want|fix|bug|project|profile"
    r")",
    re.IGNORECASE,
)
_HISTORY_SKIP_RE = re.compile(
    r"(?:ответьте\s+«?(?:принял|сделал)|подтвержд(?:ите|ение)|повторное\s+напоминание|"
    r"reminder|reply\s+['\"]?(?:done|yes)|cron|chainremind)",
    re.IGNORECASE,
)
_ENTITY_STOPWORDS = {
    "replying",
    "telegram",
    "media",
    "user",
    "assistant",
    "system",
    "сэр",
    "ответ",
    "сообщение",
    "пользователь",
    "задача",
}
LOW_SIGNAL_PREDICATES = {
    "status",
    "state",
    "enabled",
    "deleted",
    "reported",
    "updated",
    "closed",
    "ready",
    "done",
    "prepared",
    "uses_model",
    "model",
}
AUTO_CLEAN_MAX_DELETE = 250
AUTO_REFRESH_INTERVAL_SECONDS = 15 * 60
HISTORY_MAX_SESSIONS_PER_PROFILE = 2000
HISTORY_MAX_MESSAGES_PER_SESSION = 24
HISTORY_MAX_FACTS_PER_PROFILE = 10000
_TRANSIENT_VALUE_RE = re.compile(
    r"(?:готов|ready|sonnet|true|false|ok|done|closed|deleted|working|not[_ -]?responding)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PalacePaths:
    profile: str
    profile_home: Path
    storage_root: Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stable_id(prefix: str, *parts: object, max_len: int = 96) -> str:
    raw = "\0".join(str(p) for p in parts)
    digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
    slug_src = str(parts[0]) if parts else prefix
    slug = re.sub(r"[^a-z0-9]+", "_", slug_src.lower()).strip("_")[:48]
    return f"{prefix}_{slug}_{digest}"[:max_len]


def validate_palace_name(name: str) -> str:
    name = (name or "").strip()
    if not PALACE_NAME_RE.match(name):
        raise ValueError(f"Invalid palace name: {name!r}")
    return name


def resolve_profile_home(profile: str = "default") -> tuple[str, Path]:
    from hermes_cli import profiles as profiles_mod

    canonical = profiles_mod.normalize_profile_name(profile or "default")
    profiles_mod.validate_profile_name(canonical)
    if not profiles_mod.profile_exists(canonical):
        raise FileNotFoundError(f"Profile {canonical!r} does not exist")
    return canonical, Path(profiles_mod.resolve_profile_env(canonical)).resolve()


def palace_paths(
    profile: str = "default",
    *,
    profile_home: Optional[Path] = None,
) -> PalacePaths:
    if profile_home is None:
        canonical, home = resolve_profile_home(profile)
    else:
        canonical = profile
        home = Path(profile_home).resolve()
    return PalacePaths(
        profile=canonical,
        profile_home=home,
        storage_root=home / "mempalace" / "mcp-storage",
    )


def default_import_root() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "mempalace" / "imports"


def _default_profile_home() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root().resolve()


def list_profiles() -> list[dict[str, Any]]:
    from hermes_cli import profiles as profiles_mod

    rows = []
    for info in profiles_mod.list_profiles():
        name = getattr(info, "name", "default")
        path = Path(getattr(info, "path", profiles_mod.get_profile_dir(name)))
        rows.append(
            {
                "name": name,
                "path": str(path),
                "is_default": bool(getattr(info, "is_default", name == "default")),
            }
        )
    return rows


def list_import_snapshots(import_root: Optional[Path] = None) -> list[dict[str, Any]]:
    root = Path(import_root) if import_root else default_import_root()
    if not root.exists():
        return []
    snapshots: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), reverse=True):
        storage = child / "mcp-storage"
        if not storage.is_dir():
            continue
        palaces = [
            p.name
            for p in storage.iterdir()
            if (p / "mempalace" / "knowledge_graph.sqlite3").is_file()
        ]
        snapshots.append(
            {
                "name": child.name,
                "path": str(child),
                "palace_count": len(palaces),
                "palaces": sorted(palaces),
            }
        )
    return snapshots


def _db_path(paths: PalacePaths, palace: str) -> Path:
    return paths.storage_root / validate_palace_name(palace) / "mempalace" / "knowledge_graph.sqlite3"


def _ensure_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT DEFAULT 'unknown',
                properties TEXT DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS triples (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                confidence REAL DEFAULT 1.0,
                source_closet TEXT,
                source_file TEXT,
                source_drawer_id TEXT,
                adapter_name TEXT,
                extracted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (subject) REFERENCES entities(id),
                FOREIGN KEY (object) REFERENCES entities(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_triples_valid ON triples(valid_from, valid_to)")


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _connect_rw(db_path: Path) -> sqlite3.Connection:
    _ensure_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _decode_properties(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _query_terms(query: str) -> list[str]:
    terms = [t.lower() for t in _TERM_RE.findall(query or "") if len(t.strip("-_")) >= 2]
    if not terms and query.strip():
        terms = [query.lower().strip()]
    return terms[:8]


def _entity_dict(row: sqlite3.Row, degree: int = 0) -> dict[str, Any]:
    props = _decode_properties(row["properties"] if "properties" in row.keys() else "{}")
    return {
        "id": row["id"],
        "label": row["name"],
        "type": row["type"] or "unknown",
        "description": props.get("description", ""),
        "properties": props,
        "degree": degree,
        "created_at": row["created_at"] if "created_at" in row.keys() else None,
        "attributes": [],
    }


def _noise_candidate_for_entity(conn: sqlite3.Connection, row: sqlite3.Row) -> Optional[dict[str, Any]]:
    entity_id = row["id"]
    if (row["type"] or "unknown") == "unknown":
        return None
    props = _decode_properties(row["properties"] if "properties" in row.keys() else "{}")
    triples = conn.execute(
        """
        SELECT id, subject, predicate, object, confidence, source_closet,
               source_file, source_drawer_id, adapter_name, extracted_at
        FROM triples
        WHERE subject = ? OR object = ?
        ORDER BY extracted_at DESC
        LIMIT 20
        """,
        (entity_id, entity_id),
    ).fetchall()
    if not triples:
        return None

    predicates = [str(item["predicate"] or "").lower() for item in triples]
    only_low_signal = bool(predicates) and all(item in LOW_SIGNAL_PREDICATES for item in predicates)
    has_strong_relation = any(item not in LOW_SIGNAL_PREDICATES for item in predicates)
    has_source_file = bool(props.get("source_file")) or any(item["source_file"] for item in triples)
    transient_literal = any(
        item["subject"] == entity_id
        and str(item["predicate"] or "").lower() in LOW_SIGNAL_PREDICATES
        and _TRANSIENT_VALUE_RE.search(str(item["object"] or ""))
        for item in triples
    )
    degree = len(triples)

    reason = ""
    if degree <= 2 and only_low_signal and not has_strong_relation and not has_source_file:
        reason = "only transient status facts"
    elif degree <= 1 and transient_literal and not has_source_file:
        reason = "single transient status"
    if not reason:
        return None

    sources = sorted(
        {
            str(item["source_file"] or item["source_closet"] or item["adapter_name"] or "").strip()
            for item in triples
            if str(item["source_file"] or item["source_closet"] or item["adapter_name"] or "").strip()
        }
    )
    return {
        "id": entity_id,
        "label": row["name"],
        "type": row["type"] or "unknown",
        "description": props.get("description", ""),
        "degree": degree,
        "reason": reason,
        "predicates": sorted(set(predicates)),
        "sources": sources,
        "created_at": row["created_at"] if "created_at" in row.keys() else None,
        "triples": [
            {
                "id": item["id"],
                "subject": item["subject"],
                "predicate": item["predicate"],
                "object": item["object"],
                "source_closet": item["source_closet"],
                "source_file": item["source_file"],
                "adapter_name": item["adapter_name"],
                "extracted_at": item["extracted_at"],
            }
            for item in triples[:6]
        ],
    }


def palace_stats(
    palace: str,
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
) -> dict[str, Any]:
    paths = palace_paths(profile, profile_home=profile_home)
    db_path = _db_path(paths, palace)
    if not db_path.exists():
        return {
            "palace": palace,
            "exists": False,
            "node_count": 0,
            "edge_count": 0,
            "entity_count": 0,
            "triple_count": 0,
            "db_path": str(db_path),
        }
    with _connect_readonly(db_path) as conn:
        entity_count = int(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0])
        triple_count = int(conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0])
        node_count = int(
            conn.execute("SELECT COUNT(*) FROM entities WHERE type != 'unknown'").fetchone()[0]
        )
        edge_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM triples t
                JOIN entities s ON s.id = t.subject AND s.type != 'unknown'
                JOIN entities o ON o.id = t.object AND o.type != 'unknown'
                """
            ).fetchone()[0]
        )
        type_counts = [
            {"type": row["type"] or "unknown", "count": int(row["count"])}
            for row in conn.execute(
                "SELECT type, COUNT(*) AS count FROM entities GROUP BY type ORDER BY count DESC"
            )
        ]
        source_counts = [
            {"source": row["source"] or "(empty)", "count": int(row["count"])}
            for row in conn.execute(
                """
                SELECT COALESCE(adapter_name, source_closet, source_file, '') AS source,
                       COUNT(*) AS count
                FROM triples
                GROUP BY source
                ORDER BY count DESC
                LIMIT 12
                """
            )
        ]
    stat = db_path.stat()
    return {
        "palace": palace,
        "exists": True,
        "entity_count": entity_count,
        "triple_count": triple_count,
        "node_count": node_count,
        "edge_count": edge_count,
        "type_counts": type_counts,
        "source_counts": source_counts,
        "db_path": str(db_path),
        "bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def list_palaces(
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    include_stats: bool = True,
) -> list[dict[str, Any]]:
    paths = palace_paths(profile, profile_home=profile_home)
    if not paths.storage_root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for child in sorted(paths.storage_root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        db_path = child / "mempalace" / "knowledge_graph.sqlite3"
        if not db_path.is_file():
            continue
        if include_stats:
            rows.append(palace_stats(child.name, profile=profile, profile_home=paths.profile_home))
        else:
            rows.append({"palace": child.name, "db_path": str(db_path)})
    return rows


def profile_stats(
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
) -> dict[str, Any]:
    rows = list_palaces(profile=profile, profile_home=profile_home)
    type_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    for row in rows:
        for item in row.get("type_counts", []):
            key = str(item.get("type") or "unknown")
            type_counts[key] = type_counts.get(key, 0) + int(item.get("count") or 0)
        for item in row.get("source_counts", []):
            key = str(item.get("source") or "(empty)")
            source_counts[key] = source_counts.get(key, 0) + int(item.get("count") or 0)

    return {
        "palace": "",
        "label": "All profile memory",
        "exists": bool(rows),
        "topic_count": len(rows),
        "entity_count": sum(int(row.get("entity_count") or 0) for row in rows),
        "triple_count": sum(int(row.get("triple_count") or 0) for row in rows),
        "node_count": sum(int(row.get("node_count") or 0) for row in rows),
        "edge_count": sum(int(row.get("edge_count") or 0) for row in rows),
        "type_counts": [
            {"type": key, "count": value}
            for key, value in sorted(type_counts.items(), key=lambda item: item[1], reverse=True)
        ],
        "source_counts": [
            {"source": key, "count": value}
            for key, value in sorted(source_counts.items(), key=lambda item: item[1], reverse=True)[:12]
        ],
    }


def profile_matrix() -> dict[str, Any]:
    profiles = list_profiles()
    palace_names: set[str] = set()
    by_profile: dict[str, dict[str, Any]] = {}
    totals: dict[str, dict[str, int]] = {}
    for profile in profiles:
        name = str(profile["name"])
        rows = list_palaces(profile=name)
        by_profile[name] = {row["palace"]: row for row in rows}
        palace_names.update(row["palace"] for row in rows)
        totals[name] = {
            "palaces": len(rows),
            "entities": sum(int(row.get("entity_count") or 0) for row in rows),
            "triples": sum(int(row.get("triple_count") or 0) for row in rows),
        }

    palaces: list[dict[str, Any]] = []
    for palace in sorted(palace_names):
        cells = {}
        present = []
        for profile in profiles:
            name = str(profile["name"])
            row = by_profile[name].get(palace)
            if row:
                present.append(name)
                cells[name] = {
                    "exists": True,
                    "entities": int(row.get("entity_count") or 0),
                    "triples": int(row.get("triple_count") or 0),
                    "sources": row.get("source_counts", [])[:4],
                }
            else:
                cells[name] = {"exists": False, "entities": 0, "triples": 0, "sources": []}
        palaces.append(
            {
                "palace": palace,
                "profiles": present,
                "shared": len(present) > 1,
                "cells": cells,
            }
        )

    return {
        "profiles": profiles,
        "totals": totals,
        "palaces": palaces,
    }


def scan_noise(
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    palace: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    paths = palace_paths(profile, profile_home=profile_home)
    palace_filter = (palace or "").strip()
    palace_rows = (
        [{"palace": validate_palace_name(palace_filter)}]
        if palace_filter
        else list_palaces(profile=paths.profile, profile_home=paths.profile_home, include_stats=False)
    )
    limit = max(1, min(int(limit or 200), 2000))
    candidates: list[dict[str, Any]] = []
    total = 0
    by_palace: list[dict[str, Any]] = []

    for row in palace_rows:
        palace_name = row["palace"]
        db_path = _db_path(paths, palace_name)
        if not db_path.is_file():
            continue
        palace_count = 0
        with _connect_readonly(db_path) as conn:
            entity_rows = conn.execute(
                """
                SELECT id, name, type, properties, created_at
                FROM entities
                WHERE type != 'unknown'
                ORDER BY created_at DESC, name COLLATE NOCASE ASC
                """
            ).fetchall()
            for entity in entity_rows:
                candidate = _noise_candidate_for_entity(conn, entity)
                if not candidate:
                    continue
                palace_count += 1
                total += 1
                if len(candidates) < limit:
                    candidates.append({**candidate, "palace": palace_name})
        by_palace.append({"palace": palace_name, "candidates": palace_count})

    return {
        "profile": paths.profile,
        "palace": palace_filter,
        "total_candidates": total,
        "returned": len(candidates),
        "limited": total > len(candidates),
        "by_palace": by_palace,
        "candidates": candidates,
    }


def clean_noise(
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    palace: str = "",
    dry_run: bool = True,
    backup: bool = True,
    max_delete: int = 250,
) -> dict[str, Any]:
    paths = palace_paths(profile, profile_home=profile_home)
    max_delete = max(1, min(int(max_delete or 250), 5000))
    preview = scan_noise(
        profile=paths.profile,
        profile_home=paths.profile_home,
        palace=palace,
        limit=max_delete + 1,
    )
    candidates = preview["candidates"]
    total = int(preview["total_candidates"])
    if dry_run:
        return {**preview, "dry_run": True, "deleted_entities": 0, "deleted_triples": 0, "deleted_orphans": 0, "backup_root": ""}
    if total > max_delete:
        raise ValueError(f"Refusing to delete {total} candidates; max_delete is {max_delete}")

    by_palace: dict[str, list[str]] = {}
    for item in candidates:
        by_palace.setdefault(item["palace"], []).append(item["id"])

    backup_root: Optional[Path] = None
    if backup:
        backup_root = paths.profile_home / "mempalace" / "backups" / f"noise-clean-{utc_now().replace(':', '').replace('+', 'Z')}"
        backup_root.mkdir(parents=True, exist_ok=True)

    deleted_entities = 0
    deleted_triples = 0
    deleted_orphans = 0
    cleaned_palaces: list[dict[str, Any]] = []
    for palace_name, entity_ids in sorted(by_palace.items()):
        db_path = _db_path(paths, palace_name)
        if backup_root is not None:
            dest = backup_root / f"{palace_name}.knowledge_graph.sqlite3"
            shutil.copy2(db_path, dest)
        placeholders = ",".join(["?"] * len(entity_ids))
        with _connect_rw(db_path) as conn:
            triple_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM triples WHERE subject IN ({placeholders}) OR object IN ({placeholders})",
                    [*entity_ids, *entity_ids],
                ).fetchone()[0]
            )
            conn.execute(
                f"DELETE FROM triples WHERE subject IN ({placeholders}) OR object IN ({placeholders})",
                [*entity_ids, *entity_ids],
            )
            entity_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM entities WHERE id IN ({placeholders})",
                    entity_ids,
                ).fetchone()[0]
            )
            conn.execute(f"DELETE FROM entities WHERE id IN ({placeholders})", entity_ids)
            orphan_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM entities e
                    WHERE e.type = 'unknown'
                      AND NOT EXISTS (SELECT 1 FROM triples t WHERE t.subject = e.id OR t.object = e.id)
                    """
                ).fetchone()[0]
            )
            conn.execute(
                """
                DELETE FROM entities
                WHERE type = 'unknown'
                  AND NOT EXISTS (SELECT 1 FROM triples t WHERE t.subject = entities.id OR t.object = entities.id)
                """
            )
            conn.commit()
        deleted_entities += entity_count
        deleted_triples += triple_count
        deleted_orphans += orphan_count
        cleaned_palaces.append(
            {
                "palace": palace_name,
                "deleted_entities": entity_count,
                "deleted_triples": triple_count,
                "deleted_orphans": orphan_count,
            }
        )

    return {
        **preview,
        "dry_run": False,
        "deleted_entities": deleted_entities,
        "deleted_triples": deleted_triples,
        "deleted_orphans": deleted_orphans,
        "backup_root": str(backup_root) if backup_root else "",
        "cleaned_palaces": cleaned_palaces,
    }


def auto_clean_noise(
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    palace: str = "",
    enabled: bool = True,
    backup: bool = True,
    max_delete: int = AUTO_CLEAN_MAX_DELETE,
) -> dict[str, Any]:
    paths = palace_paths(profile, profile_home=profile_home)
    max_delete = max(1, min(int(max_delete or AUTO_CLEAN_MAX_DELETE), 5000))
    if not enabled:
        return {
            "profile": paths.profile,
            "palace": palace,
            "enabled": False,
            "automatic": True,
            "skipped": True,
            "skip_reason": "disabled",
            "total_candidates": 0,
            "returned": 0,
            "limited": False,
            "by_palace": [],
            "candidates": [],
            "dry_run": True,
            "deleted_entities": 0,
            "deleted_triples": 0,
            "deleted_orphans": 0,
            "backup_root": "",
            "cleaned_palaces": [],
        }

    try:
        preview = scan_noise(
            profile=paths.profile,
            profile_home=paths.profile_home,
            palace=palace,
            limit=max_delete + 1,
        )
        total = int(preview["total_candidates"])
        if total == 0:
            return {
                **preview,
                "enabled": True,
                "automatic": True,
                "skipped": False,
                "skip_reason": "",
                "dry_run": False,
                "deleted_entities": 0,
                "deleted_triples": 0,
                "deleted_orphans": 0,
                "backup_root": "",
                "cleaned_palaces": [],
            }
        if total > max_delete:
            return {
                **preview,
                "enabled": True,
                "automatic": True,
                "skipped": True,
                "skip_reason": f"refusing to delete {total} candidates; max_delete is {max_delete}",
                "dry_run": True,
                "deleted_entities": 0,
                "deleted_triples": 0,
                "deleted_orphans": 0,
                "backup_root": "",
                "cleaned_palaces": [],
            }
        return {
            **clean_noise(
                profile=paths.profile,
                profile_home=paths.profile_home,
                palace=palace,
                dry_run=False,
                backup=backup,
                max_delete=max_delete,
            ),
            "enabled": True,
            "automatic": True,
            "skipped": False,
            "skip_reason": "",
        }
    except Exception as exc:
        return {
            "profile": paths.profile,
            "palace": palace,
            "enabled": True,
            "automatic": True,
            "skipped": True,
            "skip_reason": str(exc),
            "total_candidates": 0,
            "returned": 0,
            "limited": False,
            "by_palace": [],
            "candidates": [],
            "dry_run": True,
            "deleted_entities": 0,
            "deleted_triples": 0,
            "deleted_orphans": 0,
            "backup_root": "",
            "cleaned_palaces": [],
        }


def auto_clean_palaces(
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    palaces: Optional[Iterable[str]] = None,
    enabled: bool = True,
    backup: bool = True,
    max_delete: int = AUTO_CLEAN_MAX_DELETE,
) -> list[dict[str, Any]]:
    paths = palace_paths(profile, profile_home=profile_home)
    names = sorted({validate_palace_name(str(item)) for item in (palaces or []) if str(item or "").strip()})
    if not names:
        return [
            auto_clean_noise(
                profile=paths.profile,
                profile_home=paths.profile_home,
                enabled=False,
                backup=backup,
                max_delete=max_delete,
            )
        ]
    return [
        auto_clean_noise(
            profile=paths.profile,
            profile_home=paths.profile_home,
            palace=name,
            enabled=enabled,
            backup=backup,
            max_delete=max_delete,
        )
        for name in names
    ]


def load_graph(
    palace: str = "",
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    query: str = "",
    node_limit: int = 180,
    edge_limit: int = 420,
    min_confidence: float = 0.0,
) -> dict[str, Any]:
    node_limit = max(10, min(int(node_limit or 180), 1000))
    edge_limit = max(10, min(int(edge_limit or 420), 3000))
    query = (query or "").strip()
    palace = (palace or "").strip()
    if not palace:
        return _load_profile_graph(
            profile=profile,
            profile_home=profile_home,
            query=query,
            node_limit=node_limit,
            edge_limit=edge_limit,
            min_confidence=min_confidence,
        )
    paths = palace_paths(profile, profile_home=profile_home)
    db_path = _db_path(paths, palace)
    with _connect_readonly(db_path) as conn:
        params: list[Any] = [min_confidence]
        where = "e.type != 'unknown'"
        terms = _query_terms(query)
        if terms:
            term_clauses = []
            for term in terms:
                like = f"%{term}%"
                term_clauses.append(
                    "(LOWER(e.name) LIKE ? OR LOWER(e.properties) LIKE ? "
                    "OR e.id IN (SELECT subject FROM triples WHERE LOWER(predicate) LIKE ? OR LOWER(object) LIKE ?))"
                )
                params.extend([like, like, like, like])
            where += " AND (" + " OR ".join(term_clauses) + ")"
        params.append(node_limit)
        entity_rows = conn.execute(
            f"""
            SELECT e.id, e.name, e.type, e.properties, e.created_at,
                   (
                     SELECT COUNT(*)
                     FROM triples t
                     WHERE (t.subject = e.id OR t.object = e.id)
                       AND COALESCE(t.confidence, 1.0) >= ?
                   ) AS degree
            FROM entities e
            WHERE {where}
            ORDER BY degree DESC, e.name COLLATE NOCASE ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        nodes = [_entity_dict(row, int(row["degree"] or 0)) for row in entity_rows]
        for node in nodes:
            node["palace"] = palace
        selected = {node["id"] for node in nodes}
        if not selected:
            return {
                "profile": paths.profile,
                "palace": palace,
                "nodes": [],
                "edges": [],
                "node_count": 0,
                "edge_count": 0,
                "stats": palace_stats(palace, profile=profile, profile_home=paths.profile_home),
            }

        placeholders = ",".join(["?"] * len(selected))
        edge_rows = conn.execute(
            f"""
            SELECT id, subject, predicate, object, confidence, valid_from, valid_to,
                   source_closet, source_file, source_drawer_id, adapter_name, extracted_at
            FROM triples
            WHERE subject IN ({placeholders})
              AND object IN ({placeholders})
              AND COALESCE(confidence, 1.0) >= ?
            ORDER BY COALESCE(confidence, 1.0) DESC, extracted_at DESC
            LIMIT ?
            """,
            [*selected, *selected, min_confidence, edge_limit],
        ).fetchall()
        edges = [
            {
                "id": row["id"],
                "source": row["subject"],
                "target": row["object"],
                "label": row["predicate"],
                "confidence": float(row["confidence"] if row["confidence"] is not None else 1.0),
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "source_closet": row["source_closet"],
                "source_file": row["source_file"],
                "source_drawer_id": row["source_drawer_id"],
                "adapter_name": row["adapter_name"],
                "extracted_at": row["extracted_at"],
                "palace": palace,
            }
            for row in edge_rows
        ]

        node_map = {node["id"]: node for node in nodes}
        attr_rows = conn.execute(
            f"""
            SELECT t.subject, t.predicate, t.object, t.confidence, t.source_file,
                   t.source_closet, t.adapter_name, t.extracted_at, e.name AS object_name,
                   e.type AS object_type
            FROM triples t
            LEFT JOIN entities e ON e.id = t.object
            WHERE t.subject IN ({placeholders})
              AND COALESCE(t.confidence, 1.0) >= ?
            ORDER BY t.extracted_at DESC
            LIMIT ?
            """,
            [*selected, min_confidence, max(edge_limit, node_limit * 5)],
        ).fetchall()
        for row in attr_rows:
            if row["object"] in selected and row["object_type"] != "unknown":
                continue
            node = node_map.get(row["subject"])
            if not node or len(node["attributes"]) >= 12:
                continue
            node["attributes"].append(
                {
                    "predicate": row["predicate"],
                    "value": row["object_name"] or row["object"],
                    "confidence": float(row["confidence"] if row["confidence"] is not None else 1.0),
                    "source_file": row["source_file"],
                    "source_closet": row["source_closet"],
                    "adapter_name": row["adapter_name"],
                    "extracted_at": row["extracted_at"],
                }
            )

    return {
        "profile": paths.profile,
        "palace": palace,
        "topic": palace,
        "nodes": nodes,
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "stats": palace_stats(palace, profile=profile, profile_home=paths.profile_home),
    }


def _prefix_graph_ids(graph: dict[str, Any], palace: str) -> dict[str, Any]:
    nodes = []
    for node in graph.get("nodes", []):
        copied = dict(node)
        copied["source_id"] = copied.get("id")
        copied["id"] = f"{palace}::{copied['source_id']}"
        copied["palace"] = palace
        nodes.append(copied)
    edges = []
    for edge in graph.get("edges", []):
        copied = dict(edge)
        copied["source_id"] = copied.get("source")
        copied["target_id"] = copied.get("target")
        copied["id"] = f"{palace}::{copied.get('id')}"
        copied["source"] = f"{palace}::{copied['source_id']}"
        copied["target"] = f"{palace}::{copied['target_id']}"
        copied["palace"] = palace
        edges.append(copied)
    return {**graph, "nodes": nodes, "edges": edges}


def _load_profile_graph(
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    query: str = "",
    node_limit: int = 180,
    edge_limit: int = 420,
    min_confidence: float = 0.0,
) -> dict[str, Any]:
    paths = palace_paths(profile, profile_home=profile_home)
    palaces = [row["palace"] for row in list_palaces(profile=profile, profile_home=paths.profile_home, include_stats=False)]
    if not palaces:
        return {
            "profile": paths.profile,
            "palace": "",
            "topic": "",
            "nodes": [],
            "edges": [],
            "node_count": 0,
            "edge_count": 0,
            "stats": profile_stats(profile=profile, profile_home=paths.profile_home),
        }

    per_palace_nodes = max(10, min(90, max(node_limit // max(len(palaces), 1) * 2, 24)))
    per_palace_edges = max(10, min(180, max(edge_limit // max(len(palaces), 1) * 2, 48)))
    merged_nodes: list[dict[str, Any]] = []
    merged_edges: list[dict[str, Any]] = []
    for palace_name in palaces:
        try:
            graph = load_graph(
                palace_name,
                profile=profile,
                profile_home=paths.profile_home,
                query=query,
                node_limit=per_palace_nodes,
                edge_limit=per_palace_edges,
                min_confidence=min_confidence,
            )
        except FileNotFoundError:
            continue
        prefixed = _prefix_graph_ids(graph, palace_name)
        merged_nodes.extend(prefixed.get("nodes", []))
        merged_edges.extend(prefixed.get("edges", []))

    merged_nodes.sort(key=lambda node: (int(node.get("degree") or 0), str(node.get("label") or "")), reverse=True)
    selected_nodes = merged_nodes[:node_limit]
    selected = {node["id"] for node in selected_nodes}
    selected_edges = [
        edge for edge in merged_edges
        if edge.get("source") in selected and edge.get("target") in selected
    ][:edge_limit]
    return {
        "profile": paths.profile,
        "palace": "",
        "topic": "",
        "nodes": selected_nodes,
        "edges": selected_edges,
        "node_count": len(selected_nodes),
        "edge_count": len(selected_edges),
        "stats": profile_stats(profile=profile, profile_home=paths.profile_home),
    }


def _find_entity_palace(paths: PalacePaths, entity_id: str) -> Optional[str]:
    for row in list_palaces(profile=paths.profile, profile_home=paths.profile_home, include_stats=False):
        db_path = _db_path(paths, row["palace"])
        if not db_path.exists():
            continue
        with _connect_readonly(db_path) as conn:
            found = conn.execute("SELECT 1 FROM entities WHERE id = ? LIMIT 1", (entity_id,)).fetchone()
        if found:
            return row["palace"]
    return None


def _tree_entity(row: sqlite3.Row, *, palace: str, aggregate_id: bool) -> dict[str, Any]:
    props = _decode_properties(row["properties"] if "properties" in row.keys() else "{}")
    entity_id = row["id"]
    return {
        "id": f"{palace}::{entity_id}" if aggregate_id else entity_id,
        "source_id": entity_id,
        "palace": palace,
        "label": row["name"],
        "type": row["type"] or "unknown",
        "description": props.get("description", ""),
        "properties": props,
        "created_at": row["created_at"] if "created_at" in row.keys() else None,
    }


def node_tree(
    node_id: str,
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    palace: str = "",
    depth: int = 2,
    limit: int = 80,
) -> dict[str, Any]:
    paths = palace_paths(profile, profile_home=profile_home)
    raw_id = (node_id or "").strip()
    palace_name = (palace or "").strip()
    aggregate_id = False
    if "::" in raw_id:
        prefix, raw_id = raw_id.split("::", 1)
        palace_name = palace_name or prefix
        aggregate_id = True
    if not raw_id:
        raise ValueError("node_id is required")
    if not palace_name:
        found = _find_entity_palace(paths, raw_id)
        if not found:
            raise FileNotFoundError(f"Entity {raw_id!r} not found in profile {paths.profile!r}")
        palace_name = found
    db_path = _db_path(paths, palace_name)
    depth = max(1, min(int(depth or 2), 4))
    remaining = [max(10, min(int(limit or 80), 300))]

    with _connect_readonly(db_path) as conn:
        root_row = conn.execute(
            "SELECT id, name, type, properties, created_at FROM entities WHERE id = ?",
            (raw_id,),
        ).fetchone()
        if root_row is None:
            raise FileNotFoundError(f"Entity {raw_id!r} not found in palace {palace_name!r}")

        def build(current_id: str, current_depth: int, seen: set[str]) -> list[dict[str, Any]]:
            if current_depth <= 0 or remaining[0] <= 0:
                return []
            rows = []
            outgoing = conn.execute(
                """
                SELECT 'out' AS direction, t.predicate, t.confidence, t.source_file,
                       t.source_closet, t.adapter_name, t.extracted_at,
                       e.id, e.name, e.type, e.properties, e.created_at
                FROM triples t
                LEFT JOIN entities e ON e.id = t.object
                WHERE t.subject = ?
                ORDER BY COALESCE(t.confidence, 1.0) DESC, t.extracted_at DESC
                LIMIT 60
                """,
                (current_id,),
            ).fetchall()
            incoming = conn.execute(
                """
                SELECT 'in' AS direction, t.predicate, t.confidence, t.source_file,
                       t.source_closet, t.adapter_name, t.extracted_at,
                       e.id, e.name, e.type, e.properties, e.created_at
                FROM triples t
                LEFT JOIN entities e ON e.id = t.subject
                WHERE t.object = ?
                ORDER BY COALESCE(t.confidence, 1.0) DESC, t.extracted_at DESC
                LIMIT 60
                """,
                (current_id,),
            ).fetchall()
            rows.extend(outgoing)
            rows.extend(incoming)
            result: list[dict[str, Any]] = []
            for row in rows:
                if remaining[0] <= 0:
                    break
                remaining[0] -= 1
                entity = None
                value = row["name"] or row["id"]
                child_id = row["id"]
                if child_id and row["type"] != "unknown":
                    entity = _tree_entity(row, palace=palace_name, aggregate_id=aggregate_id)
                    value = entity["label"]
                item = {
                    "direction": row["direction"],
                    "predicate": row["predicate"],
                    "confidence": float(row["confidence"] if row["confidence"] is not None else 1.0),
                    "value": value,
                    "entity": entity,
                    "source_file": row["source_file"],
                    "source_closet": row["source_closet"],
                    "adapter_name": row["adapter_name"],
                    "extracted_at": row["extracted_at"],
                    "children": [],
                }
                if entity and child_id not in seen:
                    item["children"] = build(child_id, current_depth - 1, seen | {child_id})
                result.append(item)
            return result

        return {
            "profile": paths.profile,
            "palace": palace_name,
            "root": _tree_entity(root_row, palace=palace_name, aggregate_id=aggregate_id),
            "tree": build(raw_id, depth, {raw_id}),
        }


def load_subgraph(
    center_id: str,
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    palace: str = "",
    depth: int = 1,
    node_limit: int = 180,
    edge_limit: int = 420,
    min_confidence: float = 0.0,
) -> dict[str, Any]:
    paths = palace_paths(profile, profile_home=profile_home)
    raw_id = (center_id or "").strip()
    palace_name = (palace or "").strip()
    aggregate_id = False
    if "::" in raw_id:
        prefix, raw_id = raw_id.split("::", 1)
        palace_name = palace_name or prefix
        aggregate_id = True
    if not raw_id:
        raise ValueError("center_id is required")
    if not palace_name:
        found = _find_entity_palace(paths, raw_id)
        if not found:
            raise FileNotFoundError(f"Entity {raw_id!r} not found in profile {paths.profile!r}")
        palace_name = found
    db_path = _db_path(paths, palace_name)
    depth = max(1, min(int(depth or 1), 3))
    node_limit = max(10, min(int(node_limit or 180), 1000))
    edge_limit = max(10, min(int(edge_limit or 420), 3000))

    with _connect_readonly(db_path) as conn:
        root = conn.execute(
            "SELECT id FROM entities WHERE id = ? AND type != 'unknown'",
            (raw_id,),
        ).fetchone()
        if root is None:
            raise FileNotFoundError(f"Entity {raw_id!r} not found in palace {palace_name!r}")

        selected = {raw_id}
        frontier = {raw_id}
        for _level in range(depth):
            if not frontier or len(selected) >= node_limit:
                break
            placeholders = ",".join(["?"] * len(frontier))
            rows = conn.execute(
                f"""
                SELECT DISTINCT other.id
                FROM (
                    SELECT t.object AS id
                    FROM triples t
                    JOIN entities e ON e.id = t.object AND e.type != 'unknown'
                    WHERE t.subject IN ({placeholders})
                      AND COALESCE(t.confidence, 1.0) >= ?
                    UNION
                    SELECT t.subject AS id
                    FROM triples t
                    JOIN entities e ON e.id = t.subject AND e.type != 'unknown'
                    WHERE t.object IN ({placeholders})
                      AND COALESCE(t.confidence, 1.0) >= ?
                ) other
                LIMIT ?
                """,
                [*frontier, min_confidence, *frontier, min_confidence, node_limit],
            ).fetchall()
            next_frontier = {row["id"] for row in rows if row["id"] not in selected}
            selected.update(list(next_frontier)[: max(0, node_limit - len(selected))])
            frontier = next_frontier

        placeholders = ",".join(["?"] * len(selected))
        entity_rows = conn.execute(
            f"""
            SELECT e.id, e.name, e.type, e.properties, e.created_at,
                   (
                     SELECT COUNT(*)
                     FROM triples t
                     WHERE (t.subject = e.id OR t.object = e.id)
                       AND COALESCE(t.confidence, 1.0) >= ?
                   ) AS degree
            FROM entities e
            WHERE e.id IN ({placeholders})
            ORDER BY degree DESC, e.name COLLATE NOCASE ASC
            """,
            [min_confidence, *selected],
        ).fetchall()
        nodes = [_entity_dict(row, int(row["degree"] or 0)) for row in entity_rows]
        for node in nodes:
            node["palace"] = palace_name
            if aggregate_id:
                node["source_id"] = node["id"]
                node["id"] = f"{palace_name}::{node['source_id']}"

        edge_rows = conn.execute(
            f"""
            SELECT id, subject, predicate, object, confidence, valid_from, valid_to,
                   source_closet, source_file, source_drawer_id, adapter_name, extracted_at
            FROM triples
            WHERE subject IN ({placeholders})
              AND object IN ({placeholders})
              AND COALESCE(confidence, 1.0) >= ?
            ORDER BY COALESCE(confidence, 1.0) DESC, extracted_at DESC
            LIMIT ?
            """,
            [*selected, *selected, min_confidence, edge_limit],
        ).fetchall()
        edges = []
        for row in edge_rows:
            source = row["subject"]
            target = row["object"]
            edge_id = row["id"]
            if aggregate_id:
                source = f"{palace_name}::{source}"
                target = f"{palace_name}::{target}"
                edge_id = f"{palace_name}::{edge_id}"
            edges.append(
                {
                    "id": edge_id,
                    "source": source,
                    "target": target,
                    "label": row["predicate"],
                    "confidence": float(row["confidence"] if row["confidence"] is not None else 1.0),
                    "valid_from": row["valid_from"],
                    "valid_to": row["valid_to"],
                    "source_closet": row["source_closet"],
                    "source_file": row["source_file"],
                    "source_drawer_id": row["source_drawer_id"],
                    "adapter_name": row["adapter_name"],
                    "extracted_at": row["extracted_at"],
                    "palace": palace_name,
                }
            )

        node_map = {node["source_id"] if aggregate_id else node["id"]: node for node in nodes}
        attr_rows = conn.execute(
            f"""
            SELECT t.subject, t.predicate, t.object, t.confidence, t.source_file,
                   t.source_closet, t.adapter_name, t.extracted_at, e.name AS object_name,
                   e.type AS object_type
            FROM triples t
            LEFT JOIN entities e ON e.id = t.object
            WHERE t.subject IN ({placeholders})
              AND COALESCE(t.confidence, 1.0) >= ?
            ORDER BY t.extracted_at DESC
            LIMIT ?
            """,
            [*selected, min_confidence, max(edge_limit, node_limit * 4)],
        ).fetchall()
        for row in attr_rows:
            if row["object"] in selected and row["object_type"] != "unknown":
                continue
            node = node_map.get(row["subject"])
            if not node or len(node["attributes"]) >= 12:
                continue
            node["attributes"].append(
                {
                    "predicate": row["predicate"],
                    "value": row["object_name"] or row["object"],
                    "confidence": float(row["confidence"] if row["confidence"] is not None else 1.0),
                    "source_file": row["source_file"],
                    "source_closet": row["source_closet"],
                    "adapter_name": row["adapter_name"],
                    "extracted_at": row["extracted_at"],
                }
            )

    return {
        "profile": paths.profile,
        "palace": "" if aggregate_id and not palace else palace_name,
        "topic": palace_name,
        "center_id": f"{palace_name}::{raw_id}" if aggregate_id else raw_id,
        "depth": depth,
        "nodes": nodes,
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "stats": palace_stats(palace_name, profile=profile, profile_home=paths.profile_home),
    }


def search(
    query: str,
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    palace: str = "",
    limit: int = 30,
) -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return {"query": "", "results": []}
    terms = _query_terms(query)
    if not terms:
        return {"query": query, "results": []}
    limit = max(1, min(int(limit or 30), 100))
    palaces = [validate_palace_name(palace)] if palace else [p["palace"] for p in list_palaces(profile=profile, profile_home=profile_home, include_stats=False)]
    results: list[dict[str, Any]] = []
    paths = palace_paths(profile, profile_home=profile_home)
    for palace_name in palaces:
        db_path = _db_path(paths, palace_name)
        if not db_path.exists():
            continue
        with _connect_readonly(db_path) as conn:
            entity_where = " OR ".join(["LOWER(name) LIKE ? OR LOWER(properties) LIKE ?" for _ in terms])
            entity_params: list[Any] = []
            for term in terms:
                like = f"%{term}%"
                entity_params.extend([like, like])
            entity_params.append(limit)
            for row in conn.execute(
                f"""
                SELECT id, name, type, properties, created_at
                FROM entities
                WHERE type != 'unknown' AND ({entity_where})
                ORDER BY name COLLATE NOCASE ASC
                LIMIT ?
                """,
                entity_params,
            ):
                props = _decode_properties(row["properties"])
                results.append(
                    {
                        "kind": "entity",
                        "palace": palace_name,
                        "id": row["id"],
                        "title": row["name"],
                        "subtitle": row["type"] or "unknown",
                        "snippet": props.get("description", ""),
                        "created_at": row["created_at"],
                    }
                )
            remaining = max(0, limit - len(results))
            if remaining:
                triple_where = " OR ".join(
                    [
                        "LOWER(t.predicate) LIKE ? OR LOWER(t.object) LIKE ? OR LOWER(COALESCE(o.name, '')) LIKE ?"
                        for _ in terms
                    ]
                )
                triple_params: list[Any] = []
                for term in terms:
                    like = f"%{term}%"
                    triple_params.extend([like, like, like])
                triple_params.append(remaining)
                for row in conn.execute(
                    f"""
                    SELECT t.id, t.subject, t.predicate, t.object, t.confidence,
                           t.source_file, t.source_closet, t.extracted_at,
                           s.name AS subject_name, o.name AS object_name
                    FROM triples t
                    LEFT JOIN entities s ON s.id = t.subject
                    LEFT JOIN entities o ON o.id = t.object
                    WHERE {triple_where}
                    ORDER BY t.extracted_at DESC
                    LIMIT ?
                    """,
                    triple_params,
                ):
                    results.append(
                        {
                            "kind": "triple",
                            "palace": palace_name,
                            "id": row["id"],
                            "title": row["subject_name"] or row["subject"],
                            "subtitle": row["predicate"],
                            "snippet": row["object_name"] or row["object"],
                            "confidence": float(row["confidence"] if row["confidence"] is not None else 1.0),
                            "source_file": row["source_file"],
                            "source_closet": row["source_closet"],
                            "created_at": row["extracted_at"],
                        }
                    )
            if len(results) >= limit:
                break
    return {"query": query, "profile": paths.profile, "palace": palace, "results": results[:limit]}


def recall_context(
    query: str,
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    palace: str = "",
    max_items: int = 12,
) -> str:
    data = search(query, profile=profile, profile_home=profile_home, palace=palace, limit=max_items)
    rows = data.get("results", [])
    if not rows:
        return ""
    lines = ["MemPalace recall:"]
    for row in rows[:max_items]:
        title = row.get("title") or row.get("id")
        subtitle = row.get("subtitle") or row.get("kind")
        snippet = (row.get("snippet") or "").strip()
        palace_name = row.get("palace") or palace
        if snippet:
            lines.append(f"- [{palace_name}] {title} / {subtitle}: {snippet}")
        else:
            lines.append(f"- [{palace_name}] {title} / {subtitle}")
    return "\n".join(lines)


def _memory_roots(profile_home: Path, tenant_roots: Optional[Iterable[Path]] = None) -> list[Path]:
    if tenant_roots is not None:
        return [Path(p) for p in tenant_roots]
    roots: list[Path] = []
    profile_tenants = profile_home / "tenants"
    if profile_tenants.is_dir():
        roots.append(profile_tenants)
    home_tenants = Path.home() / "tenants"
    if profile_home.resolve() == _default_profile_home() and home_tenants.is_dir() and home_tenants not in roots:
        roots.append(home_tenants)
    return roots


def _tenant_for_file(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    return rel.parts[0]


def _topic_for_file(path: Path) -> str:
    parts = list(path.parts)
    if "memory" not in parts:
        return path.stem
    idx = parts.index("memory")
    rel = Path(*parts[idx + 1 :])
    topic = str(rel.with_suffix(""))
    return topic.replace("_", " ").replace("-", " / ")


def _markdown_sources(
    profile_home: Path,
    tenant_roots: Optional[Iterable[Path]] = None,
) -> list[tuple[Path, str, str]]:
    sources: list[tuple[Path, str, str]] = []
    for root in _memory_roots(profile_home, tenant_roots):
        if not root.is_dir():
            continue
        for file_path in sorted(root.glob("*/memory/**/*.md")):
            tenant = _tenant_for_file(file_path, root)
            palace = tenant if PALACE_NAME_RE.match(tenant) else _stable_id("tenant", tenant, max_len=64)
            sources.append((file_path, tenant, palace))

    memories = profile_home / "memories"
    for file_path in (memories / "MEMORY.md", memories / "USER.md"):
        if file_path.is_file():
            sources.append((file_path, "hermes_profile", "hermes_profile"))
    return sources


def _source_signature(sources: Iterable[tuple[Path, str, str]]) -> str:
    digest = hashlib.sha1()
    for file_path, tenant, palace in sorted(sources, key=lambda item: str(item[0])):
        try:
            stat = file_path.stat()
        except FileNotFoundError:
            continue
        digest.update(str(file_path).encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(str(tenant).encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(str(palace).encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _state_path(paths: PalacePaths) -> Path:
    return paths.profile_home / "mempalace" / "refresh-state.json"


def _read_refresh_state(paths: PalacePaths) -> dict[str, Any]:
    path = _state_path(paths)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_refresh_state(paths: PalacePaths, state: dict[str, Any]) -> None:
    path = _state_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _acquire_refresh_lock(paths: PalacePaths, *, stale_after: int = 30 * 60) -> Optional[Path]:
    lock_path = paths.profile_home / "mempalace" / "refresh.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - lock_path.stat().st_mtime
        except FileNotFoundError:
            return _acquire_refresh_lock(paths, stale_after=stale_after)
        if age > stale_after:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            return _acquire_refresh_lock(paths, stale_after=stale_after)
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()} {utc_now()}\n")
    return lock_path


def _release_refresh_lock(lock_path: Optional[Path]) -> None:
    if lock_path is None:
        return
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _state_db_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path).resolve()
    from hermes_state import DEFAULT_DB_PATH

    return Path(DEFAULT_DB_PATH).resolve()


def _connect_state_readonly(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = _state_db_path(db_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _profile_route_lookup() -> tuple[str, dict[tuple[str, str], str]]:
    from hermes_cli.config import load_config

    cfg = load_config() or {}
    raw = cfg.get("profile_routes") or {}
    default_profile = str(raw.get("default_profile") or "default").strip() or "default"
    routes: dict[tuple[str, str], str] = {}
    for item in raw.get("routes") or []:
        if not isinstance(item, dict) or item.get("enabled", True) is False:
            continue
        platform = str(item.get("platform") or "").strip().lower()
        chat_id = str(item.get("chat_id") or "").strip()
        profile = str(item.get("profile") or "").strip() or default_profile
        if platform and chat_id and profile:
            routes[(platform, chat_id)] = profile
            routes[(platform, chat_id.lstrip("-"))] = profile
    return default_profile, routes


def _session_origin_lookup(state_db_path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    db_path = _state_db_path(state_db_path)
    sessions_file = db_path.parent / "sessions" / "sessions.json"
    if not sessions_file.is_file():
        return {}
    try:
        raw = json.loads(sessions_file.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    lookup: dict[str, dict[str, Any]] = {}
    for session_key, item in raw.items():
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id") or "").strip()
        origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
        if not session_id:
            continue
        lookup[session_id] = {
            "session_key": session_key,
            "display_name": item.get("display_name"),
            "platform": item.get("platform") or origin.get("platform"),
            "chat_type": item.get("chat_type") or origin.get("chat_type"),
            **origin,
        }
    return lookup


def _session_index_signature(state_db_path: Optional[Path] = None) -> str:
    sessions_file = _state_db_path(state_db_path).parent / "sessions" / "sessions.json"
    try:
        stat = sessions_file.stat()
    except FileNotFoundError:
        return "missing"
    digest = hashlib.sha1()
    digest.update(str(sessions_file).encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(str(stat.st_size).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return digest.hexdigest()


def _parse_access_scope(scope: str) -> dict[str, str]:
    raw = str(scope or "").strip()
    match = _ACCESS_SCOPE_RE.match(raw)
    if match:
        return {key: str(value or "") for key, value in match.groupdict().items()}
    parts = raw.split(":")
    if len(parts) < 3 or parts[0] != "agent":
        return {}
    idx = 2 if parts[1] == "main" else 1
    parsed: dict[str, str] = {}
    if idx + 3 < len(parts) and parts[idx] == "profile" and parts[idx + 2] == "scope":
        parsed["profile"] = parts[idx + 1]
        parsed["scope"] = parts[idx + 3]
        idx += 4
    if idx + 2 < len(parts):
        parsed["platform"] = parts[idx]
        parsed["chat_type"] = parts[idx + 1]
        parsed["chat_id"] = parts[idx + 2]
    if idx + 3 < len(parts):
        parsed["thread_id"] = parts[idx + 3]
    return parsed


def _profile_for_history_session(
    row: sqlite3.Row,
    *,
    default_profile: str,
    routes: dict[tuple[str, str], str],
    session_lookup: Optional[dict[str, dict[str, Any]]] = None,
) -> str:
    origin = (session_lookup or {}).get(str(row["id"])) or {}
    origin_profile = str(origin.get("profile_name") or "").strip()
    if origin_profile:
        return origin_profile
    source = str(row["source"] or "").strip().lower()
    parsed = _parse_access_scope(row["access_scope"] if "access_scope" in row.keys() else "")
    parsed_profile = str(parsed.get("profile") or "").strip()
    if parsed_profile:
        return parsed_profile
    platform = str(origin.get("platform") or parsed.get("platform") or source).strip().lower()
    chat_id = str(origin.get("chat_id") or parsed.get("chat_id") or row["user_id"] or "").strip()
    if platform and chat_id:
        routed = routes.get((platform, chat_id)) or routes.get((platform, chat_id.lstrip("-")))
        if routed:
            return routed
    return default_profile


def _history_palace_for_session(row: sqlite3.Row) -> str:
    source = re.sub(r"[^a-z0-9]+", "_", str(row["source"] or "session").lower()).strip("_") or "session"
    return validate_palace_name(f"history_{source}"[:80])


def _strip_reply_prefix(text: str) -> str:
    value = text.strip()
    for _ in range(3):
        next_value = re.sub(r"^\[Replying to:.*?\]\s*", "", value, flags=re.DOTALL).strip()
        next_value = re.sub(r"^\[A Telegram[^\]]*?\]\s*", "", next_value, flags=re.DOTALL).strip()
        next_value = re.sub(r"^\[The user sent[^\]]*?\]\s*", "", next_value, flags=re.DOTALL).strip()
        if next_value == value:
            break
        value = next_value
    return value


def _clean_history_content(raw: Any) -> str:
    if raw is None:
        return ""
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
    text = _strip_reply_prefix(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:1800]


def _history_importance(role: str, text: str) -> int:
    stripped = text.strip()
    if len(stripped) < 24:
        return 0
    if _HISTORY_SKIP_RE.search(stripped) and len(stripped) < 220:
        return 0
    score = 2 if role == "user" else 1
    if _HISTORY_IMPORTANCE_RE.search(stripped):
        score += 4
    if re.search(r"(?:^|\n)\s*[-*]\s+", stripped):
        score += 1
    if re.search(r"`[^`]+`|/[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|json|md|yaml|sqlite3?)", stripped):
        score += 2
    if any(marker in stripped for marker in ("?", "почему", "как ", "сделай", "давай")) and role == "user":
        score += 1
    if len(stripped) > 800:
        score -= 1
    return score


def _fact_type(role: str, text: str) -> str:
    lower = text.lower()
    if role == "user" and re.search(r"(?:предпочита|люблю|не\s+люблю|ненавиж|не\s+делай|хочу|важно|prefer|want|don't|do not)", lower):
        return "preference"
    if re.search(r"(?:задач|сделай|реализ|исправ|удали|пересоб|обнов|план|project|fix|implement|build)", lower):
        return "task"
    if role == "assistant" and re.search(r"(?:сделал|готово|исправил|добавил|удалил|перезапустил|done|fixed|added|removed)", lower):
        return "outcome"
    return "fact"


def _fact_label(text: str) -> str:
    first = " ".join(text.split())
    return first[:96] + ("..." if len(first) > 96 else "")


def _entity_label(raw: str) -> str:
    return " ".join(str(raw or "").strip("[]`'\".,:;(){}<>").split())


def _extract_history_entities(text: str) -> list[str]:
    labels: list[str] = []
    for match in _MENTION_RE.finditer(text):
        labels.append(_entity_label(match.group(1)))
    for match in _CODE_ENTITY_RE.finditer(text):
        labels.append(_entity_label(match.group(1) or match.group(0)))
    for match in _NAMED_ENTITY_RE.finditer(text):
        labels.append(_entity_label(match.group(0)))

    seen: set[str] = set()
    result: list[str] = []
    for label in labels:
        key = label.lower()
        if len(label) < 3 or key in _ENTITY_STOPWORDS or key in seen:
            continue
        if len(label) > 80:
            continue
        seen.add(key)
        result.append(label)
        if len(result) >= 8:
            break
    return result


def _history_entity_type(label: str) -> str:
    if " " in label and all(part[:1].isupper() or part[:1] in "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЭЮЯ" for part in label.split()[:2]):
        return "person"
    if re.search(r"\.(?:py|ts|tsx|js|json|md|yaml|sqlite3?)$|/", label):
        return "artifact"
    return "concept"


def _history_signature(
    *,
    profile: str = "default",
    state_db_path: Optional[Path] = None,
) -> str:
    default_profile, routes = _profile_route_lookup()
    session_lookup = _session_origin_lookup(state_db_path)
    digest = hashlib.sha1()
    digest.update(_session_index_signature(state_db_path).encode("ascii"))
    digest.update(b":")
    try:
        with _connect_state_readonly(state_db_path) as conn:
            sessions = conn.execute(
                """
                SELECT id, source, user_id, access_scope, started_at, ended_at, message_count
                FROM sessions
                ORDER BY started_at DESC
                """
            ).fetchall()
            session_ids = [
                row["id"]
                for row in sessions
                if _profile_for_history_session(
                    row,
                    default_profile=default_profile,
                    routes=routes,
                    session_lookup=session_lookup,
                )
                == profile
            ]
            digest.update(str(len(session_ids)).encode("ascii"))
            if session_ids:
                placeholders = ",".join(["?"] * len(session_ids))
                rows = conn.execute(
                    f"""
                    SELECT COUNT(*) AS count, COALESCE(MAX(id), 0) AS max_id,
                           COALESCE(MAX(timestamp), 0) AS max_ts
                    FROM messages
                    WHERE active = 1 AND session_id IN ({placeholders})
                    """,
                    session_ids,
                ).fetchone()
                digest.update(str(rows["count"]).encode("ascii"))
                digest.update(str(rows["max_id"]).encode("ascii"))
                digest.update(str(rows["max_ts"]).encode("ascii"))
    except FileNotFoundError:
        digest.update(b"missing")
    return digest.hexdigest()


def _combined_source_signature(
    paths: PalacePaths,
    *,
    tenant_roots: Optional[Iterable[Path]] = None,
    state_db_path: Optional[Path] = None,
) -> str:
    digest = hashlib.sha1()
    digest.update(_source_signature(_markdown_sources(paths.profile_home, tenant_roots)).encode("ascii"))
    digest.update(b":")
    digest.update(_history_signature(profile=paths.profile, state_db_path=state_db_path).encode("ascii"))
    return digest.hexdigest()


def _iter_markdown_entries(path: Path) -> Iterable[tuple[str, str]]:
    heading = _topic_for_file(path)
    in_fence = False
    paragraph: list[str] = []

    def flush_para():
        nonlocal paragraph
        if paragraph:
            text = " ".join(paragraph).strip()
            paragraph = []
            if len(text) >= 24:
                return text[:900]
        return None

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        title = _TITLE_RE.match(line)
        if title:
            flushed = flush_para()
            if flushed:
                yield heading, flushed
            heading = title.group(1).strip()
            continue
        if not line:
            flushed = flush_para()
            if flushed:
                yield heading, flushed
            continue
        if line.startswith(">"):
            line = line.lstrip(">").strip()
        bullet = _BULLET_RE.match(line)
        if bullet:
            flushed = flush_para()
            if flushed:
                yield heading, flushed
            text = bullet.group(1).strip()
            if len(text) >= 12:
                yield heading, text[:900]
            continue
        if line.startswith("|") and line.endswith("|"):
            continue
        if len(line) >= 24:
            paragraph.append(line)
    flushed = flush_para()
    if flushed:
        yield heading, flushed


def _upsert_entity(conn: sqlite3.Connection, entity_id: str, name: str, entity_type: str, properties: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO entities(id, name, type, properties, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            type = excluded.type,
            properties = excluded.properties
        """,
        (entity_id, name, entity_type, json.dumps(properties, ensure_ascii=False), utc_now()),
    )


def _upsert_triple(
    conn: sqlite3.Connection,
    triple_id: str,
    subject: str,
    predicate: str,
    object_id: str,
    *,
    confidence: float = 0.95,
    source_file: str = "",
    source_closet: str = "",
    adapter_name: str = "hermes_markdown",
) -> None:
    conn.execute(
        """
        INSERT INTO triples(
            id, subject, predicate, object, confidence, source_closet,
            source_file, adapter_name, extracted_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            subject = excluded.subject,
            predicate = excluded.predicate,
            object = excluded.object,
            confidence = excluded.confidence,
            source_closet = excluded.source_closet,
            source_file = excluded.source_file,
            adapter_name = excluded.adapter_name,
            extracted_at = excluded.extracted_at
        """,
        (
            triple_id,
            subject,
            predicate,
            object_id,
            confidence,
            source_closet,
            source_file,
            adapter_name,
            utc_now(),
        ),
    )


def generate_from_markdown(
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    tenant_roots: Optional[Iterable[Path]] = None,
    dry_run: bool = False,
    max_entries_per_file: int = 160,
    auto_clean: bool = True,
    clean_backup: bool = True,
    clean_max_delete: int = AUTO_CLEAN_MAX_DELETE,
) -> dict[str, Any]:
    paths = palace_paths(profile, profile_home=profile_home)
    sources = _markdown_sources(paths.profile_home, tenant_roots)

    by_palace: dict[str, dict[str, Any]] = {}
    for file_path, tenant, palace in sources:
        record = by_palace.setdefault(
            palace,
            {"palace": palace, "files": 0, "entities": 0, "triples": 0, "entries": 0},
        )
        record["files"] += 1
        entries = list(_iter_markdown_entries(file_path))[:max_entries_per_file]
        record["entries"] += len(entries)
        if dry_run or not entries:
            continue

        db_path = _db_path(paths, palace)
        with _connect_rw(db_path) as conn:
            tenant_id = _stable_id("tenant", tenant)
            _upsert_entity(
                conn,
                tenant_id,
                tenant,
                "organization",
                {"description": f"Hermes memory rebuilt from {paths.profile_home}"},
            )
            for idx, (heading, text) in enumerate(entries, start=1):
                topic_name = heading or _topic_for_file(file_path)
                topic_id = _stable_id("topic", tenant, str(file_path), topic_name)
                literal_id = _stable_id("note", tenant, str(file_path), idx)
                source = str(file_path)
                _upsert_entity(
                    conn,
                    topic_id,
                    topic_name,
                    "concept",
                    {
                        "description": f"Hermes curated memory topic from {file_path.name}",
                        "source_file": source,
                    },
                )
                _upsert_entity(
                    conn,
                    literal_id,
                    text,
                    "unknown",
                    {"source_file": source, "line_index": idx},
                )
                _upsert_triple(
                    conn,
                    _stable_id("hmd", tenant, str(file_path), idx),
                    topic_id,
                    "states",
                    literal_id,
                    source_file=source,
                    source_closet=f"hermes:{tenant}",
                )
                _upsert_triple(
                    conn,
                    _stable_id("hmd_topic", tenant, str(file_path), topic_name),
                    tenant_id,
                    "has_memory_topic",
                    topic_id,
                    source_file=source,
                    source_closet=f"hermes:{tenant}",
                )
                record["entities"] += 2
                record["triples"] += 2
            conn.commit()

    if not dry_run:
        now_iso = utc_now()
        now_epoch = time.time()
        _write_refresh_state(
            paths,
            {
                "profile": paths.profile,
                "last_checked_at": now_iso,
                "last_checked_epoch": now_epoch,
                "last_generate_at": now_iso,
                "last_generate_epoch": now_epoch,
                "source_signature": _source_signature(sources),
                "files": len(sources),
            },
        )

    return {
        "profile": paths.profile,
        "profile_home": str(paths.profile_home),
        "dry_run": dry_run,
        "files": len(sources),
        "palaces": sorted(by_palace.values(), key=lambda item: item["palace"]),
        "auto_clean": auto_clean_palaces(
            profile=paths.profile,
            profile_home=paths.profile_home,
            palaces=by_palace.keys(),
            enabled=bool(auto_clean and not dry_run),
            backup=clean_backup,
            max_delete=clean_max_delete,
        ),
    }


def generate_from_history(
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    state_db_path: Optional[Path] = None,
    dry_run: bool = False,
    max_sessions: int = HISTORY_MAX_SESSIONS_PER_PROFILE,
    max_messages_per_session: int = HISTORY_MAX_MESSAGES_PER_SESSION,
    max_facts: int = HISTORY_MAX_FACTS_PER_PROFILE,
    auto_clean: bool = True,
    clean_backup: bool = True,
    clean_max_delete: int = AUTO_CLEAN_MAX_DELETE,
) -> dict[str, Any]:
    paths = palace_paths(profile, profile_home=profile_home)
    default_profile, routes = _profile_route_lookup()
    session_lookup = _session_origin_lookup(state_db_path)
    max_sessions = max(1, min(int(max_sessions or HISTORY_MAX_SESSIONS_PER_PROFILE), 10000))
    max_messages_per_session = max(1, min(int(max_messages_per_session or HISTORY_MAX_MESSAGES_PER_SESSION), 80))
    max_facts = max(1, min(int(max_facts or HISTORY_MAX_FACTS_PER_PROFILE), 10000))

    by_palace: dict[str, dict[str, Any]] = {}
    touched: set[str] = set()
    total_sessions = 0
    total_messages = 0
    total_facts = 0

    try:
        conn_ctx = _connect_state_readonly(state_db_path)
    except FileNotFoundError:
        return {
            "profile": paths.profile,
            "profile_home": str(paths.profile_home),
            "dry_run": dry_run,
            "state_db": str(_state_db_path(state_db_path)),
            "sessions": 0,
            "messages": 0,
            "facts": 0,
            "palaces": [],
            "auto_clean": [],
            "missing_state_db": True,
        }

    with conn_ctx as state_conn:
        sessions = state_conn.execute(
            """
            SELECT id, source, user_id, access_scope, title, started_at, ended_at,
                   message_count, cwd
            FROM sessions
            WHERE COALESCE(message_count, 0) > 0
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (max_sessions * 4,),
        ).fetchall()

        for session in sessions:
            if total_sessions >= max_sessions or total_facts >= max_facts:
                break
            session_profile = _profile_for_history_session(
                session,
                default_profile=default_profile,
                routes=routes,
                session_lookup=session_lookup,
            )
            if session_profile != paths.profile:
                continue
            origin = session_lookup.get(str(session["id"])) or {}

            rows = state_conn.execute(
                """
                SELECT id, role, content, tool_name, timestamp
                FROM messages
                WHERE session_id = ? AND active = 1
                  AND role IN ('user', 'assistant')
                  AND content IS NOT NULL
                ORDER BY timestamp ASC
                """,
                (session["id"],),
            ).fetchall()
            scored: list[tuple[int, sqlite3.Row, str]] = []
            for message in rows:
                text = _clean_history_content(message["content"])
                score = _history_importance(str(message["role"] or ""), text)
                if score <= 0:
                    continue
                scored.append((score, message, text))
            scored.sort(key=lambda item: (item[0], float(item[1]["timestamp"] or 0)), reverse=True)
            selected = list(reversed(scored[:max_messages_per_session]))
            if not selected:
                continue

            palace = _history_palace_for_session(session)
            touched.add(palace)
            record = by_palace.setdefault(
                palace,
                {"palace": palace, "sessions": 0, "messages": 0, "facts": 0, "entities": 0, "triples": 0},
            )
            record["sessions"] += 1
            total_sessions += 1

            if dry_run:
                record["messages"] += len(selected)
                record["facts"] += len(selected)
                total_messages += len(selected)
                total_facts += len(selected)
                continue

            db_path = _db_path(paths, palace)
            with _connect_rw(db_path) as conn:
                profile_id = _stable_id("profile", paths.profile)
                source_name = str(origin.get("platform") or session["source"] or "unknown")
                source_id = _stable_id("source", source_name)
                session_id = _stable_id("session", session["id"])
                title = (
                    str(session["title"] or "").strip()
                    or str(origin.get("chat_topic") or "").strip()
                    or str(origin.get("display_name") or origin.get("chat_name") or "").strip()
                    or f"{source_name} session {session['id']}"
                )
                parsed_scope = _parse_access_scope(session["access_scope"] if "access_scope" in session.keys() else "")
                _upsert_entity(
                    conn,
                    profile_id,
                    paths.profile,
                    "profile",
                    {"description": "Hermes profile owning this extracted chat history"},
                )
                _upsert_entity(
                    conn,
                    source_id,
                    source_name,
                    "source",
                    {"description": "Hermes conversation source"},
                )
                _upsert_entity(
                    conn,
                    session_id,
                    title[:160],
                    "session",
                    {
                        "description": title,
                        "source_session_id": session["id"],
                        "session_key": origin.get("session_key", ""),
                        "source": session["source"],
                        "platform": origin.get("platform", session["source"]),
                        "chat_id": origin.get("chat_id", parsed_scope.get("chat_id", "")),
                        "chat_type": origin.get("chat_type", parsed_scope.get("chat_type", "")),
                        "chat_name": origin.get("chat_name", origin.get("display_name", "")),
                        "chat_topic": origin.get("chat_topic", ""),
                        "user_id": origin.get("user_id", session["user_id"]),
                        "user_name": origin.get("user_name", ""),
                        "profile_name": origin.get("profile_name", paths.profile),
                        "scope_name": origin.get("scope_name", parsed_scope.get("scope", "")),
                        "memory_scope": origin.get("memory_scope", ""),
                        "access_scope": session["access_scope"] if "access_scope" in session.keys() else "",
                        "thread_id": origin.get("thread_id", parsed_scope.get("thread_id", "")),
                        "started_at": session["started_at"],
                        "ended_at": session["ended_at"],
                        "message_count": session["message_count"],
                        "cwd": session["cwd"] if "cwd" in session.keys() else "",
                    },
                )
                _upsert_triple(
                    conn,
                    _stable_id("hist_profile_session", paths.profile, session["id"]),
                    profile_id,
                    "has_history_session",
                    session_id,
                    source_closet=f"state.db:{session['id']}",
                    adapter_name="hermes_history",
                )
                _upsert_triple(
                    conn,
                    _stable_id("hist_session_source", session["id"], source_name),
                    session_id,
                    "came_from",
                    source_id,
                    source_closet=f"state.db:{session['id']}",
                    adapter_name="hermes_history",
                )
                record["entities"] += 3
                record["triples"] += 2

                for score, message, text in selected:
                    if total_facts >= max_facts:
                        break
                    fact_id = _stable_id("fact", session["id"], message["id"])
                    role = str(message["role"] or "")
                    fact_kind = _fact_type(role, text)
                    _upsert_entity(
                        conn,
                        fact_id,
                        _fact_label(text),
                        fact_kind,
                        {
                            "description": text,
                            "role": role,
                            "importance": score,
                            "source_session_id": session["id"],
                            "source_message_id": message["id"],
                            "source_timestamp": message["timestamp"],
                            "source_title": title,
                            "source": source_name,
                            "profile_name": paths.profile,
                        },
                    )
                    _upsert_triple(
                        conn,
                        _stable_id("hist_session_fact", session["id"], message["id"]),
                        session_id,
                        "contains_fact",
                        fact_id,
                        confidence=min(0.99, 0.55 + score * 0.06),
                        source_closet=f"state.db:{session['id']}:{message['id']}",
                        adapter_name="hermes_history",
                    )
                    record["messages"] += 1
                    record["facts"] += 1
                    record["entities"] += 1
                    record["triples"] += 1
                    total_messages += 1
                    total_facts += 1

                    for label in _extract_history_entities(text):
                        entity_id = _stable_id("entity", label)
                        _upsert_entity(
                            conn,
                            entity_id,
                            label,
                            _history_entity_type(label),
                            {"description": f"Entity mentioned in Hermes chat history: {label}"},
                        )
                        _upsert_triple(
                            conn,
                            _stable_id("hist_fact_mentions", session["id"], message["id"], label),
                            fact_id,
                            "mentions",
                            entity_id,
                            confidence=0.72,
                            source_closet=f"state.db:{session['id']}:{message['id']}",
                            adapter_name="hermes_history",
                        )
                        _upsert_triple(
                            conn,
                            _stable_id("hist_session_discussed", session["id"], label),
                            session_id,
                            "discussed",
                            entity_id,
                            confidence=0.66,
                            source_closet=f"state.db:{session['id']}:{message['id']}",
                            adapter_name="hermes_history",
                        )
                        record["entities"] += 1
                        record["triples"] += 2
                conn.commit()

    return {
        "profile": paths.profile,
        "profile_home": str(paths.profile_home),
        "dry_run": dry_run,
        "state_db": str(_state_db_path(state_db_path)),
        "sessions": total_sessions,
        "messages": total_messages,
        "facts": total_facts,
        "palaces": sorted(by_palace.values(), key=lambda item: item["palace"]),
        "auto_clean": auto_clean_palaces(
            profile=paths.profile,
            profile_home=paths.profile_home,
            palaces=touched,
            enabled=bool(auto_clean and not dry_run),
            backup=clean_backup,
            max_delete=clean_max_delete,
        ),
    }


def rebuild_from_markdown(
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    tenant_roots: Optional[Iterable[Path]] = None,
    backup: bool = True,
    auto_clean: bool = True,
    clean_backup: bool = True,
    clean_max_delete: int = AUTO_CLEAN_MAX_DELETE,
    max_entries_per_file: int = 160,
) -> dict[str, Any]:
    paths = palace_paths(profile, profile_home=profile_home)
    backup_root = ""
    removed_existing = False
    if paths.storage_root.exists():
        removed_existing = True
        if backup:
            backup_dir = (
                paths.profile_home
                / "mempalace"
                / "backups"
                / f"markdown-rebuild-{utc_now().replace(':', '').replace('+', 'Z')}"
            )
            backup_dir.mkdir(parents=True, exist_ok=True)
            dest = backup_dir / "mcp-storage"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(paths.storage_root), str(dest))
            backup_root = str(backup_dir)
        else:
            shutil.rmtree(paths.storage_root)
    paths.storage_root.mkdir(parents=True, exist_ok=True)

    generated = generate_from_markdown(
        profile=paths.profile,
        profile_home=paths.profile_home,
        tenant_roots=tenant_roots,
        dry_run=False,
        max_entries_per_file=max_entries_per_file,
        auto_clean=auto_clean,
        clean_backup=clean_backup,
        clean_max_delete=clean_max_delete,
    )
    now_iso = utc_now()
    now_epoch = time.time()
    state = {
        "profile": paths.profile,
        "last_checked_at": now_iso,
        "last_checked_epoch": now_epoch,
        "last_rebuild_at": now_iso,
        "last_rebuild_epoch": now_epoch,
        "source_signature": _source_signature(_markdown_sources(paths.profile_home, tenant_roots)),
        "files": generated.get("files", 0),
    }
    _write_refresh_state(paths, state)
    return {
        "profile": paths.profile,
        "profile_home": str(paths.profile_home),
        "backup_root": backup_root,
        "removed_existing": removed_existing,
        "generated": generated,
        "auto_clean": generated.get("auto_clean", []),
        "stats": profile_stats(profile=paths.profile, profile_home=paths.profile_home),
    }


def rebuild_from_history(
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    state_db_path: Optional[Path] = None,
    tenant_roots: Optional[Iterable[Path]] = None,
    backup: bool = True,
    include_markdown: bool = True,
    auto_clean: bool = True,
    clean_backup: bool = True,
    clean_max_delete: int = AUTO_CLEAN_MAX_DELETE,
    max_sessions: int = HISTORY_MAX_SESSIONS_PER_PROFILE,
    max_messages_per_session: int = HISTORY_MAX_MESSAGES_PER_SESSION,
    max_facts: int = HISTORY_MAX_FACTS_PER_PROFILE,
) -> dict[str, Any]:
    paths = palace_paths(profile, profile_home=profile_home)
    backup_root = ""
    removed_existing = False
    if paths.storage_root.exists():
        removed_existing = True
        if backup:
            backup_dir = (
                paths.profile_home
                / "mempalace"
                / "backups"
                / f"history-rebuild-{utc_now().replace(':', '').replace('+', 'Z')}"
            )
            backup_dir.mkdir(parents=True, exist_ok=True)
            dest = backup_dir / "mcp-storage"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(paths.storage_root), str(dest))
            backup_root = str(backup_dir)
        else:
            shutil.rmtree(paths.storage_root)
    paths.storage_root.mkdir(parents=True, exist_ok=True)

    history = generate_from_history(
        profile=paths.profile,
        profile_home=paths.profile_home,
        state_db_path=state_db_path,
        max_sessions=max_sessions,
        max_messages_per_session=max_messages_per_session,
        max_facts=max_facts,
        auto_clean=auto_clean,
        clean_backup=clean_backup,
        clean_max_delete=clean_max_delete,
    )
    markdown = None
    if include_markdown:
        markdown = generate_from_markdown(
            profile=paths.profile,
            profile_home=paths.profile_home,
            tenant_roots=tenant_roots,
            auto_clean=auto_clean,
            clean_backup=clean_backup,
            clean_max_delete=clean_max_delete,
        )

    now_iso = utc_now()
    now_epoch = time.time()
    _write_refresh_state(
        paths,
        {
            "profile": paths.profile,
            "last_checked_at": now_iso,
            "last_checked_epoch": now_epoch,
            "last_rebuild_at": now_iso,
            "last_rebuild_epoch": now_epoch,
            "source_signature": _combined_source_signature(
                paths,
                tenant_roots=tenant_roots,
                state_db_path=state_db_path,
            ),
            "source_kind": "history+markdown",
            "history_sessions": history.get("sessions", 0),
            "history_facts": history.get("facts", 0),
            "markdown_files": (markdown or {}).get("files", 0),
        },
    )
    return {
        "profile": paths.profile,
        "profile_home": str(paths.profile_home),
        "backup_root": backup_root,
        "removed_existing": removed_existing,
        "history": history,
        "generated": markdown or {"files": 0, "palaces": [], "auto_clean": []},
        "auto_clean": [*history.get("auto_clean", []), *((markdown or {}).get("auto_clean", []))],
        "stats": profile_stats(profile=paths.profile, profile_home=paths.profile_home),
    }


def rebuild_all_profiles_from_history(
    *,
    state_db_path: Optional[Path] = None,
    backup: bool = True,
    include_markdown: bool = True,
    auto_clean: bool = True,
    clean_backup: bool = True,
    clean_max_delete: int = AUTO_CLEAN_MAX_DELETE,
) -> dict[str, Any]:
    results = [
        rebuild_from_history(
            profile=str(row["name"]),
            state_db_path=state_db_path,
            backup=backup,
            include_markdown=include_markdown,
            auto_clean=auto_clean,
            clean_backup=clean_backup,
            clean_max_delete=clean_max_delete,
        )
        for row in list_profiles()
    ]
    return {
        "profiles": results,
        "matrix": profile_matrix(),
    }


def rebuild_all_profiles_from_markdown(
    *,
    backup: bool = True,
    auto_clean: bool = True,
    clean_backup: bool = True,
    clean_max_delete: int = AUTO_CLEAN_MAX_DELETE,
) -> dict[str, Any]:
    results = [
        rebuild_from_markdown(
            profile=str(row["name"]),
            backup=backup,
            auto_clean=auto_clean,
            clean_backup=clean_backup,
            clean_max_delete=clean_max_delete,
        )
        for row in list_profiles()
    ]
    return {
        "profiles": results,
        "matrix": profile_matrix(),
    }


def refresh_if_due(
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    state_db_path: Optional[Path] = None,
    interval_seconds: int = AUTO_REFRESH_INTERVAL_SECONDS,
    force: bool = False,
    backup: bool = True,
    include_markdown: bool = True,
) -> dict[str, Any]:
    paths = palace_paths(profile, profile_home=profile_home)
    interval_seconds = max(60, int(interval_seconds or AUTO_REFRESH_INTERVAL_SECONDS))
    now = time.time()
    state = _read_refresh_state(paths)
    last_checked_at = float(state.get("last_checked_epoch") or 0)
    if not force and last_checked_at and now - last_checked_at < interval_seconds:
        return {
            "profile": paths.profile,
            "refreshed": False,
            "skipped": True,
            "reason": "interval",
            "next_check_in_seconds": int(interval_seconds - (now - last_checked_at)),
        }

    lock_path = _acquire_refresh_lock(paths)
    if lock_path is None:
        return {
            "profile": paths.profile,
            "refreshed": False,
            "skipped": True,
            "reason": "locked",
        }
    try:
        sources = _markdown_sources(paths.profile_home)
        signature = _combined_source_signature(paths, state_db_path=state_db_path)
        state = _read_refresh_state(paths)
        previous = str(state.get("source_signature") or "")
        state.update(
            {
                "profile": paths.profile,
                "last_checked_at": utc_now(),
                "last_checked_epoch": now,
                "source_signature": previous or signature,
                "source_kind": "history+markdown",
                "files": len(sources),
            }
        )
        if not force and previous == signature and paths.storage_root.exists():
            state["source_signature"] = signature
            _write_refresh_state(paths, state)
            return {
                "profile": paths.profile,
                "refreshed": False,
                "skipped": True,
                "reason": "unchanged",
                "files": len(sources),
            }
        result = rebuild_from_history(
            profile=paths.profile,
            profile_home=paths.profile_home,
            state_db_path=state_db_path,
            backup=backup,
            include_markdown=include_markdown,
        )
        history = result.get("history") or {}
        state.update(
            {
                "last_rebuild_at": utc_now(),
                "last_rebuild_epoch": time.time(),
                "source_signature": signature,
                "source_kind": "history+markdown",
                "history_sessions": history.get("sessions", 0),
                "history_facts": history.get("facts", 0),
                "files": len(sources),
            }
        )
        _write_refresh_state(paths, state)
        return {
            "profile": paths.profile,
            "refreshed": True,
            "skipped": False,
            "reason": "forced" if force else "changed",
            "files": len(sources),
            "history_sessions": history.get("sessions", 0),
            "history_facts": history.get("facts", 0),
            "result": result,
        }
    finally:
        _release_refresh_lock(lock_path)


def refresh_all_profiles_if_due(
    *,
    state_db_path: Optional[Path] = None,
    interval_seconds: int = AUTO_REFRESH_INTERVAL_SECONDS,
    force: bool = False,
    backup: bool = True,
    include_markdown: bool = True,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for row in list_profiles():
        name = str(row["name"])
        try:
            results.append(
                refresh_if_due(
                    profile=name,
                    state_db_path=state_db_path,
                    interval_seconds=interval_seconds,
                    force=force,
                    backup=backup,
                    include_markdown=include_markdown,
                )
            )
        except Exception as exc:
            results.append(
                {
                    "profile": name,
                    "refreshed": False,
                    "skipped": True,
                    "reason": "error",
                    "error": str(exc),
                }
            )
    return {
        "profiles": results,
        "refreshed": sum(1 for item in results if item.get("refreshed")),
        "skipped": sum(1 for item in results if item.get("skipped")),
    }


def copy_import_to_profile(
    *,
    profile: str = "default",
    profile_home: Optional[Path] = None,
    snapshot: str = "",
    overwrite: bool = False,
    import_root: Optional[Path] = None,
    auto_clean: bool = True,
    clean_backup: bool = True,
    clean_max_delete: int = AUTO_CLEAN_MAX_DELETE,
) -> dict[str, Any]:
    snapshots = list_import_snapshots(import_root)
    if not snapshots:
        raise FileNotFoundError("No MemPalace import snapshots found")
    selected = None
    if snapshot:
        for item in snapshots:
            if item["name"] == snapshot:
                selected = item
                break
        if selected is None:
            raise FileNotFoundError(f"MemPalace import snapshot {snapshot!r} not found")
    else:
        selected = snapshots[0]
    paths = palace_paths(profile, profile_home=profile_home)
    source_root = Path(selected["path"]) / "mcp-storage"
    paths.storage_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0
    copied_palaces: list[str] = []
    for child in sorted(source_root.iterdir(), key=lambda p: p.name):
        if not (child / "mempalace" / "knowledge_graph.sqlite3").is_file():
            continue
        validate_palace_name(child.name)
        dest = paths.storage_root / child.name
        if dest.exists():
            if not overwrite:
                skipped += 1
                continue
            shutil.rmtree(dest)
        shutil.copytree(child, dest)
        copied += 1
        copied_palaces.append(child.name)
    return {
        "profile": paths.profile,
        "profile_home": str(paths.profile_home),
        "snapshot": selected["name"],
        "copied": copied,
        "skipped": skipped,
        "storage_root": str(paths.storage_root),
        "auto_clean": auto_clean_palaces(
            profile=paths.profile,
            profile_home=paths.profile_home,
            palaces=copied_palaces,
            enabled=auto_clean,
            backup=clean_backup,
            max_delete=clean_max_delete,
        ),
    }


def _select_import_snapshot(snapshot: str = "", import_root: Optional[Path] = None) -> dict[str, Any]:
    snapshots = list_import_snapshots(import_root)
    if not snapshots:
        raise FileNotFoundError("No MemPalace import snapshots found")
    if not snapshot:
        return snapshots[0]
    for item in snapshots:
        if item["name"] == snapshot:
            return item
    raise FileNotFoundError(f"MemPalace import snapshot {snapshot!r} not found")


def _profile_route_chat_map() -> dict[str, str]:
    from hermes_cli.config import load_config

    cfg = load_config() or {}
    routes = ((cfg.get("profile_routes") or {}).get("routes") or [])
    chat_map: dict[str, str] = {}
    for route in routes:
        if not isinstance(route, dict) or not route.get("enabled", True):
            continue
        profile = str(route.get("profile") or "").strip()
        chat_id = str(route.get("chat_id") or "").strip()
        if not profile or not chat_id:
            continue
        chat_map[chat_id] = profile
        chat_map[chat_id.lstrip("-")] = profile
    return chat_map


def _source_chat_ids(db_path: Path) -> set[str]:
    ids: set[str] = set()
    with _connect_readonly(db_path) as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(adapter_name, source_closet, source_file, '') AS source
            FROM triples
            GROUP BY source
            """
        ).fetchall()
    for row in rows:
        for raw in re.findall(r"-?\d{6,}", str(row["source"] or "")):
            ids.add(raw)
            ids.add(raw.lstrip("-"))
    return ids


def _profile_for_import_palace(palace: str, source_ids: set[str], chat_map: dict[str, str]) -> str:
    name_hints = {
        "telegram_family": "family-chat",
        "telegram_mokuhub-ai": "proshamandovki",
    }
    hinted = name_hints.get(palace)
    if hinted and any(row["name"] == hinted for row in list_profiles()):
        return hinted
    for chat_id in sorted(source_ids, key=len, reverse=True):
        profile = chat_map.get(chat_id)
        if profile:
            return profile
    return "default"


def partition_import_to_profiles(
    *,
    snapshot: str = "",
    import_root: Optional[Path] = None,
    backup: bool = True,
    regenerate: bool = True,
    auto_clean: bool = True,
    clean_backup: bool = True,
    clean_max_delete: int = AUTO_CLEAN_MAX_DELETE,
) -> dict[str, Any]:
    selected = _select_import_snapshot(snapshot, import_root)
    source_root = Path(selected["path"]) / "mcp-storage"
    profiles = list_profiles()
    profile_names = [str(row["name"]) for row in profiles]
    chat_map = _profile_route_chat_map()
    backup_root: Optional[Path] = None
    if backup:
        backup_root = default_import_root().parent / "backups" / f"profile-split-{utc_now().replace(':', '').replace('+', 'Z')}"
        backup_root.mkdir(parents=True, exist_ok=True)

    per_profile: dict[str, dict[str, Any]] = {
        name: {"profile": name, "copied": 0, "generated_entries": 0, "palaces": [], "auto_clean": None}
        for name in profile_names
    }
    for name in profile_names:
        paths = palace_paths(name)
        if paths.storage_root.exists():
            if backup_root is not None:
                dest = backup_root / f"{name}-mcp-storage"
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.move(str(paths.storage_root), str(dest))
            else:
                shutil.rmtree(paths.storage_root)
        paths.storage_root.mkdir(parents=True, exist_ok=True)

    assignments: list[dict[str, Any]] = []
    for child in sorted(source_root.iterdir(), key=lambda p: p.name):
        db_path = child / "mempalace" / "knowledge_graph.sqlite3"
        if not db_path.is_file():
            continue
        palace_name = validate_palace_name(child.name)
        source_ids = _source_chat_ids(db_path)
        target_profile = _profile_for_import_palace(palace_name, source_ids, chat_map)
        if target_profile not in per_profile:
            target_profile = "default"
        target_paths = palace_paths(target_profile)
        dest = target_paths.storage_root / palace_name
        shutil.copytree(child, dest)
        stats = palace_stats(palace_name, profile=target_profile)
        per_profile[target_profile]["copied"] += 1
        per_profile[target_profile]["palaces"].append(
            {
                "palace": palace_name,
                "entities": stats.get("entity_count", 0),
                "triples": stats.get("triple_count", 0),
                "source_ids": sorted(source_ids),
            }
        )
        assignments.append(
            {
                "palace": palace_name,
                "profile": target_profile,
                "source_ids": sorted(source_ids),
            }
        )

    generated: list[dict[str, Any]] = []
    if regenerate:
        for name in profile_names:
            result = generate_from_markdown(profile=name, auto_clean=False)
            entries = sum(int(row.get("entries") or 0) for row in result.get("palaces", []))
            per_profile[name]["generated_entries"] = entries
            generated.append(result)

    auto_cleaned: list[dict[str, Any]] = []
    for name in profile_names:
        result = auto_clean_noise(
            profile=name,
            enabled=auto_clean,
            backup=clean_backup,
            max_delete=clean_max_delete,
        )
        per_profile[name]["auto_clean"] = result
        auto_cleaned.append(result)

    return {
        "snapshot": selected["name"],
        "backup_root": str(backup_root) if backup_root else "",
        "profiles": [per_profile[name] for name in profile_names],
        "assignments": assignments,
        "generated": generated,
        "auto_clean": auto_cleaned,
        "matrix": profile_matrix(),
    }
