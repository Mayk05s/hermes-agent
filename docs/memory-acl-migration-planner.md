# Memory ACL migration planner

`scripts/plan_memory_acl_migration.py` inventories an offline snapshot and
creates a deterministic review manifest. It has no apply mode and makes no
network calls. It must not be used as evidence that migration or activation has
occurred.

## Snapshot prerequisite

The operator must first create a complete backup/staging snapshot outside the
live Hermes root. SQLite databases must be checkpointed before capture; a
snapshot containing `-wal` or `-shm` sidecars is rejected. The snapshot must
include `metadata/backup-receipt.json`, and the migration config must pin the
SHA-256 digest of that receipt.

The snapshot root, every directory below it, every source file, the receipt,
the migration config, and the config's immediate parent directory must be
owned by the invoking user (or root) and inaccessible to group/other users
(`0700` directories and `0600` files are the recommended modes). Symlinks and
hard-linked files are rejected; the planner never changes input permissions.

The receipt format is:

```json
{
  "format": "hermes-memory-snapshot-receipt-v1",
  "backup_id": "backup-2026-09-02",
  "snapshot_complete": true,
  "sqlite_consistency": "checkpointed-no-wal",
  "files": [
    {
      "path": "profiles/personal/state.db",
      "sha256": "<lowercase sha256>",
      "size": 1234
    }
  ]
}
```

Every configured input must appear in the receipt with its exact size and
digest. Paths are snapshot-relative, and symlinks are rejected. A free-form
backup name or an unverified checksum assertion is not sufficient.

## Explicit mapping

The configuration is versioned and intentionally contains no defaults that can
guess ownership:

```json
{
  "schema_version": 1,
  "snapshot_root": "/offline/hermes-snapshot",
  "backup_receipt": {
    "path": "metadata/backup-receipt.json",
    "sha256": "<lowercase sha256>"
  },
  "sources": [
    {
      "kind": "memory_file",
      "source_namespace": "profile-personal-memory",
      "path": "profiles/personal/memories/MEMORY.md",
      "target_wing": "private.p_km4z2x7v5q3n6rta.personal"
    },
    {
      "kind": "session_db",
      "source_namespace": "profile-personal-sessions",
      "path": "profiles/personal/state.db",
      "access_scope_map": {
        "agent:main:telegram:dm:legacy-destination": {
          "source_kind": "private",
          "target_wing": "private.p_km4z2x7v5q3n6rta.personal"
        }
      }
    },
    {
      "kind": "legacy_kg_db",
      "source_namespace": "profile-personal-kg-main",
      "path": "profiles/personal/mempalace/mcp-storage/main/mempalace/knowledge_graph.sqlite3",
      "source_kind": "private",
      "target_wing": "private.p_km4z2x7v5q3n6rta.personal"
    }
  ]
}
```

`MEMORY.md` and `USER.md` can map only to private wings. Each session row is
matched by its complete legacy `access_scope`; NULL and unmapped scopes are
quarantined. Every legacy KG declares whether its source is private or group,
and its target must have the same kind. Service exports use the strict
`mempalace-offline-export-v1` format and declare an exact old-wing mapping with
the same private/group check. No palace, profile name, chat identifier, or
provenance string is used to infer a destination.

Source namespaces are operator-controlled identifiers and must not contain
names or other personal data. Their stable identity is preserved for audit as
a domain-separated digest. Raw record content, provenance, access scopes,
legacy IDs, namespaces, and paths are not written to the manifest; the planner
emits digests plus ordinal/table locators instead. The backup identifier and
receipt path are likewise emitted only as digests.

## Offline MemPalace export

There is no supported automatic exporter in this change. An operator must
create the export while preparing the offline snapshot; the planner never
connects to a live MemPalace service or database. Until a separately reviewed
read-only exporter exists, construct the snapshot artifact from the verified
backup copy and validate it against this exact shape:

```json
{
  "format": "mempalace-offline-export-v1",
  "records": [
    {
      "kind": "drawer",
      "id": "legacy-record-id",
      "wing": "legacy-personal",
      "payload": {"room": "personal", "content": "..."},
      "provenance": {"adapter": "legacy-import"}
    }
  ]
}
```

The top-level keys are exactly `format` and `records`. Every record has exactly
`kind`, `id`, `wing`, `payload`, and `provenance`; `kind` is one of `drawer`,
`document`, `kg_entity`, or `kg_triple`. `id` is a non-empty string, `wing` is
a legacy string or null, and payload/provenance must contain only finite JSON
values. The config's `wing_map` must explicitly map each non-null old wing and
declare `source_kind` (`private` or `group`) plus the exact target wing. Null
or unmapped wings are quarantined. Add the finished export's exact path, byte
size, and SHA-256 to the backup receipt before planning.

## Output and rollback

Run from the repository with the checked Python environment:

```text
python3 scripts/plan_memory_acl_migration.py --config migration.json --output manifest.json
```

The output must be outside the snapshot and is created with mode `0600`.
Its parent directory must also be owner-controlled and inaccessible to
group/other users.
Re-running with the same inputs accepts only a byte-identical manifest; a
different existing file is never overwritten.
The manifest reports counts, quarantine decisions, idempotency keys, and
content/provenance/destination collisions. It does not deduplicate records.
Every collision group, including an identical duplicate in one source, is a
future-apply blocker until an operator explicitly resolves it. Logical
idempotency keys do not include row ordinals, so reordering an offline export
or inserting another SQLite row cannot create a new identity for unchanged
content.

Any future apply implementation must reverify the named backup, receipt,
manifest, migration ID, and every per-record key. Rollback is disabling the
migration and restoring the verified backup. Destructive SQL or inferred
cleanup is not an allowed rollback mechanism.
