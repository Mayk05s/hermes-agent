from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import sys

import pytest

from run_agent import AIAgent


def _mock_response(*, usage: dict, content: str = "done"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(
        choices=[choice],
        model="test/model",
        usage=SimpleNamespace(**usage),
    )


def _make_agent(session_db, *, platform: str, gateway_session_key: str | None = None):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
            session_id=f"{platform}-session",
            platform=platform,
            gateway_session_key=gateway_session_key,
        )
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _mock_response(
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }
    )
    return agent


def test_run_conversation_persists_tokens_for_telegram_sessions():
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="telegram")

    result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    session_db.update_token_counts.assert_called_once()
    assert session_db.update_token_counts.call_args.args[0] == "telegram-session"


def test_run_conversation_persists_tokens_for_cron_sessions():
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="cron")

    result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    session_db.update_token_counts.assert_called_once()
    assert session_db.update_token_counts.call_args.args[0] == "cron-session"


def test_session_search_lazily_opens_db_when_entrypoint_did_not_pass_one(monkeypatch):
    sentinel_db = object()
    captured = {}

    class FakeSessionDB:
        def __new__(cls):
            return sentinel_db

    hermes_state = ModuleType("hermes_state")
    hermes_state.SessionDB = FakeSessionDB
    monkeypatch.setitem(sys.modules, "hermes_state", hermes_state)

    session_search_mod = ModuleType("tools.session_search_tool")

    def fake_session_search(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True, "results": []})

    session_search_mod.session_search = fake_session_search
    monkeypatch.setitem(sys.modules, "tools.session_search_tool", session_search_mod)

    agent = _make_agent(None, platform="acp")
    result = json.loads(agent._invoke_tool("session_search", {"query": "Hermes"}, "task-id"))

    assert result["success"] is True
    assert captured["db"] is sentinel_db
    assert captured["query"] == "Hermes"
    assert agent._session_db is sentinel_db


@pytest.mark.parametrize("platform", ["", "cli"])
def test_gateway_key_requires_scope_in_runtime_helper(monkeypatch, platform):
    captured = {}
    session_search_mod = ModuleType("tools.session_search_tool")

    def fake_session_search(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True, "results": []})

    session_search_mod.session_search = fake_session_search
    monkeypatch.setitem(sys.modules, "tools.session_search_tool", session_search_mod)

    agent = _make_agent(
        object(),
        platform=platform,
        gateway_session_key="agent:main:telegram:dm:alice",
    )
    result = json.loads(
        agent._invoke_tool("session_search", {"query": "private"}, "task-id")
    )

    assert result["success"] is True
    assert captured["current_access_scope"] == "agent:main:telegram:dm:alice"
    assert captured["require_access_scope"] is True


@pytest.mark.parametrize("platform", ["", "cli"])
def test_gateway_key_requires_scope_in_sequential_path(monkeypatch, platform):
    captured = {}
    session_search_mod = ModuleType("tools.session_search_tool")

    def fake_session_search(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True, "results": []})

    session_search_mod.session_search = fake_session_search
    monkeypatch.setitem(sys.modules, "tools.session_search_tool", session_search_mod)

    agent = _make_agent(
        object(),
        platform=platform,
        gateway_session_key="agent:main:telegram:dm:alice",
    )
    tool_call = SimpleNamespace(
        id="call-session-search",
        function=SimpleNamespace(
            name="session_search",
            arguments=json.dumps({"query": "private"}),
        ),
    )
    messages = []
    agent._execute_tool_calls_sequential(
        SimpleNamespace(content="", tool_calls=[tool_call]),
        messages,
        "task-id",
    )

    assert captured["current_access_scope"] == "agent:main:telegram:dm:alice"
    assert captured["require_access_scope"] is True
    assert len(messages) == 1


@pytest.mark.parametrize("platform", ["", "cli"])
def test_gateway_key_denies_cross_scope_in_both_paths(platform):
    agent = _make_agent(
        object(),
        platform=platform,
        gateway_session_key="agent:main:telegram:dm:alice",
    )
    args = {
        "question": "private canary",
        "target_scope": "agent:main:telegram:group:family",
    }

    direct = json.loads(agent._invoke_tool("query_chat_agent", args, "task-id"))
    assert direct["success"] is False
    assert "selected-fact broker" in direct["error"]
    assert "private canary" not in json.dumps(direct)

    tool_call = SimpleNamespace(
        id="call-cross-scope",
        function=SimpleNamespace(name="query_chat_agent", arguments=json.dumps(args)),
    )
    messages = []
    agent._execute_tool_calls_sequential(
        SimpleNamespace(content="", tool_calls=[tool_call]),
        messages,
        "task-id",
    )
    sequential = json.loads(messages[0]["content"])
    assert sequential["success"] is False
    assert "selected-fact broker" in sequential["error"]
    assert "private canary" not in json.dumps(sequential)
