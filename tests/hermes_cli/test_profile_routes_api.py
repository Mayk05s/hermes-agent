from fastapi.testclient import TestClient


def _client(ws):
    client = TestClient(ws.app)
    client.headers[ws._SESSION_HEADER_NAME] = ws._SESSION_TOKEN
    return client


def test_profile_routes_get_returns_default(monkeypatch):
    import hermes_cli.web_server as ws

    monkeypatch.setattr(ws, "load_config", lambda: {})
    client = _client(ws)

    response = client.get("/api/profile-routes")

    assert response.status_code == 200
    assert response.json()["default_profile"] == "default"
    assert response.json()["routes"] == []


def test_profile_routes_put_persists_normalized_config(monkeypatch):
    import hermes_cli.web_server as ws
    from hermes_cli import profiles as profiles_mod

    saved = {}
    monkeypatch.setattr(ws, "load_config", lambda: {})
    monkeypatch.setattr(ws, "save_config", lambda cfg: saved.update(cfg))
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: name in {"planning"})

    client = _client(ws)
    response = client.put(
        "/api/profile-routes",
        json={
            "default_profile": "default",
            "routes": [
                {
                    "id": "telegram-main",
                    "platform": "telegram",
                    "chat_id": "-1001",
                    "thread_id": "63",
                    "profile": "planning",
                    "label": "Planning",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert saved["profile_routes"]["routes"][0]["profile"] == "planning"
    assert saved["profile_routes"]["routes"][0]["thread_id"] == "63"


def test_profile_routes_put_rejects_invalid_profile(monkeypatch):
    import hermes_cli.web_server as ws

    monkeypatch.setattr(ws, "load_config", lambda: {})
    client = _client(ws)

    response = client.put(
        "/api/profile-routes",
        json={
            "default_profile": "default",
            "routes": [{"platform": "telegram", "chat_id": "-1001", "profile": "../escape"}],
        },
    )

    assert response.status_code == 400
    assert "Invalid profile name" in response.json()["detail"]


def test_profile_routes_put_rejects_missing_profile(monkeypatch):
    import hermes_cli.web_server as ws
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(ws, "load_config", lambda: {})
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: False)
    client = _client(ws)

    response = client.put(
        "/api/profile-routes",
        json={
            "default_profile": "default",
            "routes": [{"platform": "telegram", "chat_id": "-1001", "profile": "work"}],
        },
    )

    assert response.status_code == 400
    assert "does not exist" in response.json()["detail"]


def test_profile_routes_discovered_reads_session_origins(monkeypatch, tmp_path):
    import json
    import hermes_cli.web_server as ws

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "sessions.json").write_text(
        json.dumps({
            "agent:main:telegram:group:-1001:63": {
                "updated_at": "2026-06-03T10:00:00",
                "display_name": "Work Chat",
                "platform": "telegram",
                "chat_type": "group",
                "origin": {
                    "platform": "telegram",
                    "chat_id": "-1001",
                    "chat_name": "Work Chat",
                    "chat_type": "group",
                    "thread_id": "63",
                    "chat_topic": "Planning",
                },
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(ws, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(ws, "load_config", lambda: {
        "profile_routes": {
            "default_profile": "default",
            "routes": [
                {"id": "chat", "platform": "telegram", "chat_id": "-1001", "profile": "work"}
            ],
        }
    })

    response = _client(ws).get("/api/profile-routes/discovered")

    assert response.status_code == 200
    items = response.json()["items"]
    assert {item["thread_id"] for item in items} == {"", "63"}
    topic = next(item for item in items if item["thread_id"] == "63")
    assert topic["label"] == "Work Chat / Planning"
    assert topic["chat_topic"] == "Planning"
    assert topic["effective_profile"] == "work"
    assert topic["match_type"] == "chat"


def test_profile_chat_settings_get_put(monkeypatch):
    import hermes_cli.web_server as ws

    saved = {}
    monkeypatch.setattr(ws, "load_config", lambda: {
        "chat_settings": {
            "settings": [
                {
                    "platform": "telegram",
                    "chat_id": "-1001",
                    "response_mode": "mentions",
                    "transcribe_audio": "off",
                }
            ]
        }
    })
    monkeypatch.setattr(ws, "save_config", lambda cfg: saved.update(cfg))
    client = _client(ws)

    response = client.get("/api/profile-routes/chat-settings")

    assert response.status_code == 200
    assert response.json()["defaults"]["response_mode"] == "default"
    assert response.json()["settings"][0]["id"] == "telegram:-1001:"
    assert response.json()["settings"][0]["response_mode"] == "mentions"

    response = client.put(
        "/api/profile-routes/chat-settings",
        json={
            "defaults": {
                "response_mode": "mentions",
                "tool_progress": "off",
                "show_reasoning": "on",
            },
            "settings": [
                {
                    "platform": "telegram",
                    "chat_id": "-1002",
                    "response_mode": "all",
                    "transcribe_audio": "on",
                    "reply_to_mode": "off",
                    "label": "Ops",
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert saved["chat_settings"]["defaults"]["response_mode"] == "mentions"
    assert saved["chat_settings"]["defaults"]["tool_progress"] == "off"
    assert saved["chat_settings"]["defaults"]["show_reasoning"] == "on"
    assert saved["chat_settings"]["settings"][0]["id"] == "telegram:-1002:"
    assert saved["chat_settings"]["settings"][0]["label"] == "Ops"
    assert saved["chat_settings"]["settings"][0]["reply_to_mode"] == "off"


def test_profile_communication_style_get_put(monkeypatch, tmp_path):
    import yaml
    import hermes_cli.web_server as ws
    from hermes_cli import profiles as profiles_mod

    styles_dir = tmp_path / "communication-styles"
    styles_dir.mkdir()
    style_file = styles_dir / "default.md"
    style_file.write_text("# Shared style\n\nSpeak plainly.", encoding="utf-8")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "config.yaml").write_text(
        yaml.safe_dump({"communication_style": {"style": "default"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ws, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(ws, "_resolve_profile_dir", lambda name: profile_dir)
    client = _client(ws)

    response = client.get("/api/communication-styles")

    assert response.status_code == 200
    assert response.json()["styles"][0]["style"] == "default"
    assert response.json()["styles"][0]["label"] == "Shared style"

    response = client.get("/api/profiles/work/communication-style")

    assert response.status_code == 200
    assert response.json()["style"] == "default"
    assert response.json()["label"] == "Shared style"
    assert response.json()["file"] == str(style_file)
    assert response.json()["exists"] is True
    assert "Speak plainly" in response.json()["content"]

    response = client.put(
        "/api/profiles/work/communication-style",
        json={"style": "default"},
    )

    assert response.status_code == 200
    data = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert data["communication_style"]["style"] == "default"
    assert response.json()["exists"] is True

    response = client.put(
        "/api/communication-styles/default",
        json={"content": "# Updated style\n\nSpeak with care."},
    )

    assert response.status_code == 200
    assert response.json()["label"] == "Updated style"
    assert "Speak with care" in style_file.read_text(encoding="utf-8")

    response = client.post(
        "/api/communication-styles",
        json={"style": "work-direct"},
    )

    assert response.status_code == 200
    assert response.json()["style"] == "work-direct"
    assert (styles_dir / "work-direct.md").exists()


def test_profile_skills_are_profile_scoped(monkeypatch, tmp_path):
    import yaml
    import hermes_cli.web_server as ws

    profile_dir = tmp_path / "profile"
    skill_dir = profile_dir / "skills" / "family"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: family-memory\ndescription: Family-only memory skill\n---\n\n# Skill\n",
        encoding="utf-8",
    )
    (profile_dir / "config.yaml").write_text(
        yaml.safe_dump({"skills": {"disabled": ["family-memory"]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ws, "_resolve_profile_dir", lambda name: profile_dir)
    client = _client(ws)

    response = client.get("/api/profiles/family/skills")

    assert response.status_code == 200
    assert response.json()["skills"][0]["name"] == "family-memory"
    assert response.json()["skills"][0]["enabled"] is False

    response = client.put(
        "/api/profiles/family/skills/toggle",
        json={"name": "family-memory", "enabled": True},
    )

    assert response.status_code == 200
    data = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert data["skills"]["disabled"] == []
