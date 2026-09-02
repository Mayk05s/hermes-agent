from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys

import pytest

import hermes_cli.memory_acl_migration as migration_module
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.memory_acl_migration import (
    MigrationPlanError,
    build_migration_manifest,
    main,
    manifest_bytes,
    write_manifest,
)


PRINCIPAL = "p_km4z2x7v5q3n6rta"
PRIVATE = f"private.{PRINCIPAL}.personal"
GROUP = "group.telegram.c_aaaaaaaaaaaaaaaaaaaaaaaaaa.root.shared"
GROUP_ALT = "group.telegram.c_aaaaaaaaaaaaaaaaaaaaaaaaaa.root.archive"
DM_SCOPE = "agent:main:telegram:dm:dm-chat"
GROUP_SCOPE = "agent:main:telegram:group:group-chat"
UNMAPPED_SCOPE = "agent:main:telegram:dm:unmapped-chat"
FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "memory_acl_migration"
    / "service_export_v1.json"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_id_sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _manifest_ref(kind: str, value: str) -> str:
    return hashlib.sha256(
        json.dumps(
            ["hermes-memory-migration-ref-v1", kind, value],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _write_session_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                access_scope TEXT,
                system_prompt TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            [
                ("dm-session", "gateway", DM_SCOPE, "PRIVATE_SESSION_PROMPT_CANARY"),
                (
                    "group-session",
                    "gateway",
                    GROUP_SCOPE,
                    "GROUP_SESSION_PROMPT_CANARY",
                ),
                ("null-session", "gateway", None, "NULL_SCOPE_CANARY"),
                (
                    "unmapped-session",
                    "gateway",
                    UNMAPPED_SCOPE,
                    "UNMAPPED_SCOPE_CANARY",
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?)",
            [
                (1, "dm-session", "user", "PRIVATE_MESSAGE_CANARY"),
                (2, "group-session", "user", "GROUP_MESSAGE_CANARY"),
                (3, "null-session", "user", "NULL_MESSAGE_CANARY"),
                (4, "unmapped-session", "user", "UNMAPPED_MESSAGE_CANARY"),
                (5, "missing-session", "user", "ORPHAN_MESSAGE_CANARY"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _write_kg(path: Path, *, common_name: str, unique_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE entities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT,
                properties TEXT,
                created_at TEXT
            );
            CREATE TABLE triples (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                source_closet TEXT,
                source_file TEXT,
                source_drawer_id TEXT,
                adapter_name TEXT,
                evidence TEXT,
                extracted_at TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO entities VALUES (?, ?, ?, ?, ?)",
            [
                ("shared-entity", common_name, "person", "{}", "2026-01-01"),
                ("identical-entity", "Identical entity", "person", "{}", "2026-01-01"),
                (
                    "provenance-entity",
                    "Same semantic entity",
                    "person",
                    "{}",
                    f"2026-01-0{1 if unique_id == 'a' else 2}",
                ),
                (unique_id, f"Unique {unique_id}", "project", "{}", "2026-01-01"),
            ],
        )
        conn.executemany(
            "INSERT INTO triples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"triple-{unique_id}",
                    unique_id,
                    "belongs_to",
                    "shared-entity",
                    f"snapshot:{unique_id}",
                    "MEMORY.md",
                    None,
                    "legacy-test",
                    "KG_EVIDENCE_CANARY",
                    "2026-01-01",
                ),
                (
                    "provenance-triple",
                    "identical-entity",
                    "relates_to",
                    "shared-entity",
                    f"snapshot:{unique_id}",
                    "MEMORY.md",
                    None,
                    "legacy-test",
                    "same semantic triple",
                    "2026-01-01",
                ),
                (
                    "semantic-triple",
                    "identical-entity",
                    "belongs_to",
                    unique_id if unique_id == "c" else "shared-entity",
                    "same-source",
                    "MEMORY.md",
                    None,
                    "legacy-test",
                    "same provenance",
                    "2026-01-01",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _receipt(snapshot: Path, relative_paths: list[str]) -> tuple[str, str]:
    files = []
    for relative in sorted(relative_paths):
        path = snapshot / relative
        files.append({
            "path": relative,
            "sha256": _sha(path),
            "size": path.stat().st_size,
        })
    payload = {
        "format": "hermes-memory-snapshot-receipt-v1",
        "backup_id": "backup-test-20260902",
        "snapshot_complete": True,
        "sqlite_consistency": "checkpointed-no-wal",
        "files": files,
    }
    receipt_path = snapshot / "metadata" / "backup-receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    for path in snapshot.rglob("*"):
        if path.is_symlink():
            continue
        path.chmod(0o700 if path.is_dir() else 0o600)
    snapshot.chmod(0o700)
    return "metadata/backup-receipt.json", _sha(receipt_path)


def _snapshot_config(tmp_path: Path) -> tuple[Path, Path, dict]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    snapshot = tmp_path / "offline-snapshot"
    (snapshot / "profile" / "memories").mkdir(parents=True)
    memory_path = snapshot / "profile" / "memories" / "MEMORY.md"
    memory_path.write_text("PRIVATE_MEMORY_FILE_CANARY\n", encoding="utf-8")
    session_path = snapshot / "profile" / "state.db"
    _write_session_db(session_path)
    kg_a = snapshot / "profile" / "kg-a.sqlite3"
    kg_b = snapshot / "profile" / "kg-b.sqlite3"
    kg_c = snapshot / "profile" / "kg-c.sqlite3"
    _write_kg(kg_a, common_name="Same entity", unique_id="a")
    _write_kg(kg_b, common_name="Same entity", unique_id="b")
    _write_kg(kg_c, common_name="Conflicting entity", unique_id="c")
    export_path = snapshot / "service" / "export.json"
    export_path.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE, export_path)

    paths = [
        "profile/memories/MEMORY.md",
        "profile/state.db",
        "profile/kg-a.sqlite3",
        "profile/kg-b.sqlite3",
        "profile/kg-c.sqlite3",
        "service/export.json",
    ]
    receipt_path, receipt_sha = _receipt(snapshot, paths)
    sources = [
        {
            "kind": "memory_file",
            "source_namespace": "profile-memory",
            "path": paths[0],
            "target_wing": PRIVATE,
        },
        {
            "kind": "session_db",
            "source_namespace": "profile-sessions",
            "path": paths[1],
            "access_scope_map": {
                DM_SCOPE: {"source_kind": "private", "target_wing": PRIVATE},
                GROUP_SCOPE: {"source_kind": "group", "target_wing": GROUP},
            },
        },
        {
            "kind": "legacy_kg_db",
            "source_namespace": "profile-kg-a",
            "path": paths[2],
            "source_kind": "private",
            "target_wing": PRIVATE,
        },
        {
            "kind": "legacy_kg_db",
            "source_namespace": "profile-kg-b",
            "path": paths[3],
            "source_kind": "private",
            "target_wing": PRIVATE,
        },
        {
            "kind": "legacy_kg_db",
            "source_namespace": "profile-kg-c",
            "path": paths[4],
            "source_kind": "private",
            "target_wing": PRIVATE,
        },
        {
            "kind": "mempalace_export",
            "source_namespace": "service-export",
            "path": paths[5],
            "wing_map": {
                "legacy-personal": {"source_kind": "private", "target_wing": PRIVATE},
                "legacy-group": {"source_kind": "group", "target_wing": GROUP},
            },
        },
    ]
    config = {
        "schema_version": 1,
        "snapshot_root": str(snapshot),
        "backup_receipt": {"path": receipt_path, "sha256": receipt_sha},
        "sources": sources,
    }
    config_path = tmp_path / "migration-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config_path.chmod(0o600)
    return config_path, snapshot, config


def _records(manifest: dict, *, namespace: str) -> list[dict]:
    return [
        item
        for item in manifest["records"]
        if item["source_namespace_sha256"]
        == _manifest_ref("source-namespace", namespace)
    ]


def _by_legacy_id(records: list[dict], legacy_id: str) -> dict:
    digest = _legacy_id_sha(legacy_id)
    return next(item for item in records if item["legacy_id_sha256"] == digest)


def test_builds_deterministic_content_free_manifest_and_quarantines_mixed_scopes(
    tmp_path: Path,
) -> None:
    config_path, snapshot, _ = _snapshot_config(tmp_path)
    source_hashes = {
        path.relative_to(snapshot).as_posix(): _sha(path)
        for path in snapshot.rglob("*")
        if path.is_file()
    }

    first = build_migration_manifest(config_path)
    second = build_migration_manifest(config_path)

    assert manifest_bytes(first) == manifest_bytes(second)
    assert first["mode"] == "dry-run-only"
    assert first["rollback"] == {
        "strategy": "disable-memory-migration-and-restore-verified-backup",
        "backup_id_sha256": _manifest_ref("backup-id", "backup-test-20260902"),
        "destructive_sql_allowed": False,
    }
    session_records = _records(first, namespace="profile-sessions")
    assert _by_legacy_id(session_records, "dm-session")["target_wing"] == PRIVATE
    assert _by_legacy_id(session_records, "dm-session/1")["target_wing"] == PRIVATE
    assert _by_legacy_id(session_records, "group-session")["target_wing"] == GROUP
    assert _by_legacy_id(session_records, "group-session/2")["target_wing"] == GROUP
    assert (
        _by_legacy_id(session_records, "null-session")["quarantine_reason"]
        == "legacy_null_access_scope"
    )
    assert (
        _by_legacy_id(session_records, "null-session/3")["quarantine_reason"]
        == "legacy_null_access_scope"
    )
    assert (
        _by_legacy_id(session_records, "unmapped-session")["quarantine_reason"]
        == "unmapped_access_scope"
    )
    assert (
        _by_legacy_id(session_records, "missing-session/5")["quarantine_reason"]
        == "orphan_session"
    )
    service_records = _records(first, namespace="service-export")
    assert _by_legacy_id(service_records, "drawer-private-1")["target_wing"] == PRIVATE
    assert _by_legacy_id(service_records, "abcddcba")["target_wing"] == GROUP
    assert (
        _by_legacy_id(service_records, "legacy-null-entity")["quarantine_reason"]
        == "legacy_null_wing"
    )
    assert source_hashes == {
        path.relative_to(snapshot).as_posix(): _sha(path)
        for path in snapshot.rglob("*")
        if path.is_file()
    }

    encoded = manifest_bytes(first)
    for canary in (
        b"PRIVATE_MEMORY_FILE_CANARY",
        b"PRIVATE_SESSION_PROMPT_CANARY",
        b"PRIVATE_MESSAGE_CANARY",
        b"PRIVATE_SERVICE_CANARY",
        b"GROUP_SERVICE_CANARY",
        b"KG_EVIDENCE_CANARY",
        b"shared-entity",
        b"dm-session",
        b"legacy-null-entity",
        b"profile-memory",
        b"profile/memories/MEMORY.md",
        b"backup-test-20260902",
        DM_SCOPE.encode(),
        GROUP_SCOPE.encode(),
    ):
        assert canary not in encoded
    assert str(snapshot).encode() not in encoded


def test_reports_identical_duplicates_and_semantic_entity_conflicts(
    tmp_path: Path,
) -> None:
    config_path, _, _ = _snapshot_config(tmp_path)

    manifest = build_migration_manifest(config_path)

    collision = next(
        item
        for item in manifest["collisions"]
        if item["kind"] == "kg_entity"
        and item["legacy_id_sha256"] == _legacy_id_sha("shared-entity")
    )
    assert collision["classification"] == "semantic_conflict"
    assert {member["source_namespace_sha256"] for member in collision["members"]} == {
        _manifest_ref("source-namespace", "profile-kg-a"),
        _manifest_ref("source-namespace", "profile-kg-b"),
        _manifest_ref("source-namespace", "profile-kg-c"),
    }
    assert len({member["content_sha256"] for member in collision["members"]}) == 2
    identical = next(
        item
        for item in manifest["collisions"]
        if item["kind"] == "kg_entity"
        and item["legacy_id_sha256"] == _legacy_id_sha("identical-entity")
    )
    assert identical["classification"] == "identical_duplicate"
    provenance_entity = next(
        item
        for item in manifest["collisions"]
        if item["kind"] == "kg_entity"
        and item["legacy_id_sha256"] == _legacy_id_sha("provenance-entity")
    )
    provenance_triple = next(
        item
        for item in manifest["collisions"]
        if item["kind"] == "kg_triple"
        and item["legacy_id_sha256"] == _legacy_id_sha("provenance-triple")
    )
    semantic_triple = next(
        item
        for item in manifest["collisions"]
        if item["kind"] == "kg_triple"
        and item["legacy_id_sha256"] == _legacy_id_sha("semantic-triple")
    )
    assert provenance_entity["classification"] == "provenance_conflict"
    assert provenance_triple["classification"] == "provenance_conflict"
    assert semantic_triple["classification"] == "semantic_conflict"


def test_group_session_mapping_to_private_is_denied(tmp_path: Path) -> None:
    config_path, _, config = _snapshot_config(tmp_path)
    session_source = next(
        item for item in config["sources"] if item["kind"] == "session_db"
    )
    session_source["access_scope_map"][GROUP_SCOPE] = {
        "source_kind": "group",
        "target_wing": PRIVATE,
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(MigrationPlanError, match="cannot cross private/group"):
        build_migration_manifest(config_path)


def test_memory_file_may_not_target_group(tmp_path: Path) -> None:
    config_path, _, config = _snapshot_config(tmp_path)
    memory_source = next(
        item for item in config["sources"] if item["kind"] == "memory_file"
    )
    memory_source["target_wing"] = GROUP
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(MigrationPlanError, match="may target private wings only"):
        build_migration_manifest(config_path)


def test_tampered_receipt_and_tampered_source_are_denied(tmp_path: Path) -> None:
    config_path, snapshot, config = _snapshot_config(tmp_path)
    receipt = snapshot / config["backup_receipt"]["path"]
    receipt.write_text(receipt.read_text() + " ", encoding="utf-8")

    with pytest.raises(MigrationPlanError, match="receipt checksum mismatch"):
        build_migration_manifest(config_path)

    config_path, snapshot, _ = _snapshot_config(tmp_path / "source-case")
    with (snapshot / "profile" / "memories" / "MEMORY.md").open(
        "a", encoding="utf-8"
    ) as handle:
        handle.write("tampered")
    with pytest.raises(MigrationPlanError, match="does not match.*receipt"):
        build_migration_manifest(config_path)


def test_symlink_escape_and_live_root_are_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, snapshot, config = _snapshot_config(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = snapshot / "profile" / "memories" / "link.md"
    link.symlink_to(outside)
    memory_source = next(
        item for item in config["sources"] if item["kind"] == "memory_file"
    )
    memory_source["path"] = "profile/memories/link.md"
    receipt_path, receipt_sha = _receipt(snapshot, ["profile/memories/link.md"])
    config["backup_receipt"] = {"path": receipt_path, "sha256": receipt_sha}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(MigrationPlanError, match="symlink"):
        build_migration_manifest(config_path)

    live_config_path, live_snapshot, _ = _snapshot_config(tmp_path / "live-case")
    monkeypatch.setenv("HERMES_HOME", str(live_snapshot))
    with pytest.raises(
        MigrationPlanError, match="disjoint from every live Hermes root"
    ):
        build_migration_manifest(live_config_path)


def test_snapshot_nested_under_live_root_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_root = tmp_path / "live"
    config_path, _, config = _snapshot_config(live_root / "state-snapshots" / "capture")
    monkeypatch.setenv("HERMES_HOME", str(live_root))

    with pytest.raises(
        MigrationPlanError, match="disjoint from every live Hermes root"
    ):
        build_migration_manifest(config_path)


def test_snapshot_ancestor_of_live_root_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, snapshot, config = _snapshot_config(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(snapshot / "profile"))
    config["snapshot_root"] = str(snapshot)
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(
        MigrationPlanError, match="disjoint from every live Hermes root"
    ):
        build_migration_manifest(config_path)


def test_context_local_effective_hermes_home_is_denied(tmp_path: Path) -> None:
    config_path, snapshot, _ = _snapshot_config(tmp_path)
    token = set_hermes_home_override(snapshot)
    try:
        with pytest.raises(
            MigrationPlanError, match="disjoint from every live Hermes root"
        ):
            build_migration_manifest(config_path)
    finally:
        reset_hermes_home_override(token)


def test_snapshot_and_source_permissions_must_be_private(tmp_path: Path) -> None:
    config_path, snapshot, _ = _snapshot_config(tmp_path)
    snapshot.chmod(0o755)
    with pytest.raises(MigrationPlanError, match="inaccessible to group/other"):
        build_migration_manifest(config_path)

    snapshot.chmod(0o700)
    source = snapshot / "profile" / "memories" / "MEMORY.md"
    source.chmod(0o640)
    with pytest.raises(MigrationPlanError, match="inaccessible to group/other"):
        build_migration_manifest(config_path)


def test_config_must_be_private_regular_single_link(tmp_path: Path) -> None:
    config_path, _, _ = _snapshot_config(tmp_path)
    config_path.chmod(0o644)
    with pytest.raises(MigrationPlanError, match="private file"):
        build_migration_manifest(config_path)

    config_path.chmod(0o600)
    link = tmp_path / "linked-config.json"
    link.symlink_to(config_path)
    with pytest.raises(MigrationPlanError, match="must not be a symlink"):
        build_migration_manifest(link)


def test_wal_sidecar_is_denied(tmp_path: Path) -> None:
    config_path, snapshot, _ = _snapshot_config(tmp_path)
    (snapshot / "profile" / "state.db-wal").write_bytes(b"not-a-checkpointed-snapshot")

    with pytest.raises(MigrationPlanError, match="WAL sidecars"):
        build_migration_manifest(config_path)


def test_write_manifest_is_0600_idempotent_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    config_path, _, _ = _snapshot_config(tmp_path)
    manifest = build_migration_manifest(config_path)
    output = tmp_path / "migration-manifest.json"

    snapshot_root = Path(json.loads(config_path.read_text())["snapshot_root"])
    assert write_manifest(output, manifest, snapshot_root=snapshot_root) == "created"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert write_manifest(output, manifest, snapshot_root=snapshot_root) == "unchanged"
    output.write_text("different", encoding="utf-8")

    with pytest.raises(MigrationPlanError, match="refusing to overwrite"):
        write_manifest(output, manifest, snapshot_root=snapshot_root)


def test_manifest_output_rejects_hardlinks_and_public_parent(tmp_path: Path) -> None:
    private_parent = tmp_path / "private-output"
    private_parent.mkdir(mode=0o700)
    snapshot = tmp_path / "offline-snapshot"
    snapshot.mkdir(mode=0o700)
    protected = snapshot / "protected.json"
    protected.write_bytes(b"snapshot-metadata")
    protected.chmod(0o600)
    original_mode = stat.S_IMODE(protected.stat().st_mode)
    hardlink = private_parent / "manifest.json"
    hardlink.hardlink_to(protected)

    with pytest.raises(MigrationPlanError, match="single-link"):
        write_manifest(hardlink, {"mode": "dry-run-only"}, snapshot_root=snapshot)
    assert protected.read_bytes() == b"snapshot-metadata"
    assert stat.S_IMODE(protected.stat().st_mode) == original_mode

    hardlink.unlink()
    public_parent = tmp_path / "public-output"
    public_parent.mkdir(mode=0o700)
    public_parent.chmod(0o777)
    with pytest.raises(MigrationPlanError, match="parent.*private"):
        write_manifest(
            public_parent / "manifest.json",
            {"mode": "dry-run-only"},
            snapshot_root=snapshot,
        )


def test_manifest_output_must_not_modify_snapshot(tmp_path: Path) -> None:
    config_path, snapshot, _ = _snapshot_config(tmp_path)
    manifest = build_migration_manifest(config_path)

    with pytest.raises(MigrationPlanError, match="outside snapshot_root"):
        write_manifest(snapshot / "manifest.json", manifest, snapshot_root=snapshot)


def test_source_replacement_during_inventory_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, _, _ = _snapshot_config(tmp_path)
    original = migration_module._inventory_memory_file

    def replace_after_inventory(**kwargs):
        records = original(**kwargs)
        source_path = kwargs["path"]
        replacement = source_path.with_name("replacement.md")
        replacement.write_text("REPLACEMENT_CANARY", encoding="utf-8")
        replacement.chmod(0o600)
        os.replace(replacement, source_path)
        return records

    monkeypatch.setattr(
        migration_module, "_inventory_memory_file", replace_after_inventory
    )
    with pytest.raises(MigrationPlanError, match="changed during planning"):
        build_migration_manifest(config_path)


def test_receipt_replacement_during_parse_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, snapshot, config = _snapshot_config(tmp_path)
    receipt = snapshot / config["backup_receipt"]["path"]
    original = migration_module._decode_json
    replaced = False

    def decode_then_replace(raw: bytes, *, label: str, max_bytes: int):
        nonlocal replaced
        document = original(raw, label=label, max_bytes=max_bytes)
        if label == "backup receipt" and not replaced:
            replaced = True
            replacement = receipt.with_name("replacement-receipt.json")
            replacement.write_bytes(raw)
            replacement.chmod(0o600)
            os.replace(replacement, receipt)
        return document

    monkeypatch.setattr(migration_module, "_decode_json", decode_then_replace)
    with pytest.raises(MigrationPlanError, match="replaced during planning"):
        build_migration_manifest(config_path)


def test_service_group_to_private_mapping_is_denied(tmp_path: Path) -> None:
    config_path, _, config = _snapshot_config(tmp_path)
    service = next(
        item for item in config["sources"] if item["kind"] == "mempalace_export"
    )
    service["wing_map"]["legacy-group"] = {
        "source_kind": "group",
        "target_wing": PRIVATE,
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(MigrationPlanError, match="cannot cross private/group"):
        build_migration_manifest(config_path)


def test_personal_kg_to_group_mapping_is_denied(tmp_path: Path) -> None:
    config_path, _, config = _snapshot_config(tmp_path)
    kg = next(item for item in config["sources"] if item["kind"] == "legacy_kg_db")
    kg["target_wing"] = GROUP
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(MigrationPlanError, match="cannot cross private/group"):
        build_migration_manifest(config_path)


def test_service_collisions_distinguish_provenance_and_destination(
    tmp_path: Path,
) -> None:
    config_path, snapshot, config = _snapshot_config(tmp_path)
    destination_copy = snapshot / "service" / "export-destination.json"
    provenance_copy = snapshot / "service" / "export-provenance.json"
    shutil.copyfile(FIXTURE, destination_copy)
    provenance_payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    provenance_payload["records"][0]["provenance"]["adapter"] = "different-adapter"
    provenance_copy.write_text(json.dumps(provenance_payload), encoding="utf-8")
    config["sources"].extend([
        {
            "kind": "mempalace_export",
            "source_namespace": "service-destination-copy",
            "path": "service/export-destination.json",
            "wing_map": {
                "legacy-personal": {"source_kind": "private", "target_wing": PRIVATE},
                "legacy-group": {"source_kind": "group", "target_wing": GROUP_ALT},
            },
        },
        {
            "kind": "mempalace_export",
            "source_namespace": "service-provenance-copy",
            "path": "service/export-provenance.json",
            "wing_map": {
                "legacy-personal": {"source_kind": "private", "target_wing": PRIVATE},
                "legacy-group": {"source_kind": "group", "target_wing": GROUP},
            },
        },
    ])
    all_paths = [source["path"] for source in config["sources"]]
    receipt_path, receipt_sha = _receipt(snapshot, all_paths)
    config["backup_receipt"] = {"path": receipt_path, "sha256": receipt_sha}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    manifest = build_migration_manifest(config_path)

    drawer = next(
        item
        for item in manifest["collisions"]
        if item["kind"] == "drawer"
        and item["legacy_id_sha256"] == _legacy_id_sha("drawer-private-1")
    )
    document = next(
        item
        for item in manifest["collisions"]
        if item["kind"] == "document"
        and item["legacy_id_sha256"] == _legacy_id_sha("abcddcba")
    )
    assert drawer["classification"] == "provenance_conflict"
    assert document["classification"] == "destination_conflict"


def test_same_source_duplicates_are_reported_with_stable_logical_idempotency(
    tmp_path: Path,
) -> None:
    config_path, snapshot, config = _snapshot_config(tmp_path)
    export = snapshot / "service" / "export.json"
    payload = json.loads(export.read_text(encoding="utf-8"))
    template = {
        "kind": "drawer",
        "wing": "legacy-personal",
        "payload": {"room": "personal", "content": "same"},
        "provenance": {"adapter": "legacy"},
    }
    payload["records"].extend([
        {**template, "id": "same-source-identical"},
        {**template, "id": "same-source-identical"},
        {**template, "id": "same-source-conflict"},
        {
            **template,
            "id": "same-source-conflict",
            "payload": {"room": "personal", "content": "different"},
        },
    ])
    export.write_text(json.dumps(payload), encoding="utf-8")
    receipt_path, receipt_sha = _receipt(
        snapshot, [source["path"] for source in config["sources"]]
    )
    config["backup_receipt"] = {"path": receipt_path, "sha256": receipt_sha}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    manifest = build_migration_manifest(config_path)

    identical = next(
        item
        for item in manifest["collisions"]
        if item["legacy_id_sha256"] == _legacy_id_sha("same-source-identical")
    )
    conflict = next(
        item
        for item in manifest["collisions"]
        if item["legacy_id_sha256"] == _legacy_id_sha("same-source-conflict")
    )
    assert identical["classification"] == "identical_duplicate"
    assert conflict["classification"] == "semantic_conflict"
    identical_records = [
        record
        for record in manifest["records"]
        if record["legacy_id_sha256"] == _legacy_id_sha("same-source-identical")
    ]
    conflict_records = [
        record
        for record in manifest["records"]
        if record["legacy_id_sha256"] == _legacy_id_sha("same-source-conflict")
    ]
    assert len({record["idempotency_key"] for record in identical_records}) == 1
    assert len({record["idempotency_key"] for record in conflict_records}) == 2
    assert identical["requires_operator_resolution"] is True
    assert conflict["requires_operator_resolution"] is True

    before = {
        (record["legacy_id_sha256"], record["content_sha256"]): record[
            "idempotency_key"
        ]
        for record in manifest["records"]
    }
    payload["records"].reverse()
    export.write_text(json.dumps(payload), encoding="utf-8")
    receipt_path, receipt_sha = _receipt(
        snapshot, [source["path"] for source in config["sources"]]
    )
    config["backup_receipt"] = {"path": receipt_path, "sha256": receipt_sha}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    reordered = build_migration_manifest(config_path)
    after = {
        (record["legacy_id_sha256"], record["content_sha256"]): record[
            "idempotency_key"
        ]
        for record in reordered["records"]
    }
    assert after == before


def test_receipt_and_source_paths_must_be_snapshot_relative(tmp_path: Path) -> None:
    config_path, _, config = _snapshot_config(tmp_path)
    config["sources"][0]["path"] = "../outside"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(MigrationPlanError, match="snapshot-relative"):
        build_migration_manifest(config_path)


def test_output_contains_no_apply_mode_or_network_destination(tmp_path: Path) -> None:
    config_path, _, _ = _snapshot_config(tmp_path)
    manifest = build_migration_manifest(config_path)

    assert "apply" not in manifest
    assert all("url" not in source for source in manifest["sources"])
    assert manifest["future_apply_preconditions"]
    assert (
        manifest["manifest_sha256"]
        == hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in manifest.items()
                    if key != "manifest_sha256"
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
    )


@pytest.mark.parametrize(
    "raw_config",
    [
        '{"PRIVATE_KEY_CANARY":1,"PRIVATE_KEY_CANARY":2}',
        '{"PRIVATE_KEY_CANARY":1}',
    ],
)
def test_cli_denial_never_echoes_private_config_keys(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    raw_config: str,
) -> None:
    config_path = tmp_path / "invalid-config.json"
    config_path.write_text(raw_config, encoding="utf-8")
    config_path.chmod(0o600)

    assert main(["--config", str(config_path), "--output", str(tmp_path / "out")]) == 2
    captured = capsys.readouterr()
    assert captured.err == "migration plan denied\n"
    assert "PRIVATE_KEY_CANARY" not in captured.err


def test_cli_reads_config_once_and_entry_script_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _, _ = _snapshot_config(tmp_path)
    original = migration_module._load_migration_config
    calls = 0

    def load_then_replace(path: Path):
        nonlocal calls
        calls += 1
        config = original(path)
        config_path.write_text('{"REPLACED_CONFIG_CANARY":true}', encoding="utf-8")
        config_path.chmod(0o600)
        return config

    monkeypatch.setattr(migration_module, "_load_migration_config", load_then_replace)
    output = tmp_path / "direct-manifest.json"
    assert main(["--config", str(config_path), "--output", str(output)]) == 0
    assert calls == 1
    assert output.exists()
    assert "REPLACED_CONFIG_CANARY" not in output.read_text(encoding="utf-8")
    assert "dry-run manifest created" in capsys.readouterr().out

    valid_config, _, _ = _snapshot_config(tmp_path / "subprocess-case")
    script = Path(__file__).parents[2] / "scripts" / "plan_memory_acl_migration.py"
    process = subprocess.run(
        [
            sys.executable,
            str(script),
            "--config",
            str(valid_config),
            "--output",
            str((tmp_path / "subprocess-case" / "manifest.json")),
        ],
        cwd=Path(__file__).parents[2],
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    assert "dry-run manifest created" in process.stdout


def test_cli_output_path_failure_is_static_and_contains_no_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, _, _ = _snapshot_config(tmp_path)
    private_path_canary = "PRIVATE_OUTPUT_PATH_CANARY"
    output = tmp_path / private_path_canary / "manifest.json"

    assert main(["--config", str(config_path), "--output", str(output)]) == 2
    captured = capsys.readouterr()
    assert captured.err == "migration plan denied\n"
    assert private_path_canary not in captured.err
    assert "Traceback" not in captured.err
