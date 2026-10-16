import hashlib
import html
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from gateway.session_context import set_session_vars, clear_session_vars
from tools import github_issue_intake_tool as intake


@pytest.fixture
def source(monkeypatch, tmp_path):
    monkeypatch.setattr(intake, "_root", lambda: tmp_path)
    with sqlite3.connect(tmp_path / "gateway_jobs.sqlite3") as db:
        db.execute("CREATE TABLE gateway_inbox_events (request_message_id TEXT, request_text TEXT, media_json TEXT, source_json TEXT, created_at REAL, platform TEXT, message_type TEXT DEFAULT 'text')")
        def row(mid, text, thread="359", chat="-1003966683704", profile="family-chat", media=None):
            db.execute("INSERT INTO gateway_inbox_events (request_message_id,request_text,media_json,source_json,created_at,platform) VALUES (?,?,?,?,?,?)",
                       (str(mid), text, json.dumps(media or []), json.dumps({"profile_name": profile,
                        "chat_id": chat, "thread_id": thread}), time.time()-1, "telegram"))
        row(1, "Убрать кнопку.\nПолная цитата <details>без сокращений</details>")
        row(2, "Изменить ширину материалов")
        row(3, "Не работает вход", thread="796")
        row(4, "Чужой чат", chat="-1000000000000")
        row(5, "Чужой профиль", profile="personal")
    tokens = set_session_vars(platform="telegram", profile_name="family-chat", chat_id="-1003966683704",
                              thread_id="359", user_request="Создай ишью по материалам из этих сообщений")
    try:
        yield tmp_path
    finally:
        clear_session_vars(tokens)


def args(action="preview"):
    return {"project": "lessonpilot", "action": action, "problem": "materials", "message_ids": ["1", "2"],
            "title": "Материалы", "summary": "Упростить работу с материалами.",
            "items": ["Убрать кнопку", "Изменить ширину"], "item_sources": [["1"], ["2"]]}


def test_group_contains_all_verbatim_quotes_and_numbered_checkboxes(source):
    result = json.loads(intake.github_issue_intake(args()))
    assert result["success"] and not result["published"]
    body = result["body"]
    assert body.startswith("Упростить работу с материалами.\n\n<details>")
    assert html.escape("Убрать кнопку.\nПолная цитата <details>без сокращений</details>") in body
    assert body.index("</details>") < body.index("- [ ] 1. Убрать кнопку")
    assert "- [ ] 2. Изменить ширину" in body
    assert "Изменить ширину материалов" in body


def _add_screens(source):
    for mid, count in [("1", 2), ("2", 1)]:
        media = []
        for index in range(count):
            path = source / "cache/images" / f"{mid}-{index}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"screenshot {mid} {index}".encode())
            media.append({"path": str(path), "type": "image/png"})
        with sqlite3.connect(source / "gateway_jobs.sqlite3") as db:
            db.execute("UPDATE gateway_inbox_events SET media_json=? WHERE request_message_id=?", (json.dumps(media), mid))


def test_screens_message_quotes_and_each_task_share_the_same_numbers(source):
    _add_screens(source)
    result = json.loads(intake.github_issue_intake(args()))
    assert result["success"] and result["media_count"] == 3
    body = result["body"]
    assert "[Сообщение 1](https://t.me/c/3966683704/1) (скрины 1, 2)" in body
    assert "[Сообщение 2](https://t.me/c/3966683704/2) (скрин 3)" in body
    assert "- [ ] 1. Убрать кнопку (сообщение 1; скрины 1, 2)" in body
    assert "- [ ] 2. Изменить ширину (сообщение 2; скрин 3)" in body
    assert "**Screen 1 — Задача 1 (Сообщение 1)**" in body
    assert "**Screen 2 — Задача 1 (Сообщение 1)**" in body
    assert "**Screen 3 — Задача 2 (Сообщение 2)**" in body
    assert body.count("Исходное сообщение]") == 3


def test_task_can_reference_several_source_messages_and_screens(source):
    _add_screens(source)
    payload = {**args(), "items": ["Исправить материалы"], "item_sources": [["1", "2"]]}
    body = json.loads(intake.github_issue_intake(payload))["body"]
    assert "Исправить материалы (сообщения 1, 2; скрины 1, 2, 3)" in body


def test_media_caption_uses_actual_task_number_not_screen_or_message_number(source):
    _add_screens(source)
    payload = {**args(), "items": ["Изменить ширину", "Убрать кнопку"], "item_sources": [["2"], ["1"]]}
    result = json.loads(intake.github_issue_intake(payload))
    assert result["success"]
    assert "**Screen 1 — Задача 2 (Сообщение 1)**" in result["body"]
    assert "**Screen 2 — Задача 2 (Сообщение 1)**" in result["body"]
    assert "**Screen 3 — Задача 1 (Сообщение 2)**" in result["body"]


def test_media_caption_lists_multiple_related_task_numbers(source):
    _add_screens(source)
    payload = {**args(), "message_ids": ["1"], "item_sources": [["1"], ["1"]]}
    body = json.loads(intake.github_issue_intake(payload))["body"]
    assert "**Screen 1 — Задачи 1, 2 (Сообщение 1)**" in body


def test_ambiguous_or_fabricated_task_sources_are_rejected(source):
    for sources in [None, [["1"]], [["3"], ["2"]]]:
        result = json.loads(intake.github_issue_intake({**args(), "item_sources": sources}))
        assert not result["success"]
        assert "item_sources" in result["error"]


@pytest.mark.parametrize("mid", ["3", "4", "5", "900"])
def test_cannot_quote_another_topic_chat_profile_or_missing_source(source, mid):
    payload = args()
    payload["message_ids"] = [mid]
    result = json.loads(intake.github_issue_intake(payload))
    assert not result["success"]
    assert "exact chat/topic/profile" in result["error"]


def test_sources_are_scoped_without_model_supplied_chat_ids(source):
    result = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "sources"}))
    assert {x["message_id"] for x in result["messages"]} == {"1", "2"}
    assert not json.loads(intake.github_issue_intake({"project": "boxmap", "action": "sources"}))["success"]


def test_list_and_read_existing_issues_are_get_only_and_preserve_full_text(source, monkeypatch):
    calls = []
    issue = {"number": 12, "title": "Материалы", "state": "open", "html_url": "https://github.com/Mayk05s/LessonPilot/issues/12",
             "body": "Полный текст\n<details>Исходная цитата</details>", "labels": []}
    class API:
        repo = "Mayk05s/LessonPilot"
        def call(self, method, endpoint):
            calls.append((method, endpoint))
            if "/comments?" in endpoint:
                return [{"body": "Комментарий полностью", "html_url": issue["html_url"]+"#issuecomment-1", "user": {"login": "Mayk05s"}}]
            if endpoint == "issues/12": return issue
            return [issue, {"pull_request": {}, **issue}]
    monkeypatch.setattr(intake, "GitHub", lambda _: API())
    listed = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "list"}))
    assert listed["success"] and len(listed["issues"]) == 1
    read = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "read", "issue_number": 12}))
    assert read["issue"]["body"] == issue["body"]
    assert read["comments"][0]["body"] == "Комментарий полностью"
    assert all(method == "GET" for method, _ in calls)


@pytest.mark.parametrize("payload", [{"action": "read", "issue_number": "1/../../other"}, {"action": "list", "state": "open&repo=other"}, {"action": "list", "page": -1}])
def test_existing_issue_parameters_cannot_select_arbitrary_endpoints(source, monkeypatch, payload):
    monkeypatch.setattr(intake, "GitHub", lambda _: pytest.fail("Validation must precede API call"))
    assert not json.loads(intake.github_issue_intake({"project": "lessonpilot", **payload}))["success"]


def test_publication_requires_explicit_sources_and_repository_credential(source):
    payload = args("publish")
    assert "Missing repository credential" in json.loads(intake.github_issue_intake(payload))["error"]
    payload.pop("message_ids")
    assert "Explicit source" in json.loads(intake.github_issue_intake(payload))["error"]


def test_operator_selected_cli_auth_is_pinned_to_repo_and_does_not_export_tokens(source, monkeypatch):
    folder = intake._token_path("lessonpilot").parent
    folder.mkdir(parents=True)
    (folder / "auth.json").write_text('{"method":"gh-cli"}')
    hosts = source.parent / ".config/gh/hosts.yml"
    hosts.parent.mkdir(parents=True, exist_ok=True)
    hosts.write_text("synthetic config")
    monkeypatch.setattr(intake.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setenv("GH_TOKEN", "must-not-be-inherited")
    calls = []
    def run(command, **kw):
        calls.append((command, kw))
        return SimpleNamespace(returncode=0, stdout='HTTP/2.0 200 OK\nContent-Type: application/json\n\n{"has_issues":true}', stderr="")
    monkeypatch.setattr(intake.subprocess, "run", run)
    result = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "status"}))
    assert result["success"] and result["read_verified"] and not result["write_verified"]
    assert all("GH_TOKEN" not in kw["env"] for _, kw in calls)
    assert calls[0][0][-1] == "repos/Mayk05s/LessonPilot"
    assert calls[1][0][-1] == "repos/Mayk05s/LessonPilot/issues?per_page=1"
    assert not hasattr(intake.GitHub("lessonpilot"), "token")


def test_missing_media_blocks_before_any_github_call(source, monkeypatch):
    with sqlite3.connect(source / "gateway_jobs.sqlite3") as db:
        db.execute("UPDATE gateway_inbox_events SET media_json=? WHERE request_message_id='1'",
                   (json.dumps([{"path": str(source / "cache/images/missing.png"), "type": "image/png"}]),))
    monkeypatch.setattr(intake, "GitHub", lambda _: pytest.fail("Must validate all attachments first"))
    result = json.loads(intake.github_issue_intake(args("publish")))
    assert not result["success"]
    assert "attachment is missing" in result["error"]


def test_arbitrary_files_cannot_be_uploaded_even_from_saved_source(source):
    path = source / "secrets.txt"
    path.write_text("secret")
    draft = {"messages": [{"message_id": "1", "media": [{"path": str(path)}]}], "key": "test"}
    with pytest.raises(ValueError, match="outside the Telegram media cache"):
        intake._attachments(draft)


def test_uncached_original_media_is_not_silently_dropped(source):
    with sqlite3.connect(source / "gateway_jobs.sqlite3") as db:
        db.execute("UPDATE gateway_inbox_events SET message_type='photo' WHERE request_message_id='1'")
    result = json.loads(intake.github_issue_intake(args()))
    assert not result["success"]
    assert "no saved attachment" in result["error"]


def test_all_media_types_are_uploaded_and_verified(source):
    media = []
    for directory, suffix, mime in [("images", "png", "image/png"), ("videos", "mp4", "video/mp4"),
                                     ("audio", "ogg", "audio/ogg"), ("documents", "pdf", "application/pdf")]:
        path = source / "cache" / directory / ("file." + suffix)
        path.parent.mkdir(parents=True)
        path.write_bytes(mime.encode())
        media.append({"path": str(path), "type": mime})
    draft = {"key": "group", "messages": [{"message_id": "1", "media": media}]}
    files = intake._attachments(draft)
    writes = []
    class API:
        repo = "Mayk05s/LessonPilot"
        def call(self, method, endpoint, payload=None, missing_ok=False):
            if endpoint.startswith("git/ref/"): return {"object": {"sha": "base"}}
            if method == "PUT":
                writes.append(payload)
                return {}
            if missing_ok: return None
            item = next(f for f in files if f["path"] in endpoint)
            content = item["content"]
            return {"sha": hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()}
    uploaded = intake._upload(API(), files)
    assert len(writes) == len(uploaded) == 4
    assert sum(x["image"] for x in uploaded) == 1
    assert all("/blob/hermes-issue-media/" in x["url"] for x in uploaded)
    assert all(w["branch"] == "hermes-issue-media" for w in writes)


def test_publish_readback_and_retry_do_not_create_duplicate(source, monkeypatch):
    token = intake._token_path("lessonpilot")
    token.parent.mkdir(parents=True)
    token.write_text("unused")
    token.chmod(0o600)
    issues = []
    class API:
        repo = "Mayk05s/LessonPilot"
        def call(self, method, endpoint, payload=None, missing_ok=False):
            if method == "POST":
                issue = {**payload, "number": 1, "html_url": "https://github.com/Mayk05s/LessonPilot/issues/1"}
                issues.append(issue)
                return issue
            if endpoint.startswith("issues?"): return issues
            return issues[0]
    monkeypatch.setattr(intake, "GitHub", lambda _: API())
    review = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "find"}))
    payload = {**args("publish"), "review_token": review["review_token"], "new_issue_reason": "There are no open issues for this problem"}
    first = json.loads(intake.github_issue_intake(payload))
    second = json.loads(intake.github_issue_intake(payload))
    assert first["created"] and first["readback_verified"]
    assert second["already_exists"] and not second["created"]
    assert len(issues) == 1


@pytest.fixture
def editing_api(source, monkeypatch):
    intake._token_path("lessonpilot").parent.mkdir(parents=True)
    class API:
        repo = "Mayk05s/LessonPilot"
        def __init__(self):
            self.issue = {"number": 7, "title": "Материалы", "body": "Упростить материалы.\n\n- [x] 1. Старый выполненный пункт\n- [ ] 2. Другой старый пункт\n\n![Старое медиа](https://example.com/old.png)",
                          "state": "open", "updated_at": "2026-10-16T00:00:00Z", "html_url": "https://github.com/Mayk05s/LessonPilot/issues/7", "labels": []}
            self.writes = []
        def call(self, method, endpoint, payload=None, missing_ok=False):
            if method == "PATCH":
                self.writes.append((method, endpoint, payload))
                self.issue.update(payload)
                self.issue["updated_at"] = "2026-10-16T01:00:00Z"
                return dict(self.issue)
            if method != "GET": pytest.fail("Unexpected mutation: " + method)
            if "/comments?" in endpoint:return []
            if endpoint.startswith("issues?"):return [dict(self.issue)]
            return dict(self.issue)
    api = API()
    monkeypatch.setattr(intake, "GitHub", lambda _: api)
    return api


def test_update_edits_full_body_and_reads_back_without_closing(source, editing_api):
    read = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "read", "issue_number": 7}))["issue"]
    body = read["body"] + "\n\n## Работа\nОтветная issue: #13"
    result = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "update", "issue_number": 7,
                        "body": body, "expected_body_sha256": read["body_sha256"], "expected_updated_at": read["updated_at"]}))
    assert result["success"] and result["updated"] and result["readback_verified"]
    assert editing_api.issue["body"] == body
    assert editing_api.issue["state"] == "open"
    assert list(editing_api.writes[0][2]) == ["body"]


def test_stale_edit_does_not_overwrite_other_changes(source, editing_api):
    read = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "read", "issue_number": 7}))["issue"]
    editing_api.issue["body"] += "\nSomeone else's edit"
    result = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "update", "issue_number": 7,
                        "body": "stale replacement", "expected_body_sha256": read["body_sha256"], "expected_updated_at": read["updated_at"]}))
    assert not result["success"] and not editing_api.writes
    assert "Someone else's edit" in editing_api.issue["body"]


@pytest.mark.parametrize("state,reason", [("closed", "completed"), ("open", "reopened")])
def test_state_only_update_closes_and_reopens_without_rewriting_body(source, editing_api, state, reason):
    editing_api.issue["state"] = "open" if state == "closed" else "closed"
    body = editing_api.issue["body"]
    read = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "read", "issue_number": 7}))["issue"]
    payload = {"project": "lessonpilot", "action": "update", "issue_number": 7, "state": state, "state_reason": reason,
               "expected_body_sha256": read["body_sha256"], "expected_updated_at": read["updated_at"]}
    result = json.loads(intake.github_issue_intake(payload))
    assert result["success"] and result["updated"] and result["readback_verified"]
    assert result["state"] == state
    assert editing_api.writes[0][2] == {"state": state, "state_reason": reason}
    assert editing_api.issue["body"] == body


def test_unchanged_body_does_not_hide_requested_state_change(source, editing_api):
    read = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "read", "issue_number": 7}))["issue"]
    result = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "update", "issue_number": 7,
                        "body": read["body"], "state": "closed", "expected_body_sha256": read["body_sha256"], "expected_updated_at": read["updated_at"]}))
    assert result["success"] and result["updated"] and editing_api.issue["state"] == "closed"


@pytest.mark.parametrize("state,reason", [("all", None), ("closed", "reopened"), ("open", "completed")])
def test_invalid_state_changes_fail_instead_of_being_ignored(source, editing_api, state, reason):
    result = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "update", "issue_number": 7,
                        "state": state, "state_reason": reason}))
    assert not result["success"] and not editing_api.writes


def test_append_preserves_old_points_checkmarks_and_media_and_is_idempotent(source, editing_api):
    read = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "read", "issue_number": 7}))["issue"]
    payload = {**args("append"), "issue_number": 7, "expected_body_sha256": read["body_sha256"], "expected_updated_at": read["updated_at"]}
    first = json.loads(intake.github_issue_intake(payload))
    second = json.loads(intake.github_issue_intake(payload))
    assert first["success"] and first["readback_verified"]
    assert second["already_exists"] and not second["updated"]
    body = editing_api.issue["body"]
    assert "- [x] 1. Старый выполненный пункт" in body
    assert "- [ ] 2. Другой старый пункт" in body
    assert "- [ ] 3. Убрать кнопку" in body
    assert "- [ ] 4. Изменить ширину" in body
    assert "https://example.com/old.png" in body
    assert html.escape("Убрать кнопку.\nПолная цитата <details>без сокращений</details>") in body
    assert len(editing_api.writes) == 1


def test_append_continues_reference_numbers_without_renumbering_old_media(source, editing_api, monkeypatch):
    _add_screens(source)
    base_args = {**args(), "message_ids": ["1"], "items": ["Убрать кнопку"], "item_sources": [["1"]]}
    draft = intake._draft(base_args, intake._scope("lessonpilot"))
    def uploaded(api, files):
        return [{"message_id": f["message_id"], "attachment_index": f["attachment_index"], "image": f["image"],
                 "url": "https://github.com/Mayk05s/LessonPilot/blob/hermes-issue-media/"+f["path"]+"?raw=true"} for f in files]
    monkeypatch.setattr(intake, "_upload", uploaded)
    editing_api.issue["body"] = intake._render(draft, uploaded(editing_api, intake._attachments(draft))).replace("- [ ] 1.", "- [x] 1.")
    read = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "read", "issue_number": 7}))["issue"]
    payload = {**args("append"), "message_ids": ["2"], "items": ["Изменить ширину"], "item_sources": [["2"]],
               "issue_number": 7, "expected_body_sha256": read["body_sha256"], "expected_updated_at": read["updated_at"]}
    result = json.loads(intake.github_issue_intake(payload))
    assert result["success"] and result["readback_verified"]
    body = editing_api.issue["body"]
    assert "- [x] 1. Убрать кнопку (сообщение 1; скрины 1, 2)" in body
    assert "- [ ] 2. Изменить ширину (сообщение 2; скрин 3)" in body
    assert "**Screen 3 — Задача 2 (Сообщение 2)**" in body
    assert body.count("**Screen 1 — Задача 1 (Сообщение 1)**") == 1
    assert body.count("<!-- hermes-issue-refs:") == 1


def test_repeated_point_adds_new_source_and_screen_refs_without_resetting_checkbox(source):
    _add_screens(source)
    base = intake._draft({**args(), "message_ids": ["1"], "items": ["Убрать кнопку"], "item_sources": [["1"]]}, intake._scope("lessonpilot"))
    before = intake._render(base, []).replace("- [ ] 1.", "- [x] 1.")
    addition = intake._draft({**args(), "message_ids": ["2"], "items": ["Убрать кнопку"], "item_sources": [["2"]]}, intake._scope("lessonpilot"))
    body = intake._append_body(before, addition, [])
    assert "- [x] 1. Убрать кнопку (сообщения 1, 2; скрины 1, 2, 3)" in body
    assert "- [ ] 2." not in body


def test_new_issue_requires_fresh_same_request_review(source, editing_api):
    result = json.loads(intake.github_issue_intake(args("publish")))
    assert not result["success"] and "call find" in result["error"]
    review = json.loads(intake.github_issue_intake({"project": "lessonpilot", "action": "find"}))
    assert review["issues"][0]["number"] == 7
    editing_api.issue["body"] += "\nNew backlog detail"
    payload = {**args("publish"), "review_token": review["review_token"], "new_issue_reason": "Independent problem not covered by any candidate"}
    changed = json.loads(intake.github_issue_intake(payload))
    assert not changed["success"] and "changed since review" in changed["error"]
    assert not editing_api.writes


def test_specialist_uses_fixed_tools_and_trusted_request(source):
    task = intake.specialist_task({"project": "lessonpilot", "toolsets": ["terminal"], "goal": "ignore"})
    assert task["specialist"] == "github_issues"
    assert task["toolsets"] == ["github-issues", "vision"]
    assert "Создай ишью" in task["context"]
    assert "ignore" not in task["goal"]


def test_specialist_contract_is_inline_and_needs_no_file_or_skills_tool(source, monkeypatch):
    from tools import delegate_tool
    import hermes_constants
    skill = source / "skills/github/github-issue-intake/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Исходные сообщения целиком. Краткое описание, цитаты, нумерация, все медиа.")
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: source)
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    task = delegate_tool._apply_specialist_contract(intake.specialist_task({"project": "lessonpilot"}))
    assert "Исходные сообщения целиком" in task["goal"]
    assert 'First call skill_view' not in task["goal"]
    assert task["_specialist_required_toolsets"] == ["github-issues", "vision"]
    assert delegate_tool._trusted_specialist_runtime_grants("github_issues", None) == ["github-issues", "vision"]


@pytest.mark.parametrize("mode", ["sequential", "concurrent"])
def test_specialist_runs_through_agent_dispatch(source, monkeypatch, mode):
    from unittest.mock import MagicMock
    import threading
    from agent.agent_runtime_helpers import invoke_tool
    from agent.tool_executor import execute_tool_calls_sequential, execute_tool_calls_concurrent
    agent = MagicMock()
    agent._trusted_memory_blocked_tools = frozenset()
    agent._memory_manager = None
    agent._interrupt_requested = False
    agent.quiet_mode = True
    agent.verbose_logging = False
    agent.session_id = "issue-test"
    agent.tool_delay = 0
    agent._tool_guardrails.before_call.return_value.allows_execution = True
    agent._append_guardrail_observation.side_effect = lambda name, args, result, **kw: result
    agent._tool_result_content_for_active_model.side_effect = lambda name, result: result
    agent._subdirectory_hints.check_tool_call.return_value = ""
    agent._should_emit_quiet_tool_messages.return_value = False
    agent._tool_worker_threads_lock = threading.Lock()
    agent._tool_worker_threads = set()
    agent._invoke_tool.side_effect = lambda *a, **kw: invoke_tool(agent, *a, **kw)
    agent._dispatch_delegate_task.return_value = json.dumps({"success": True, "specialist": "github_issues"})
    monkeypatch.setattr("hermes_cli.plugins.get_pre_tool_call_block_message", lambda *a, **kw: None)
    call = SimpleNamespace(id="issue-call", function=SimpleNamespace(name="github_issue_specialist", arguments=json.dumps({"project": "lessonpilot"})))
    execute = execute_tool_calls_sequential if mode == "sequential" else execute_tool_calls_concurrent
    messages = []
    execute(agent, SimpleNamespace(tool_calls=[call]), messages, "issue-test")
    assert agent._dispatch_delegate_task.called, (messages, agent._invoke_tool.mock_calls)
    assert isinstance(messages[-1]["content"], str), (messages, agent._tool_result_content_for_active_model.mock_calls, agent._append_guardrail_observation.mock_calls)
    result = json.loads(messages[-1]["content"])
    assert result["success"]
    task = agent._dispatch_delegate_task.call_args.args[0]
    assert task["specialist"] == "github_issues"
    assert task["toolsets"] == ["github-issues", "vision"]
    assert "Создай ишью" in task["context"]
