import json
import sqlite3
from pathlib import Path

from hermes_cli import mempalace


def _create_state_db(root: Path, sessions: list[dict], messages: list[dict]) -> Path:
    db_path = root / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                user_id TEXT,
                access_scope TEXT,
                title TEXT,
                started_at REAL,
                ended_at REAL,
                message_count INTEGER,
                cwd TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                content TEXT,
                tool_name TEXT,
                timestamp REAL,
                active INTEGER DEFAULT 1
            )
            """
        )
        for row in sessions:
            conn.execute(
                """
                INSERT INTO sessions
                    (id, source, user_id, access_scope, title, started_at, ended_at, message_count, cwd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row.get("source", "telegram"),
                    row.get("user_id"),
                    row.get("access_scope"),
                    row.get("title"),
                    row.get("started_at", 1.0),
                    row.get("ended_at"),
                    row.get("message_count", 1),
                    row.get("cwd", ""),
                ),
            )
        for row in messages:
            conn.execute(
                """
                INSERT INTO messages
                    (id, session_id, role, content, tool_name, timestamp, active)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["session_id"],
                    row.get("role", "user"),
                    row.get("content", ""),
                    row.get("tool_name"),
                    row.get("timestamp", float(row["id"])),
                    row.get("active", 1),
                ),
            )
        conn.commit()
    return db_path


def _write_sessions_index(root: Path, entries: dict[str, dict]) -> None:
    sessions_dir = root / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "sessions.json").write_text(json.dumps(entries), encoding="utf-8")


def test_generate_from_markdown_creates_profile_scoped_palace(tmp_path: Path):
    profile_home = tmp_path / "profile"
    tenant_root = tmp_path / "tenants"
    memory_dir = tenant_root / "telegram_health" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "knowledge.md").write_text(
        "# Health facts\n"
        "- User prefers salmon for dinner.\n"
        "- Training plan uses short daily pull-up sessions.\n",
        encoding="utf-8",
    )

    result = mempalace.generate_from_markdown(
        profile="test",
        profile_home=profile_home,
        tenant_roots=[tenant_root],
    )

    assert result["profile"] == "test"
    assert result["files"] == 1
    assert result["palaces"][0]["palace"] == "telegram_health"
    assert result["palaces"][0]["entries"] == 2

    palaces = mempalace.list_palaces(profile="test", profile_home=profile_home)
    assert palaces[0]["palace"] == "telegram_health"
    assert palaces[0]["entity_count"] >= 4
    assert palaces[0]["triple_count"] >= 3

    graph = mempalace.load_graph(
        "telegram_health",
        profile="test",
        profile_home=profile_home,
        query="Health",
    )
    assert graph["nodes"]
    assert any(node["attributes"] for node in graph["nodes"])


def test_search_and_recall_context_read_profile_storage(tmp_path: Path):
    profile_home = tmp_path / "profile"
    tenant_root = tmp_path / "tenants"
    memory_dir = tenant_root / "telegram_planning" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "knowledge.md").write_text(
        "# Product planning\n"
        "- Boxmap work depends on backend milestones.\n",
        encoding="utf-8",
    )
    mempalace.generate_from_markdown(
        profile="planner",
        profile_home=profile_home,
        tenant_roots=[tenant_root],
    )

    results = mempalace.search(
        "Boxmap",
        profile="planner",
        profile_home=profile_home,
        palace="telegram_planning",
    )

    assert results["results"]
    assert results["results"][0]["palace"] == "telegram_planning"

    context = mempalace.recall_context(
        "backend milestones",
        profile="planner",
        profile_home=profile_home,
    )
    assert "MemPalace recall:" in context
    assert "telegram_planning" in context


def test_named_profile_generation_uses_profile_local_memories_only(tmp_path: Path):
    profile_home = tmp_path / "profiles" / "family-chat"
    memories = profile_home / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text(
        "# Profile memory\n"
        "- Family chat uses isolated long-term memory.\n",
        encoding="utf-8",
    )

    result = mempalace.generate_from_markdown(
        profile="family-chat",
        profile_home=profile_home,
    )

    assert result["files"] == 1
    assert [row["palace"] for row in result["palaces"]] == ["hermes_profile"]

    palaces = mempalace.list_palaces(profile="family-chat", profile_home=profile_home)
    assert [row["palace"] for row in palaces] == ["hermes_profile"]
    assert palaces[0]["entity_count"] >= 2


def test_generate_from_history_extracts_chat_facts_by_profile_origin(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    db_path = _create_state_db(
        state_root,
        sessions=[
            {
                "id": "family-session",
                "source": "telegram",
                "title": "Family project planning",
                "message_count": 3,
            },
            {
                "id": "default-session",
                "source": "telegram",
                "title": "Default health planning",
                "message_count": 1,
            },
        ],
        messages=[
            {
                "id": 1,
                "session_id": "family-session",
                "role": "user",
                "content": "Important remember: BoxMap needs backend milestone tracking and the mobile health-check endpoint /api/healthz.",
            },
            {
                "id": 2,
                "session_id": "family-session",
                "role": "assistant",
                "content": "Done: added the BoxMap backend health-check plan for profile routing.",
            },
            {
                "id": 3,
                "session_id": "default-session",
                "role": "user",
                "content": "Important remember: default profile uses Mobility Plan for training schedule decisions.",
            },
        ],
    )
    _write_sessions_index(
        state_root,
        {
            "agent:main:telegram:group:-1001:42": {
                "session_id": "family-session",
                "display_name": "Family",
                "origin": {
                    "platform": "telegram",
                    "chat_id": "-1001",
                    "chat_type": "group",
                    "thread_id": "42",
                    "chat_topic": "planning",
                    "profile_name": "family-chat",
                    "scope_name": "default",
                    "memory_scope": "default",
                },
            },
            "agent:main:telegram:dm:local": {
                "session_id": "default-session",
                "display_name": "Default",
                "origin": {
                    "platform": "telegram",
                    "chat_id": "local",
                    "chat_type": "dm",
                },
            },
        },
    )

    family_home = tmp_path / "family-profile"
    result = mempalace.generate_from_history(
        profile="family-chat",
        profile_home=family_home,
        state_db_path=db_path,
        auto_clean=False,
    )

    assert result["sessions"] == 1
    assert result["facts"] == 2
    assert [row["palace"] for row in result["palaces"]] == ["history_telegram"]
    assert mempalace.search(
        "BoxMap",
        profile="family-chat",
        profile_home=family_home,
        palace="history_telegram",
    )["results"]

    default_home = tmp_path / "default-profile"
    default_result = mempalace.generate_from_history(
        profile="default",
        profile_home=default_home,
        state_db_path=db_path,
        auto_clean=False,
    )
    assert default_result["sessions"] == 1
    assert not mempalace.search(
        "BoxMap",
        profile="default",
        profile_home=default_home,
        palace="history_telegram",
    )["results"]


def test_profile_graph_subgraph_and_tree_work_without_required_palace(tmp_path: Path):
    profile_home = tmp_path / "profile"
    tenants = tmp_path / "tenants"
    health_dir = tenants / "telegram_health" / "memory"
    planning_dir = tenants / "telegram_planning" / "memory"
    health_dir.mkdir(parents=True)
    planning_dir.mkdir(parents=True)
    (health_dir / "knowledge.md").write_text(
        "# Health\n"
        "- Training plan uses short daily pull-up sessions.\n",
        encoding="utf-8",
    )
    (planning_dir / "knowledge.md").write_text(
        "# Planning\n"
        "- Boxmap depends on backend milestones.\n",
        encoding="utf-8",
    )
    mempalace.generate_from_markdown(
        profile="default",
        profile_home=profile_home,
        tenant_roots=[tenants],
    )

    graph = mempalace.load_graph(profile="default", profile_home=profile_home)

    assert graph["palace"] == ""
    assert graph["stats"]["topic_count"] == 2
    assert {node["palace"] for node in graph["nodes"]} == {
        "telegram_health",
        "telegram_planning",
    }
    assert all("::" in node["id"] for node in graph["nodes"])

    center = graph["nodes"][0]["id"]
    subgraph = mempalace.load_subgraph(
        center,
        profile="default",
        profile_home=profile_home,
        depth=1,
    )
    assert subgraph["nodes"]
    assert subgraph["topic"] in {"telegram_health", "telegram_planning"}

    tree = mempalace.node_tree(
        center,
        profile="default",
        profile_home=profile_home,
        depth=2,
    )
    assert tree["root"]["label"]
    assert tree["tree"]


def test_clean_noise_previews_backs_up_and_deletes_low_signal_entities(tmp_path: Path):
    profile_home = tmp_path / "profile"
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    db_path = mempalace._db_path(paths, "telegram_boxmap")
    with mempalace._connect_rw(db_path) as conn:
        mempalace._upsert_entity(
            conn,
            "вася",
            "Вася",
            "person",
            {"description": "специалист, backend tester"},
        )
        mempalace._upsert_entity(conn, "готов,_sonnet", "готов, sonnet", "unknown", {})
        mempalace._upsert_triple(
            conn,
            "t_vasya_status",
            "вася",
            "status",
            "готов,_sonnet",
            source_closet="tg:-1003735932411:6827",
            adapter_name="",
        )
        mempalace._upsert_entity(
            conn,
            "backend_tester",
            "backend_tester",
            "service",
            {"description": "QA backend tester"},
        )
        mempalace._upsert_entity(conn, "qa_backend", "qa backend", "unknown", {})
        mempalace._upsert_triple(
            conn,
            "t_backend_role",
            "backend_tester",
            "role",
            "qa_backend",
            source_closet="tg:-1003735932411:6827",
            adapter_name="",
        )
        conn.commit()

    preview = mempalace.clean_noise(
        profile="default",
        profile_home=profile_home,
        palace="telegram_boxmap",
        dry_run=True,
    )

    assert preview["total_candidates"] == 1
    assert preview["candidates"][0]["label"] == "Вася"

    result = mempalace.clean_noise(
        profile="default",
        profile_home=profile_home,
        palace="telegram_boxmap",
        dry_run=False,
        max_delete=10,
    )

    assert result["deleted_entities"] == 1
    assert result["deleted_triples"] == 1
    assert result["deleted_orphans"] >= 1
    assert result["backup_root"]
    assert Path(result["backup_root"]).exists()

    with mempalace._connect_readonly(db_path) as conn:
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'вася'").fetchone() is None
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'готов,_sonnet'").fetchone() is None
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'backend_tester'").fetchone()


def test_generate_from_markdown_auto_cleans_noise_in_touched_palace(tmp_path: Path):
    profile_home = tmp_path / "profile"
    tenant_root = tmp_path / "tenants"
    memory_dir = tenant_root / "telegram_boxmap" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "knowledge.md").write_text(
        "# BoxMap\n"
        "- BoxMap keeps durable project milestones in Hermes memory.\n",
        encoding="utf-8",
    )
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    db_path = mempalace._db_path(paths, "telegram_boxmap")
    with mempalace._connect_rw(db_path) as conn:
        mempalace._upsert_entity(conn, "вася", "Вася", "person", {"description": "backend tester"})
        mempalace._upsert_entity(conn, "готов,_sonnet", "готов, sonnet", "unknown", {})
        mempalace._upsert_triple(
            conn,
            "t_vasya_status",
            "вася",
            "status",
            "готов,_sonnet",
            source_closet="tg:-1003735932411:6827",
            adapter_name="",
        )
        conn.commit()

    result = mempalace.generate_from_markdown(
        profile="default",
        profile_home=profile_home,
        tenant_roots=[tenant_root],
    )

    auto_clean = result["auto_clean"]
    assert len(auto_clean) == 1
    assert auto_clean[0]["palace"] == "telegram_boxmap"
    assert auto_clean[0]["deleted_entities"] == 1
    assert auto_clean[0]["deleted_triples"] == 1
    assert auto_clean[0]["backup_root"]

    with mempalace._connect_readonly(db_path) as conn:
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'вася'").fetchone() is None
        assert conn.execute("SELECT 1 FROM triples WHERE id = 't_vasya_status'").fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM triples WHERE source_file LIKE '%knowledge.md'").fetchone()[0] >= 1


def test_import_auto_cleans_only_copied_palaces(tmp_path: Path):
    import_root = tmp_path / "imports"
    source_paths = mempalace.PalacePaths(
        profile="snapshot",
        profile_home=tmp_path / "snapshot-profile",
        storage_root=import_root / "snap-1" / "mcp-storage",
    )
    source_db = mempalace._db_path(source_paths, "telegram_boxmap")
    with mempalace._connect_rw(source_db) as conn:
        mempalace._upsert_entity(conn, "вася", "Вася", "person", {"description": "backend tester"})
        mempalace._upsert_entity(conn, "готов,_sonnet", "готов, sonnet", "unknown", {})
        mempalace._upsert_triple(
            conn,
            "t_vasya_status",
            "вася",
            "status",
            "готов,_sonnet",
            source_closet="tg:-1003735932411:6827",
            adapter_name="",
        )
        conn.commit()

    profile_home = tmp_path / "profile"
    dest_paths = mempalace.palace_paths("default", profile_home=profile_home)
    existing_db = mempalace._db_path(dest_paths, "telegram_existing")
    with mempalace._connect_rw(existing_db) as conn:
        mempalace._upsert_entity(conn, "петя", "Петя", "person", {"description": "temporary"})
        mempalace._upsert_entity(conn, "ready_sonnet", "ready sonnet", "unknown", {})
        mempalace._upsert_triple(
            conn,
            "t_petya_status",
            "петя",
            "status",
            "ready_sonnet",
            source_closet="tg:-1003735932411:6827",
            adapter_name="",
        )
        conn.commit()

    result = mempalace.copy_import_to_profile(
        profile="default",
        profile_home=profile_home,
        snapshot="snap-1",
        import_root=import_root,
    )

    assert result["copied"] == 1
    assert result["auto_clean"][0]["palace"] == "telegram_boxmap"
    assert result["auto_clean"][0]["deleted_entities"] == 1

    copied_db = mempalace._db_path(dest_paths, "telegram_boxmap")
    with mempalace._connect_readonly(copied_db) as conn:
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'вася'").fetchone() is None
    with mempalace._connect_readonly(existing_db) as conn:
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'петя'").fetchone()


def test_rebuild_from_markdown_replaces_existing_storage(tmp_path: Path):
    profile_home = tmp_path / "profile"
    tenant_root = tmp_path / "tenants"
    memory_dir = tenant_root / "telegram_boxmap" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "knowledge.md").write_text(
        "# BoxMap\n"
        "- BoxMap rebuild keeps only current markdown memory.\n",
        encoding="utf-8",
    )
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    old_db = mempalace._db_path(paths, "telegram_old")
    with mempalace._connect_rw(old_db) as conn:
        mempalace._upsert_entity(conn, "old_node", "Old node", "concept", {})
        conn.commit()

    result = mempalace.rebuild_from_markdown(
        profile="default",
        profile_home=profile_home,
        tenant_roots=[tenant_root],
    )

    assert result["removed_existing"] is True
    assert result["backup_root"]
    assert Path(result["backup_root"]).exists()
    palaces = mempalace.list_palaces(profile="default", profile_home=profile_home)
    assert [row["palace"] for row in palaces] == ["telegram_boxmap"]
    assert not old_db.exists()


def test_rebuild_from_history_uses_transcript_primary_and_markdown_overlay(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    db_path = _create_state_db(
        state_root,
        sessions=[
            {
                "id": "history-session",
                "source": "telegram",
                "title": "Project Atlas",
                "message_count": 1,
            },
        ],
        messages=[
            {
                "id": 1,
                "session_id": "history-session",
                "role": "user",
                "content": "Important remember: Project Atlas must rebuild MemPalace from chat history before curated files.",
            },
        ],
    )
    _write_sessions_index(
        state_root,
        {
            "agent:main:telegram:dm:atlas": {
                "session_id": "history-session",
                "origin": {
                    "platform": "telegram",
                    "chat_id": "atlas",
                    "chat_type": "dm",
                    "profile_name": "default",
                },
            }
        },
    )
    profile_home = tmp_path / "profile"
    tenant_root = tmp_path / "tenants"
    memory_dir = tenant_root / "telegram_notes" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "knowledge.md").write_text(
        "# Curated notes\n"
        "- Curated launch checklist stays as a markdown overlay.\n",
        encoding="utf-8",
    )
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    old_db = mempalace._db_path(paths, "telegram_old")
    with mempalace._connect_rw(old_db) as conn:
        mempalace._upsert_entity(conn, "old_node", "Old node", "concept", {})
        conn.commit()

    result = mempalace.rebuild_from_history(
        profile="default",
        profile_home=profile_home,
        state_db_path=db_path,
        tenant_roots=[tenant_root],
        auto_clean=False,
    )

    assert result["removed_existing"] is True
    assert result["history"]["facts"] == 1
    assert result["generated"]["files"] == 1
    palaces = {row["palace"] for row in mempalace.list_palaces(profile="default", profile_home=profile_home)}
    assert palaces == {"history_telegram", "telegram_notes"}
    assert not old_db.exists()
    assert "Project Atlas" in mempalace.recall_context(
        "Project Atlas",
        profile="default",
        profile_home=profile_home,
    )
    assert "Curated launch checklist" in mempalace.recall_context(
        "Curated launch checklist",
        profile="default",
        profile_home=profile_home,
    )


def test_refresh_if_due_rebuilds_only_after_interval_and_source_change(tmp_path: Path):
    profile_home = tmp_path / "profile"
    state_root = tmp_path / "state"
    state_root.mkdir()
    db_path = _create_state_db(state_root, sessions=[], messages=[])
    memories = profile_home / "memories"
    memories.mkdir(parents=True)
    memory_file = memories / "MEMORY.md"
    memory_file.write_text(
        "# Profile\n"
        "- First durable profile memory fact.\n",
        encoding="utf-8",
    )

    first = mempalace.refresh_if_due(
        profile="default",
        profile_home=profile_home,
        state_db_path=db_path,
        force=True,
    )
    assert first["refreshed"] is True

    skipped = mempalace.refresh_if_due(
        profile="default",
        profile_home=profile_home,
        state_db_path=db_path,
        interval_seconds=600,
    )
    assert skipped["skipped"] is True
    assert skipped["reason"] == "interval"

    memory_file.write_text(
        "# Profile\n"
        "- Second durable profile memory fact after edit.\n",
        encoding="utf-8",
    )
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    state = mempalace._read_refresh_state(paths)
    state["last_checked_epoch"] = 0
    mempalace._write_refresh_state(paths, state)

    refreshed = mempalace.refresh_if_due(
        profile="default",
        profile_home=profile_home,
        state_db_path=db_path,
        interval_seconds=60,
    )
    assert refreshed["refreshed"] is True
    assert refreshed["reason"] == "changed"
    assert "Second durable" in mempalace.recall_context(
        "Second durable",
        profile="default",
        profile_home=profile_home,
    )


def test_refresh_if_due_rebuilds_when_chat_history_changes(tmp_path: Path):
    profile_home = tmp_path / "profile"
    state_root = tmp_path / "state"
    state_root.mkdir()
    db_path = _create_state_db(
        state_root,
        sessions=[
            {
                "id": "history-session",
                "source": "telegram",
                "title": "History refresh",
                "message_count": 1,
            },
        ],
        messages=[
            {
                "id": 1,
                "session_id": "history-session",
                "role": "user",
                "content": "Important remember: Alpha Health Plan belongs in MemPalace history graph.",
            },
        ],
    )
    _write_sessions_index(
        state_root,
        {
            "agent:main:telegram:dm:alpha": {
                "session_id": "history-session",
                "origin": {
                    "platform": "telegram",
                    "chat_id": "alpha",
                    "chat_type": "dm",
                    "profile_name": "default",
                },
            }
        },
    )

    first = mempalace.refresh_if_due(
        profile="default",
        profile_home=profile_home,
        state_db_path=db_path,
        force=True,
    )
    assert first["refreshed"] is True
    assert "Alpha Health Plan" in mempalace.recall_context(
        "Alpha Health Plan",
        profile="default",
        profile_home=profile_home,
    )

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO messages (id, session_id, role, content, timestamp, active)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                2,
                "history-session",
                "user",
                "Important remember: Beta Dashboard owner prefers weekly status summaries.",
                2.0,
                1,
            ),
        )
        conn.execute("UPDATE sessions SET message_count = 2 WHERE id = ?", ("history-session",))
        conn.commit()
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    state = mempalace._read_refresh_state(paths)
    state["last_checked_epoch"] = 0
    mempalace._write_refresh_state(paths, state)

    refreshed = mempalace.refresh_if_due(
        profile="default",
        profile_home=profile_home,
        state_db_path=db_path,
        interval_seconds=60,
    )

    assert refreshed["refreshed"] is True
    assert refreshed["history_facts"] == 2
    assert "Beta Dashboard" in mempalace.recall_context(
        "Beta Dashboard",
        profile="default",
        profile_home=profile_home,
    )
