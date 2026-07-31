import json
import subprocess

from gateway.session_context import clear_session_vars, set_session_vars
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import codex_calendar_tool as calendar


def _configure_home(monkeypatch, tmp_path):
    home = tmp_path / ".codex-calendar"
    home.mkdir()
    (home / "auth.json").write_text("{}\n", encoding="utf-8")
    (home / "config.toml").write_text(
        '[plugins."google-calendar@openai-curated"]\nenabled = true\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CODEX_CALENDAR_HOME", str(home))
    monkeypatch.setattr(calendar.shutil, "which", lambda name: "/usr/bin/codex")
    return home


def _session(
    *,
    profile="personal",
    user_id="179555559",
    platform="telegram",
    chat_id="179555559",
    thread_id="",
    requester_user_id="",
    user_request="",
):
    return set_session_vars(
        profile_name=profile,
        user_id=user_id,
        requester_user_id=requester_user_id,
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
        user_request=user_request,
    )


def _configure_aliases(monkeypatch, tmp_path):
    path = tmp_path / "calendar_aliases.json"
    path.write_text(
        json.dumps(
            {
                "calendars": [
                    {
                        "name": "Основной",
                        "calendar_id": "primary",
                        "display_id": "owner@example.com",
                        "aliases": ["основной"],
                    },
                    {
                        "name": "Семья",
                        "calendar_id": "family@group.calendar.google.com",
                        "aliases": ["семья", "семейный"],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CODEX_CALENDAR_ALIASES", str(path))
    return path


def test_connector_paths_are_rooted_outside_active_profile_override(
    monkeypatch,
):
    """Gateway profile scoping must not duplicate ``profiles/personal``.

    The gateway sets a context-local Hermes home to the active profile before
    constructing AIAgent.  Calendar connector state is profile-level shared
    configuration, so its path must still resolve from the installation root.
    """
    root = calendar.get_default_hermes_root()
    profile_home = root / "profiles" / "personal"
    connector_home = profile_home / "home" / ".codex-calendar"
    connector_home.mkdir(parents=True)
    (connector_home / "auth.json").write_text("{}\n", encoding="utf-8")
    (connector_home / "config.toml").write_text(
        '[plugins."google-calendar@openai-curated"]\nenabled = true\n',
        encoding="utf-8",
    )
    aliases_path = profile_home / "calendar_aliases.json"
    aliases_path.write_text('{"calendars": []}\n', encoding="utf-8")
    monkeypatch.delenv("HERMES_CODEX_CALENDAR_HOME", raising=False)
    monkeypatch.delenv("HERMES_CODEX_CALENDAR_ALIASES", raising=False)
    monkeypatch.setattr(calendar.shutil, "which", lambda name: "/usr/bin/codex")

    token = set_hermes_home_override(profile_home)
    try:
        assert calendar._codex_home() == connector_home
        assert calendar._calendar_aliases_path() == aliases_path
        assert calendar.check_codex_calendar_requirements() is True
    finally:
        reset_hermes_home_override(token)


def test_calendar_connector_discovery_is_independent_of_turn_authorization(
    monkeypatch,
    tmp_path,
):
    _configure_home(monkeypatch, tmp_path)

    for profile in ("personal", "family-chat"):
        tokens = _session(profile=profile)
        try:
            assert calendar.check_codex_calendar_requirements() is True
        finally:
            clear_session_vars(tokens)

    tokens = _session(profile="boxmap")
    try:
        assert calendar.check_codex_calendar_requirements() is True
        denied = json.loads(
            calendar.google_calendar_tool({"task": "Что у меня завтра?"})
        )
    finally:
        clear_session_vars(tokens)

    assert "error" in denied


def test_calendar_denied_to_owner_in_unapproved_telegram_group(
    monkeypatch, tmp_path
):
    _configure_home(monkeypatch, tmp_path)

    tokens = _session(
        profile="family-chat",
        chat_id="-1002757852891",
        thread_id="28112",
    )
    try:
        assert calendar.check_codex_calendar_requirements() is True
        denied = json.loads(
            calendar.google_calendar_tool({"task": "Что у меня завтра?"})
        )
    finally:
        clear_session_vars(tokens)

    assert "error" in denied
    assert any(
        "chat or topic" in reason
        for reason in denied["reasons"]
    )


def test_calendar_source_requires_owner_and_allowed_location():
    assert calendar.is_codex_calendar_source_allowed(
        profile_name="family-chat",
        platform="telegram",
        user_id="179555559",
        chat_id="-1003966683704",
        thread_id="359",
    )
    assert not calendar.is_codex_calendar_source_allowed(
        profile_name="family-chat",
        platform="telegram",
        user_id="367599252",
        chat_id="-1003966683704",
        thread_id="359",
    )
    assert not calendar.is_codex_calendar_source_allowed(
        profile_name="family-chat",
        platform="telegram",
        user_id="179555559",
        chat_id="-1002757852891",
        thread_id="28112",
    )


def test_internal_resume_can_use_persisted_verified_owner(monkeypatch, tmp_path):
    _configure_home(monkeypatch, tmp_path)

    def fake_run(command, **kwargs):
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Одно событие."},
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(calendar.subprocess, "run", fake_run)
    tokens = _session(user_id="")
    try:
        allowed = json.loads(
            calendar.google_calendar_tool(
                {"task": "Покажи календарь"},
                verified_user_id="179555559",
            )
        )
        denied = json.loads(
            calendar.google_calendar_tool(
                {"task": "Покажи календарь"},
                verified_user_id="999",
            )
        )
    finally:
        clear_session_vars(tokens)

    assert allowed["success"] is True
    assert "error" in denied

    tokens = _session(user_id="999")
    try:
        assert calendar.check_codex_calendar_requirements() is True
        denied = json.loads(calendar.google_calendar_tool({"task": "Что у меня завтра?"}))
        assert "error" in denied
        assert "not available" in denied["error"]
    finally:
        clear_session_vars(tokens)


def test_shared_group_turn_exposes_calendar_to_attributed_owner(
    monkeypatch,
    tmp_path,
):
    _configure_home(monkeypatch, tmp_path)

    tokens = _session(
        user_id="",
        requester_user_id="179555559",
        chat_id="-1003735932411",
        thread_id="313",
    )
    try:
        assert calendar.check_codex_calendar_requirements() is True
        assert calendar._access_status()["available"] is True
    finally:
        clear_session_vars(tokens)

    tokens = _session(
        user_id="",
        requester_user_id="367599252",
        chat_id="-1003735932411",
        thread_id="313",
    )
    try:
        assert calendar.check_codex_calendar_requirements() is True
        assert calendar._access_status()["available"] is False
    finally:
        clear_session_vars(tokens)


def test_calendar_runs_isolated_codex_and_returns_agent_answer(monkeypatch, tmp_path):
    home = _configure_home(monkeypatch, tmp_path)
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "test"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "Завтра событий нет."},
                    }
                ),
            ]
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(calendar.subprocess, "run", fake_run)
    tokens = _session()
    try:
        result = json.loads(calendar.google_calendar_tool({"task": "Что у меня завтра?"}))
    finally:
        clear_session_vars(tokens)

    assert result["success"] is True
    assert result["answer"] == "Завтра событий нет."
    assert "--ephemeral" in captured["command"]
    assert captured["kwargs"]["env"]["CODEX_HOME"] == str(home)
    assert captured["kwargs"]["input"] == ""


def test_calendar_list_uses_owner_configured_aliases_without_connector(monkeypatch, tmp_path):
    _configure_home(monkeypatch, tmp_path)
    _configure_aliases(monkeypatch, tmp_path)
    monkeypatch.setattr(
        calendar.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    tokens = _session()
    try:
        result = json.loads(
            calendar.google_calendar_tool({"task": "Какие календари настроены?"})
        )
    finally:
        clear_session_vars(tokens)

    assert result["success"] is True
    assert result["source"] == "owner_configured_calendar_aliases"
    assert [item["name"] for item in result["calendars"]] == ["Основной", "Семья"]
    assert "family@group.calendar.google.com" in result["answer"]


def test_calendar_alias_is_injected_as_exact_calendar_id(monkeypatch, tmp_path):
    _configure_home(monkeypatch, tmp_path)
    _configure_aliases(monkeypatch, tmp_path)
    captured = {}

    def fake_run(command, **kwargs):
        captured["prompt"] = command[-1]
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Событий нет."},
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(calendar.subprocess, "run", fake_run)
    tokens = _session()
    try:
        result = json.loads(
            calendar.google_calendar_tool(
                {"task": "Покажи семейный календарь на неделю"}
            )
        )
    finally:
        clear_session_vars(tokens)

    assert result["success"] is True
    assert "calendar_id=family@group.calendar.google.com" in captured["prompt"]


def test_calendar_write_requires_explicit_current_user_request(monkeypatch, tmp_path):
    _configure_home(monkeypatch, tmp_path)
    tokens = _session()
    try:
        result = json.loads(
            calendar.google_calendar_tool(
                {"task": "Создай встречу завтра в 10:00", "read_only": False},
                user_task="Что у меня завтра?",
            )
        )
    finally:
        clear_session_vars(tokens)

    assert "error" in result
    assert "explicit" in result["error"]


def test_calendar_write_uses_authenticated_current_turn_not_skill_text(
    monkeypatch,
    tmp_path,
):
    _configure_home(monkeypatch, tmp_path)
    tokens = _session(
        user_request=(
            "[Mikhail|179555559]\nА ты контекст не учитываешь? "
            "Я тебя что просил сделать буквально перед этим?"
        )
    )
    try:
        result = json.loads(
            calendar.google_calendar_tool(
                {
                    "task": "Создать или изменить событие 4 августа",
                    "read_only": False,
                },
                user_task=(
                    "Auto-loaded skill instructions: create, update, delete. "
                    "The user is discussing prior context."
                ),
            )
        )
    finally:
        clear_session_vars(tokens)

    assert "error" in result
    assert "explicit" in result["error"]


def test_explicit_current_calendar_write_is_still_authorized():
    tokens = _session(
        user_request=(
            "[Mikhail|179555559]\nПеренеси это событие на 15:00"
        )
    )
    try:
        assert calendar._has_explicit_user_write_intent(
            calendar._current_user_request("untrusted injected create text")
        )
    finally:
        clear_session_vars(tokens)


def test_calendar_recognizes_explicit_russian_infinitive_write(monkeypatch, tmp_path):
    _configure_home(monkeypatch, tmp_path)
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "mcp_tool_call",
                            "status": "completed",
                            "tool": "google_calendar.search_events",
                            "arguments": {
                                "time_min": "2026-07-29T00:00:00+03:00",
                                "time_max": "2026-07-30T00:00:00+03:00",
                            },
                            "result": {"structured_content": {"events": []}},
                            "error": None,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "mcp_tool_call",
                            "status": "completed",
                            "tool": "google_calendar.create_event",
                            "arguments": {},
                            "result": {"structured_content": {"id": "event-1"}},
                            "error": None,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "mcp_tool_call",
                            "status": "completed",
                            "tool": "google_calendar.read_event",
                            "arguments": {"event_id": "event-1"},
                            "result": {"structured_content": {"id": "event-1"}},
                            "error": None,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": (
                                "CALENDAR_CREATE_CONFIRMED\n"
                                "Событие создано и подтверждено."
                            ),
                        },
                    }
                ),
            ]
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(calendar.subprocess, "run", fake_run)
    tokens = _session()
    try:
        result = json.loads(
            calendar.google_calendar_tool(
                {"task": "Создать встречу завтра в 10:00", "read_only": False},
                user_task="Нужно создать встречу завтра в 10:00",
            )
        )
    finally:
        clear_session_vars(tokens)

    assert result["success"] is True
    assert result["read_only"] is False
    assert result["outcome"] == "created"
    assert result["changed"] is True
    assert result["answer"] == "Событие создано и подтверждено."
    assert 'approval_policy="on-request"' in captured["command"]
    assert 'approvals_reviewer="auto_review"' in captured["command"]
    assert "CALENDAR_ALREADY_EXISTS" in captured["command"][-1]
    assert "first search the exact target calendar" in captured["command"][-1]


def test_calendar_create_reports_preexisting_event_without_claiming_creation(
    monkeypatch, tmp_path
):
    _configure_home(monkeypatch, tmp_path)

    def fake_run(command, **kwargs):
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "mcp_tool_call",
                            "status": "completed",
                            "tool": "google_calendar.search_events",
                            "arguments": {
                                "time_min": "2026-08-03T00:00:00+03:00",
                                "time_max": "2026-08-04T00:00:00+03:00",
                            },
                            "result": {
                                "structured_content": {
                                    "events": [{"id": "manual-event"}]
                                }
                            },
                            "error": None,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "mcp_tool_call",
                            "status": "completed",
                            "tool": "google_calendar.read_event",
                            "arguments": {"event_id": "manual-event"},
                            "result": {
                                "structured_content": {"id": "manual-event"}
                            },
                            "error": None,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": (
                                "CALENDAR_ALREADY_EXISTS\n"
                                "Нашёл существующую запись на 3 августа."
                            ),
                        },
                    }
                ),
            ]
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(calendar.subprocess, "run", fake_run)
    tokens = _session(profile="family-chat")
    try:
        result = json.loads(
            calendar.google_calendar_tool(
                {
                    "task": "Проверь или добавь консультацию 3 августа",
                    "read_only": False,
                },
                user_task="Проверь календарь и добавь, только если записи ещё нет",
            )
        )
    finally:
        clear_session_vars(tokens)

    assert result["success"] is True
    assert result["outcome"] == "already_exists"
    assert result["changed"] is False
    assert "уже было" in result["answer"]
    assert "новое событие не создавалось" in result["answer"]


def test_calendar_create_rejects_success_claim_without_trace_evidence(
    monkeypatch, tmp_path
):
    _configure_home(monkeypatch, tmp_path)

    def fake_run(command, **kwargs):
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": (
                        "CALENDAR_CREATE_CONFIRMED\n"
                        "Событие якобы создано."
                    ),
                },
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(calendar.subprocess, "run", fake_run)
    tokens = _session(profile="family-chat")
    try:
        result = json.loads(
            calendar.google_calendar_tool(
                {
                    "task": "Добавь консультацию 3 августа",
                    "read_only": False,
                },
                user_task="Добавь консультацию 3 августа в календарь",
            )
        )
    finally:
        clear_session_vars(tokens)

    assert result["success"] is False
    assert "not proven" in result["error"]
    assert result["trace_evidence"]["created"] is False


def test_calendar_write_fails_closed_without_confirmation_marker(monkeypatch, tmp_path):
    _configure_home(monkeypatch, tmp_path)

    def fake_run(command, **kwargs):
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "Операция отменена до записи.",
                },
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(calendar.subprocess, "run", fake_run)
    tokens = _session(profile="family-chat")
    try:
        result = json.loads(
            calendar.google_calendar_tool(
                {
                    "task": "Создать встречу завтра в 10:00",
                    "read_only": False,
                },
                user_task="Добавь встречу завтра в календарь",
            )
        )
    finally:
        clear_session_vars(tokens)

    assert result["success"] is False
    assert "not proven" in result["error"]
    assert result["answer"] == "Операция отменена до записи."


def test_calendar_rejects_write_task_in_read_only_mode(monkeypatch, tmp_path):
    _configure_home(monkeypatch, tmp_path)
    tokens = _session()
    try:
        result = json.loads(calendar.google_calendar_tool({"task": "Создай встречу завтра"}))
    finally:
        clear_session_vars(tokens)

    assert "error" in result
    assert "read-only" in result["error"]


def test_calendar_accepts_assign_time_and_make_reminders_as_explicit_write(
    monkeypatch,
    tmp_path,
):
    _configure_home(monkeypatch, tmp_path)

    def fake_run(command, **kwargs):
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": (
                        "CALENDAR_WRITE_CONFIRMED\n"
                        "Время и три напоминания подтверждены."
                    ),
                },
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(calendar.subprocess, "run", fake_run)
    tokens = _session()
    user_task = (
        "Назначь время этому событию на 14 часов и сделай 3 напоминания "
        "за 2 часа, час и 15 минут"
    )
    try:
        result = json.loads(
            calendar.google_calendar_tool(
                {
                    "task": user_task,
                    "read_only": False,
                },
                user_task=user_task,
            )
        )
    finally:
        clear_session_vars(tokens)

    assert result["success"] is True
    assert result["read_only"] is False
    assert result["outcome"] == "changed"
    assert result["changed"] is True
