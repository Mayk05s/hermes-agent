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


def _fake_mempalace_llm(messages, **_kwargs) -> str:
    raw = messages[-1]["content"].split("\n\n", 1)[1]
    payload = json.loads(raw)
    text = "\n".join(item.get("content", "") for item in payload.get("messages", []))
    entities = []
    facts = []
    relations = []
    if "Alpha Health Plan" in text:
        entities.append({"name": "Alpha Health Plan", "type": "project", "description": "Health planning memory", "confidence": 0.92})
        facts.append({"subject": "Alpha Health Plan", "predicate": "belongs_in", "object": "MemPalace history graph", "confidence": 0.9, "evidence_message_ids": [1]})
    if "Beta Dashboard" in text:
        entities.append({"name": "Beta Dashboard", "type": "project", "description": "Dashboard project", "confidence": 0.91})
        facts.append({"subject": "Beta Dashboard", "predicate": "prefers", "object": "weekly status summaries", "confidence": 0.88, "evidence_message_ids": [2]})
    if "Mobile health-check endpoint" in text or "mobile health-check endpoint" in text:
        entities.extend(
            [
                {"name": "BoxMap", "type": "project", "description": "Project with backend milestone tracking", "confidence": 0.92},
                {"name": "Mobile health-check endpoint", "type": "service", "description": "/api/healthz", "confidence": 0.9},
            ]
        )
        relations.append({"subject": "BoxMap", "predicate": "uses", "object": "Mobile health-check endpoint", "confidence": 0.86, "evidence_message_ids": [1]})
    return json.dumps({"entities": entities, "facts": facts, "relations": relations, "contradictions": []}, ensure_ascii=False)


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
    assert [row["palace"] for row in result["palaces"]] == ["telegram_planning"]
    assert mempalace.search(
        "BoxMap",
        profile="family-chat",
        profile_home=family_home,
        palace="telegram_planning",
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


def test_history_palace_uses_json_origin_topic_and_thread_fallback(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    db_path = _create_state_db(
        state_root,
        sessions=[
            {
                "id": "cv-topic",
                "source": "telegram",
                "title": "CV work",
                "message_count": 1,
                "access_scope": json.dumps(
                    {
                        "session_key": "agent:main:telegram:group:-1001:777",
                        "origin": {
                            "platform": "telegram",
                            "chat_id": "-1001",
                            "chat_type": "group",
                            "thread_id": "777",
                            "chat_topic": "CV",
                            "profile_name": "default",
                        },
                    }
                ),
            },
            {
                "id": "raw-topic",
                "source": "telegram",
                "title": "Raw topic",
                "message_count": 1,
                "access_scope": json.dumps(
                    {
                        "session_key": "agent:main:telegram:group:-1001:888",
                        "origin": {
                            "platform": "telegram",
                            "chat_id": "-1001",
                            "chat_type": "group",
                            "thread_id": "888",
                            "profile_name": "default",
                        },
                    }
                ),
            },
        ],
        messages=[
            {
                "id": 1,
                "session_id": "cv-topic",
                "role": "user",
                "content": "Important remember: CV topic tracks resume rewrite and Java recruiter replies.",
            },
            {
                "id": 2,
                "session_id": "raw-topic",
                "role": "user",
                "content": "Important remember: raw Telegram topic keeps deployment notes separate.",
            },
        ],
    )
    profile_home = tmp_path / "profile"

    result = mempalace.generate_from_history(
        profile="default",
        profile_home=profile_home,
        state_db_path=db_path,
        auto_clean=False,
    )

    palaces = {row["palace"] for row in result["palaces"]}
    assert "telegram_cv" in palaces
    assert "tg_1001_888" in palaces
    assert mempalace.search(
        "resume rewrite",
        profile="default",
        profile_home=profile_home,
        palace="telegram_cv",
    )["results"]


def test_consolidate_profile_uses_llm_extractor_and_cursor(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    db_path = _create_state_db(
        state_root,
        sessions=[
            {
                "id": "family-session",
                "source": "telegram",
                "title": "Family project planning",
                "message_count": 1,
            },
        ],
        messages=[
            {
                "id": 1,
                "session_id": "family-session",
                "role": "user",
                "content": "Important remember: BoxMap uses the Mobile health-check endpoint /api/healthz.",
            },
        ],
    )
    _write_sessions_index(
        state_root,
        {
            "agent:main:telegram:group:-1001:42": {
                "session_id": "family-session",
                "origin": {
                    "platform": "telegram",
                    "chat_id": "-1001",
                    "chat_type": "group",
                    "thread_id": "42",
                    "profile_name": "family-chat",
                },
            },
        },
    )
    profile_home = tmp_path / "family-profile"

    result = mempalace.consolidate_profile(
        profile="family-chat",
        profile_home=profile_home,
        state_db_path=db_path,
        force=True,
        auto_clean=False,
        llm_call=_fake_mempalace_llm,
    )

    assert result["processed_messages"] == 1
    assert result["cursor_message_id"] == 1
    assert result["triples"] == 1
    assert "Mobile health-check endpoint" in mempalace.recall_context(
        "health-check",
        profile="family-chat",
        profile_home=profile_home,
    )

    skipped = mempalace.consolidate_profile(
        profile="family-chat",
        profile_home=profile_home,
        state_db_path=db_path,
        force=True,
        auto_clean=False,
        llm_call=_fake_mempalace_llm,
    )
    assert skipped["processed_messages"] == 0


def test_consolidate_profile_dry_run_does_not_write_cursor(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    db_path = _create_state_db(
        state_root,
        sessions=[{"id": "history-session", "source": "telegram", "title": "History", "message_count": 1}],
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
                "origin": {"platform": "telegram", "chat_id": "alpha", "chat_type": "dm", "profile_name": "default"},
            }
        },
    )
    profile_home = tmp_path / "profile"

    preview = mempalace.consolidate_profile(
        profile="default",
        profile_home=profile_home,
        state_db_path=db_path,
        dry_run=True,
        force=True,
        llm_call=_fake_mempalace_llm,
    )

    assert preview["processed_messages"] == 1
    assert preview["batches"][0]["palaces"][0]["entities"] == 1
    assert mempalace.consolidator_status(
        profile="default",
        profile_home=profile_home,
        state_db_path=db_path,
    )["cursor_message_id"] == 0
    assert mempalace.recall_context("Alpha Health Plan", profile="default", profile_home=profile_home) == ""


def test_consolidate_profile_auto_clean_uses_validator_not_deterministic_clean(tmp_path: Path, monkeypatch):
    state_root = tmp_path / "state"
    state_root.mkdir()
    db_path = _create_state_db(
        state_root,
        sessions=[{"id": "history-session", "source": "telegram", "title": "History", "message_count": 1}],
        messages=[
            {
                "id": 1,
                "session_id": "history-session",
                "role": "user",
                "content": "Important remember: Volatile Project status is ready by 2026-06-10.",
            },
        ],
    )
    _write_sessions_index(
        state_root,
        {
            "agent:main:telegram:dm:volatile": {
                "session_id": "history-session",
                "origin": {"platform": "telegram", "chat_id": "volatile", "chat_type": "dm", "profile_name": "default"},
            }
        },
    )
    profile_home = tmp_path / "profile"

    def fake_extractor(_messages, **_kwargs):
        return json.dumps(
            {
                "entities": [
                    {"name": "Volatile Project", "type": "project", "description": "Status-only candidate", "confidence": 0.9}
                ],
                "facts": [
                    {
                        "subject": "Volatile Project",
                        "predicate": "status",
                        "object": "ready by 2026-06-10",
                        "confidence": 0.8,
                        "evidence_message_ids": [1],
                    }
                ],
                "relations": [],
                "contradictions": [],
            },
            ensure_ascii=False,
        )

    def fail_deterministic_clean(**_kwargs):
        raise AssertionError("deterministic clean_noise must not run from consolidator auto-clean")

    validator_calls = []

    def fake_validator(messages, **_kwargs):
        validator_calls.append(messages)
        payload = json.loads(messages[-1]["content"])
        assert any(item["label"] == "Volatile Project" for item in payload["candidates"])
        return json.dumps(
            {
                "delete": [],
                "keep": [{"id": item["id"], "reason": "needs review"} for item in payload["candidates"]],
                "summary": "kept by validator",
            }
        )

    monkeypatch.setattr(mempalace, "clean_noise", fail_deterministic_clean)
    monkeypatch.setattr(mempalace, "_call_validator_llm", fake_validator)
    monkeypatch.setattr(mempalace, "_profile_route_lookup", lambda: ("default", {}))

    result = mempalace.consolidate_profile(
        profile="default",
        profile_home=profile_home,
        state_db_path=db_path,
        force=True,
        auto_clean=True,
        llm_call=fake_extractor,
    )

    assert validator_calls
    assert result["auto_clean"][0]["validator"] is True
    assert result["auto_clean"][0]["deleted_entities"] == 0
    assert "Volatile Project" in mempalace.recall_context(
        "Volatile Project",
        profile="default",
        profile_home=profile_home,
    )


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

    assert preview["total_candidates"] == 2
    assert {item["label"] for item in preview["candidates"]} == {"Вася", "готов, sonnet"}

    result = mempalace.clean_noise(
        profile="default",
        profile_home=profile_home,
        palace="telegram_boxmap",
        dry_run=False,
        max_delete=10,
    )

    assert result["deleted_entities"] == 2
    assert result["deleted_triples"] == 1
    assert result["deleted_orphans"] == 0
    assert result["backup_root"]
    assert Path(result["backup_root"]).exists()

    with mempalace._connect_readonly(db_path) as conn:
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'вася'").fetchone() is None
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'готов,_sonnet'").fetchone() is None
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'backend_tester'").fetchone()


def test_generate_from_markdown_auto_clean_uses_validator_not_deterministic_clean(tmp_path: Path, monkeypatch):
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

    def fail_deterministic_clean(**_kwargs):
        raise AssertionError("deterministic clean_noise must not run from markdown auto-clean")

    validator_calls = []

    def fake_validator(messages, **_kwargs):
        validator_calls.append(messages)
        payload = json.loads(messages[-1]["content"])
        assert any(item["label"] == "Вася" for item in payload["candidates"])
        return json.dumps({"delete": [], "keep": [], "summary": "kept by validator"})

    monkeypatch.setattr(mempalace, "clean_noise", fail_deterministic_clean)
    monkeypatch.setattr(mempalace, "_call_validator_llm", fake_validator)

    result = mempalace.generate_from_markdown(
        profile="default",
        profile_home=profile_home,
        tenant_roots=[tenant_root],
    )

    auto_clean = result["auto_clean"]
    assert len(auto_clean) == 1
    assert auto_clean[0]["palace"] == "telegram_boxmap"
    assert auto_clean[0]["validator"] is True
    assert auto_clean[0]["deleted_entities"] == 0
    assert validator_calls

    with mempalace._connect_readonly(db_path) as conn:
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'вася'").fetchone()
        assert conn.execute("SELECT 1 FROM triples WHERE id = 't_vasya_status'").fetchone() is None
        props = json.loads(conn.execute("SELECT properties FROM entities WHERE id = 'вася'").fetchone()[0])
        assert any(
            item.get("predicate") == "status" and item.get("value") == "готов, sonnet"
            for item in props.get("literal_facts", [])
        )
        assert conn.execute("SELECT COUNT(*) FROM triples WHERE source_file LIKE '%knowledge.md'").fetchone()[0] >= 1


def test_import_auto_clean_uses_validator_not_deterministic_clean(tmp_path: Path, monkeypatch):
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

    def fail_deterministic_clean(**_kwargs):
        raise AssertionError("deterministic clean_noise must not run from import auto-clean")

    validator_calls = []

    def fake_validator(messages, **_kwargs):
        validator_calls.append(messages)
        payload = json.loads(messages[-1]["content"])
        assert any(item["label"] == "Вася" for item in payload["candidates"])
        return json.dumps({"delete": [], "keep": [], "summary": "kept by validator"})

    monkeypatch.setattr(mempalace, "clean_noise", fail_deterministic_clean)
    monkeypatch.setattr(mempalace, "_call_validator_llm", fake_validator)

    result = mempalace.copy_import_to_profile(
        profile="default",
        profile_home=profile_home,
        snapshot="snap-1",
        import_root=import_root,
    )

    assert result["copied"] == 1
    assert result["auto_clean"][0]["palace"] == "telegram_boxmap"
    assert result["auto_clean"][0]["validator"] is True
    assert result["auto_clean"][0]["deleted_entities"] == 0
    assert validator_calls

    copied_db = mempalace._db_path(dest_paths, "telegram_boxmap")
    with mempalace._connect_readonly(copied_db) as conn:
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'вася'").fetchone()
    with mempalace._connect_readonly(existing_db) as conn:
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'петя'").fetchone()


def test_generate_from_history_auto_clean_uses_validator_not_deterministic_clean(tmp_path: Path, monkeypatch):
    state_root = tmp_path / "state"
    state_root.mkdir()
    db_path = _create_state_db(
        state_root,
        sessions=[{"id": "history-session", "source": "telegram", "title": "History", "message_count": 1}],
        messages=[
            {
                "id": 1,
                "session_id": "history-session",
                "role": "user",
                "content": "Important remember: BoxMap uses the Mobile health-check endpoint /api/healthz.",
            },
        ],
    )
    _write_sessions_index(
        state_root,
        {
            "agent:main:telegram:dm:boxmap": {
                "session_id": "history-session",
                "origin": {"platform": "telegram", "chat_id": "boxmap", "chat_type": "dm", "profile_name": "default"},
            }
        },
    )
    profile_home = tmp_path / "profile"

    def fail_deterministic_clean(**_kwargs):
        raise AssertionError("deterministic clean_noise must not run from history auto-clean")

    validator_calls = []

    def fake_validator(messages, **_kwargs):
        validator_calls.append(messages)
        payload = json.loads(messages[-1]["content"])
        assert payload["candidates"]
        return json.dumps({"delete": [], "keep": [], "summary": "kept by validator"})

    monkeypatch.setattr(mempalace, "clean_noise", fail_deterministic_clean)
    monkeypatch.setattr(mempalace, "_call_validator_llm", fake_validator)
    monkeypatch.setattr(mempalace, "_profile_route_lookup", lambda: ("default", {}))

    result = mempalace.generate_from_history(
        profile="default",
        profile_home=profile_home,
        state_db_path=db_path,
    )

    assert validator_calls
    assert result["auto_clean"][0]["validator"] is True
    assert result["auto_clean"][0]["deleted_entities"] == 0
    assert mempalace.search(
        "BoxMap",
        profile="default",
        profile_home=profile_home,
        palace="history_telegram",
    )["results"]


def test_partition_import_auto_clean_uses_validator_not_deterministic_clean(tmp_path: Path, monkeypatch):
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

    profile_root = tmp_path / "profiles"

    def fake_palace_paths(profile="default", *, profile_home=None):
        home = Path(profile_home) if profile_home is not None else profile_root / profile
        return mempalace.PalacePaths(
            profile=profile,
            profile_home=home,
            storage_root=home / "mempalace" / "mcp-storage",
        )

    def fail_deterministic_clean(**_kwargs):
        raise AssertionError("deterministic cleanup must not run from partition auto-clean")

    validator_calls = []

    def fake_validator(messages, **_kwargs):
        validator_calls.append(messages)
        payload = json.loads(messages[-1]["content"])
        assert any(item["label"] == "Вася" for item in payload["candidates"])
        return json.dumps({"delete": [], "keep": [], "summary": "kept by validator"})

    monkeypatch.setattr(mempalace, "list_profiles", lambda: [{"name": "default"}])
    monkeypatch.setattr(mempalace, "palace_paths", fake_palace_paths)
    monkeypatch.setattr(mempalace, "_profile_route_chat_map", lambda: {})
    monkeypatch.setattr(mempalace, "clean_noise", fail_deterministic_clean)
    monkeypatch.setattr(mempalace, "auto_clean_noise", fail_deterministic_clean)
    monkeypatch.setattr(mempalace, "_call_validator_llm", fake_validator)

    result = mempalace.partition_import_to_profiles(
        snapshot="snap-1",
        import_root=import_root,
        backup=False,
        regenerate=False,
    )

    assert validator_calls
    assert result["auto_clean"][0]["validator"] is True
    assert result["auto_clean"][0]["deleted_entities"] == 0
    copied_db = profile_root / "default" / "mempalace" / "mcp-storage" / "telegram_boxmap" / "mempalace" / "knowledge_graph.sqlite3"
    with mempalace._connect_readonly(copied_db) as conn:
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'вася'").fetchone()


def test_refresh_if_due_auto_clean_uses_validator_not_deterministic_clean(tmp_path: Path, monkeypatch):
    profile_home = tmp_path / "profile"
    memories = profile_home / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text(
        "# Profile\n"
        "- Refresh keeps curated profile memory.\n",
        encoding="utf-8",
    )
    state_root = tmp_path / "state"
    state_root.mkdir()
    db_path = _create_state_db(state_root, sessions=[], messages=[])
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    palace_db = mempalace._db_path(paths, "hermes_profile")
    with mempalace._connect_rw(palace_db) as conn:
        mempalace._upsert_entity(conn, "вася", "Вася", "person", {"description": "backend tester"})
        mempalace._upsert_entity(conn, "готов,_sonnet", "готов, sonnet", "unknown", {})
        mempalace._upsert_triple(conn, "t_vasya_status", "вася", "status", "готов,_sonnet")
        conn.commit()

    def fail_deterministic_clean(**_kwargs):
        raise AssertionError("deterministic clean_noise must not run from refresh auto-clean")

    validator_calls = []

    def fake_validator(messages, **_kwargs):
        validator_calls.append(messages)
        payload = json.loads(messages[-1]["content"])
        assert any(item["label"] == "Вася" for item in payload["candidates"])
        return json.dumps({"delete": [], "keep": [], "summary": "kept by validator"})

    monkeypatch.setattr(mempalace, "clean_noise", fail_deterministic_clean)
    monkeypatch.setattr(mempalace, "_call_validator_llm", fake_validator)
    monkeypatch.setattr(mempalace, "_profile_route_lookup", lambda: ("default", {}))

    result = mempalace.refresh_if_due(
        profile="default",
        profile_home=profile_home,
        state_db_path=db_path,
        force=True,
    )

    assert result["refreshed"] is True
    assert validator_calls
    with mempalace._connect_readonly(palace_db) as conn:
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'вася'").fetchone()


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
    mempalace.set_consolidator_auto_enabled(profile="default", profile_home=profile_home, enabled=True)

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


def test_refresh_if_due_consolidates_when_chat_history_changes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(mempalace, "_call_history_llm", _fake_mempalace_llm)
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
    mempalace.set_consolidator_auto_enabled(profile="default", profile_home=profile_home, enabled=True)

    refreshed = mempalace.refresh_if_due(
        profile="default",
        profile_home=profile_home,
        state_db_path=db_path,
        interval_seconds=60,
    )

    assert refreshed["refreshed"] is True
    assert refreshed["history_messages_processed"] == 1
    assert "Beta Dashboard" in mempalace.recall_context(
        "Beta Dashboard",
        profile="default",
        profile_home=profile_home,
    )


def test_scan_noise_includes_unknown_boolean_literals_and_validator_receives_them(tmp_path: Path):
    profile_home = tmp_path / "profile"
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    db_path = mempalace._db_path(paths, "history_telegram")
    with mempalace._connect_rw(db_path) as conn:
        mempalace._upsert_entity(conn, "boxmap", "BoxMap", "project", {"source_file": "curated.md"})
        mempalace._upsert_entity(conn, "literal_true", "true", "unknown", {})
        mempalace._upsert_entity(conn, "literal_false", "false", "unknown", {})
        mempalace._upsert_triple(conn, "t_true", "boxmap", "enabled", "literal_true")
        mempalace._upsert_triple(conn, "t_false", "boxmap", "active", "literal_false")
        conn.commit()

    preview = mempalace.scan_noise(
        profile="default",
        profile_home=profile_home,
        palace="history_telegram",
    )

    candidate_ids = {item["id"] for item in preview["candidates"]}
    assert {"literal_true", "literal_false"} <= candidate_ids

    calls = []

    def fake_validator(messages, **_kwargs):
        calls.append(messages)
        payload = json.loads(messages[-1]["content"])
        seen_ids = {item["id"] for item in payload["candidates"]}
        assert {"literal_true", "literal_false"} <= seen_ids
        return json.dumps({"delete": [], "keep": [], "summary": "checked"})

    result = mempalace.validate_and_clean_noise_with_llm(
        profile="default",
        profile_home=profile_home,
        palace="history_telegram",
        dry_run=True,
        llm_call=fake_validator,
    )

    assert calls
    assert result["total_candidates"] >= 2


def test_scan_noise_flags_transient_predicate_candidates(tmp_path: Path):
    profile_home = tmp_path / "profile"
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    db_path = mempalace._db_path(paths, "history_telegram")
    with mempalace._connect_rw(db_path) as conn:
        mempalace._upsert_entity(conn, "alex", "Alex", "person", {})
        mempalace._upsert_entity(conn, "home", "home", "unknown", {})
        mempalace._upsert_triple(conn, "t_today", "alex", "today", "home")
        conn.commit()

    preview = mempalace.scan_noise(
        profile="default",
        profile_home=profile_home,
        palace="history_telegram",
    )

    by_id = {item["id"]: item for item in preview["candidates"]}
    assert by_id["alex"]["reason"] == "transient predicate"


def test_validator_accepts_delete_string_id(tmp_path: Path):
    profile_home = tmp_path / "profile"
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    db_path = mempalace._db_path(paths, "history_telegram")
    with mempalace._connect_rw(db_path) as conn:
        mempalace._upsert_entity(conn, "boxmap", "BoxMap", "project", {})
        mempalace._upsert_entity(conn, "literal_true", "true", "unknown", {})
        mempalace._upsert_triple(conn, "t_true", "boxmap", "enabled", "literal_true")
        conn.commit()

    result = mempalace.validate_and_clean_noise_with_llm(
        profile="default",
        profile_home=profile_home,
        palace="history_telegram",
        dry_run=True,
        llm_call=lambda _messages, **_kwargs: json.dumps({"delete": ["literal_true"], "keep": [], "summary": "delete exact id"}),
    )

    assert result["status"] == "success"
    assert result["validator_contract_error"] is False
    assert [item["id"] for item in result["selected"]] == ["literal_true"]
    assert result["selected"][0]["validator_matched_by"] == "id"


def test_validator_maps_unique_label_delete_conservatively(tmp_path: Path):
    profile_home = tmp_path / "profile"
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    db_path = mempalace._db_path(paths, "history_telegram")
    with mempalace._connect_rw(db_path) as conn:
        mempalace._upsert_entity(conn, "unique_subject", "Unique Status Subject", "project", {})
        mempalace._upsert_entity(conn, "literal_ready", "ready", "unknown", {})
        mempalace._upsert_triple(conn, "t_ready", "unique_subject", "status", "literal_ready")
        conn.commit()

    result = mempalace.validate_and_clean_noise_with_llm(
        profile="default",
        profile_home=profile_home,
        palace="history_telegram",
        dry_run=True,
        llm_call=lambda _messages, **_kwargs: json.dumps(
            {"delete": [{"name": "Unique Status Subject", "reason": "status-only"}], "keep": [], "summary": "delete by unique label"}
        ),
    )

    assert result["status"] == "success"
    assert result["validator_contract_error"] is False
    assert [item["id"] for item in result["selected"]] == ["unique_subject"]
    assert result["selected"][0]["validator_matched_by"] == "unique_label"


def test_validator_does_not_delete_ambiguous_label_without_exact_id(tmp_path: Path):
    profile_home = tmp_path / "profile"
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    db_path = mempalace._db_path(paths, "history_telegram")
    with mempalace._connect_rw(db_path) as conn:
        mempalace._upsert_entity(conn, "boxmap_a", "BoxMap A", "project", {})
        mempalace._upsert_entity(conn, "boxmap_b", "BoxMap B", "project", {})
        mempalace._upsert_entity(conn, "literal_true_a", "true", "unknown", {})
        mempalace._upsert_entity(conn, "literal_true_b", "true", "unknown", {})
        mempalace._upsert_triple(conn, "t_true_a", "boxmap_a", "enabled", "literal_true_a")
        mempalace._upsert_triple(conn, "t_true_b", "boxmap_b", "enabled", "literal_true_b")
        conn.commit()

    result = mempalace.validate_and_clean_noise_with_llm(
        profile="default",
        profile_home=profile_home,
        palace="history_telegram",
        dry_run=True,
        llm_call=lambda _messages, **_kwargs: json.dumps(
            {"delete": [{"label": "true", "reason": "boolean literal"}], "keep": [], "summary": "delete true"}
        ),
    )

    assert result["status"] == "contract_error"
    assert result["validator_contract_error"] is True
    assert result["selected"] == []
    assert "ambiguous delete label" in result["validator_contract_errors"][0]


def test_validator_contract_error_when_summary_implies_delete_without_ids(tmp_path: Path):
    profile_home = tmp_path / "profile"
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    db_path = mempalace._db_path(paths, "history_telegram")
    with mempalace._connect_rw(db_path) as conn:
        mempalace._upsert_entity(conn, "boxmap", "BoxMap", "project", {})
        mempalace._upsert_entity(conn, "literal_true", "true", "unknown", {})
        mempalace._upsert_triple(conn, "t_true", "boxmap", "enabled", "literal_true")
        conn.commit()

    result = mempalace.validate_and_clean_noise_with_llm(
        profile="default",
        profile_home=profile_home,
        palace="history_telegram",
        dry_run=True,
        llm_call=lambda _messages, **_kwargs: json.dumps(
            {"delete": [], "keep": [], "summary": "Delete all obvious transient/status/debris candidates."}
        ),
    )

    assert result["status"] == "contract_error"
    assert result["validator_contract_error"] is True
    assert result["selected"] == []
    assert "summary implies deletion" in result["validator_contract_errors"][0]


def test_validator_compacts_kept_boolean_and_numeric_leaf_literals(tmp_path: Path):
    profile_home = tmp_path / "profile"
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    db_path = mempalace._db_path(paths, "history_telegram")
    with mempalace._connect_rw(db_path) as conn:
        mempalace._upsert_entity(conn, "mikhail", "Mikhail Shokolov", "person", {})
        mempalace._upsert_entity(conn, "literal_100", "100", "unknown", {})
        mempalace._upsert_entity(conn, "literal_true", "true", "unknown", {})
        mempalace._upsert_triple(
            conn,
            "t_protein",
            "mikhail",
            "daily_protein_goal_g",
            "literal_100",
            source_closet="state.db:history_telegram:1",
            adapter_name="hermes_history_llm",
        )
        mempalace._upsert_triple(
            conn,
            "t_mcp",
            "mikhail",
            "wants_custom_mcp_endpoints_for_regular_actions",
            "literal_true",
            source_closet="state.db:history_telegram:2",
            adapter_name="hermes_history_llm",
        )
        conn.commit()

    result = mempalace.validate_and_clean_noise_with_llm(
        profile="default",
        profile_home=profile_home,
        palace="history_telegram",
        llm_call=lambda _messages, **_kwargs: json.dumps(
            {"delete": [], "keep": [{"candidate_id": "literal_100"}, {"candidate_id": "literal_true"}], "summary": "keep useful facts"}
        ),
    )

    assert result["status"] == "success"
    assert result["deleted_entities"] == 0
    assert result["compacted_literals"] == 2
    assert result["compacted_triples"] == 2
    assert result["compaction_backup_root"]
    with mempalace._connect_readonly(db_path) as conn:
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'literal_100'").fetchone() is None
        assert conn.execute("SELECT 1 FROM triples WHERE id IN ('t_protein', 't_mcp')").fetchone() is None
        props = json.loads(conn.execute("SELECT properties FROM entities WHERE id = 'mikhail'").fetchone()[0])
    facts = props["literal_facts"]
    assert {item["predicate"]: item["value"] for item in facts} == {
        "daily_protein_goal_g": "100",
        "wants_custom_mcp_endpoints_for_regular_actions": "true",
    }
    assert mempalace.scan_noise(
        profile="default",
        profile_home=profile_home,
        palace="history_telegram",
    )["total_candidates"] == 0
    assert "daily_protein_goal_g: 100" in mempalace.recall_context(
        "daily_protein_goal",
        profile="default",
        profile_home=profile_home,
        palace="history_telegram",
    )


def test_validator_does_not_compact_non_leaf_literal(tmp_path: Path):
    profile_home = tmp_path / "profile"
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    db_path = mempalace._db_path(paths, "history_telegram")
    with mempalace._connect_rw(db_path) as conn:
        mempalace._upsert_entity(conn, "mikhail", "Mikhail Shokolov", "person", {})
        mempalace._upsert_entity(conn, "literal_100", "100", "unknown", {})
        mempalace._upsert_entity(conn, "grams", "grams", "concept", {})
        mempalace._upsert_triple(conn, "t_protein", "mikhail", "daily_protein_goal_g", "literal_100")
        mempalace._upsert_triple(conn, "t_unit", "literal_100", "has_unit", "grams")
        conn.commit()

    result = mempalace.validate_and_clean_noise_with_llm(
        profile="default",
        profile_home=profile_home,
        palace="history_telegram",
        llm_call=lambda _messages, **_kwargs: json.dumps(
            {"delete": [], "keep": [{"candidate_id": "literal_100"}], "summary": "keep non-leaf"}
        ),
    )

    assert result["compacted_literals"] == 0
    with mempalace._connect_readonly(db_path) as conn:
        assert conn.execute("SELECT 1 FROM entities WHERE id = 'literal_100'").fetchone()
        assert conn.execute("SELECT 1 FROM triples WHERE id = 't_protein'").fetchone()
    assert mempalace.scan_noise(
        profile="default",
        profile_home=profile_home,
        palace="history_telegram",
    )["total_candidates"] >= 1


def test_validator_status_records_last_validation(tmp_path: Path, monkeypatch):
    profile_home = tmp_path / "profile"
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    db_path = mempalace._db_path(paths, "history_telegram")
    with mempalace._connect_rw(db_path) as conn:
        mempalace._upsert_entity(conn, "boxmap", "BoxMap", "project", {})
        mempalace._upsert_entity(conn, "literal_true", "true", "unknown", {})
        mempalace._upsert_triple(conn, "t_true", "boxmap", "enabled", "literal_true")
        conn.commit()
    monkeypatch.setattr(mempalace, "_profile_route_lookup", lambda: ("default", {}))

    result = mempalace.validate_and_clean_noise_with_llm(
        profile="default",
        profile_home=profile_home,
        palace="history_telegram",
        llm_call=lambda _messages, **_kwargs: json.dumps({"delete": [], "keep": [], "summary": "kept and compacted"}),
    )
    status = mempalace.consolidator_status(profile="default", profile_home=profile_home)

    assert result["finished_at"]
    assert status["last_validation_finished_at"] == result["finished_at"]
    assert status["last_validation_status"] == "success"
    assert status["last_validation_summary"] == "kept and compacted"
    assert status["last_validation_compacted_literals"] == 1


def test_llm_extraction_skips_boolean_literals_low_signal_relations_and_records_evidence(tmp_path: Path):
    profile_home = tmp_path / "profile"
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    extraction = {
        "entities": [
            {"name": "BoxMap", "type": "project", "description": "Project memory", "confidence": 0.94},
            {
                "name": "Mobile health-check endpoint",
                "type": "service",
                "description": "/api/healthz",
                "confidence": 0.9,
            },
            {"name": "true", "type": "unknown", "description": "true", "confidence": 0.2},
        ],
        "facts": [
            {
                "subject": "BoxMap",
                "predicate": "uses",
                "object": "Mobile health-check endpoint",
                "confidence": 0.88,
                "evidence_message_ids": [42],
            },
            {
                "subject": "BoxMap",
                "predicate": "enabled",
                "object": "true",
                "confidence": 0.5,
                "evidence_message_ids": [42],
            },
            {
                "subject": "BoxMap",
                "predicate": "current_status",
                "object": "ready",
                "confidence": 0.5,
                "evidence_message_ids": [42],
            },
        ],
        "relations": [
            {
                "subject": "Alice",
                "predicate": "talks_to",
                "object": "Bob",
                "confidence": 0.4,
                "evidence_message_ids": [42],
            }
        ],
        "contradictions": [],
    }

    counts = mempalace._write_llm_extraction(
        paths=paths,
        palace="history_telegram",
        extraction=extraction,
        batch_messages=[{"id": 42, "content": "Important remember: BoxMap uses the Mobile health-check endpoint."}],
    )

    assert counts == {"entities": 2, "triples": 1, "skipped": 3}

    db_path = mempalace._db_path(paths, "history_telegram")
    with mempalace._connect_readonly(db_path) as conn:
        assert conn.execute("SELECT 1 FROM entities WHERE LOWER(name) = 'true'").fetchone() is None
        assert conn.execute("SELECT 1 FROM triples WHERE predicate = 'talks_to'").fetchone() is None
        row = conn.execute("SELECT predicate, evidence FROM triples WHERE predicate = 'uses'").fetchone()

    assert row["predicate"] == "uses"
    evidence = json.loads(row["evidence"])
    assert evidence["message_ids"] == [42]
    assert evidence["source_closet"] == "state.db:history_telegram:42-42"


def test_llm_extraction_requires_explicit_in_batch_triple_evidence(tmp_path: Path):
    profile_home = tmp_path / "profile"
    paths = mempalace.palace_paths("default", profile_home=profile_home)
    extraction = {
        "entities": [
            {"name": "BoxMap", "type": "project", "description": "Project memory", "confidence": 0.94},
            {"name": "Endpoint A", "type": "service", "description": "Valid endpoint", "confidence": 0.9},
            {"name": "Endpoint B", "type": "service", "description": "No evidence endpoint", "confidence": 0.9},
            {"name": "Endpoint C", "type": "service", "description": "Wrong evidence endpoint", "confidence": 0.9},
        ],
        "facts": [
            {
                "subject": "BoxMap",
                "predicate": "uses",
                "object": "Endpoint A",
                "confidence": 0.88,
                "evidence_message_ids": [42, 99, 42],
            },
            {
                "subject": "BoxMap",
                "predicate": "uses",
                "object": "Endpoint B",
                "confidence": 0.88,
            },
            {
                "subject": "BoxMap",
                "predicate": "uses",
                "object": "Endpoint C",
                "confidence": 0.88,
                "evidence_message_ids": [12345],
            },
        ],
        "relations": [
            {
                "subject": "BoxMap",
                "predicate": "depends_on",
                "object": "Endpoint A",
                "confidence": 0.8,
                "evidence_message_ids": [99],
            },
            {
                "subject": "Endpoint B",
                "predicate": "depends_on",
                "object": "Endpoint C",
                "confidence": 0.8,
                "evidence_message_ids": [777],
            },
        ],
        "contradictions": [],
    }

    counts = mempalace._write_llm_extraction(
        paths=paths,
        palace="history_telegram",
        extraction=extraction,
        batch_messages=[
            {"id": 42, "content": "Important remember: BoxMap uses Endpoint A."},
            {"id": 99, "content": "Important remember: BoxMap depends on Endpoint A."},
        ],
    )

    assert counts["triples"] == 2
    assert counts["skipped"] == 3

    db_path = mempalace._db_path(paths, "history_telegram")
    with mempalace._connect_readonly(db_path) as conn:
        rows = conn.execute("SELECT predicate, evidence FROM triples ORDER BY predicate").fetchall()

    evidence_by_predicate = {row["predicate"]: json.loads(row["evidence"])["message_ids"] for row in rows}
    assert evidence_by_predicate == {
        "depends_on": [99],
        "uses": [42, 99],
    }
