import json

from tools.cross_scope_query_tool import query_chat_agent
from tools.registry import registry


def test_query_chat_agent_is_disabled_and_exposes_no_history():
    result = json.loads(query_chat_agent(
        question="What did you decide about the deploy?",
        target_scope="agent:main:telegram:dm:bob",
        source_scope="agent:main:telegram:dm:alice",
    ))

    assert result["success"] is False
    assert "selected-fact broker" in result["error"]
    assert "messages" not in result
    assert "history" not in result
    assert "memory" not in result


def test_query_chat_agent_with_grant_cannot_invoke_runner():
    seen = {}

    def runner(*, source_scope, target_scope, question):
        seen.update(source_scope=source_scope, target_scope=target_scope, question=question)
        return "Final answer from target scope, not raw transcript."

    result = json.loads(query_chat_agent(
        question="Summarize the deployment decision",
        target_scope="agent:main:telegram:dm:bob",
        source_scope="agent:main:telegram:dm:alice",
        mediated_runner=runner,
        grant_checker=lambda source_scope, target_scope: True,
    ))

    assert result["success"] is False
    assert "selected-fact broker" in result["error"]
    assert seen == {}


def test_registry_query_chat_agent_uses_trusted_scope_not_model_args(monkeypatch):
    del monkeypatch

    result = json.loads(registry.dispatch(
        "query_chat_agent",
        {
            "question": "What is the decision?",
            "target_scope": "agent:main:telegram:dm:bob",
            "source_scope": "agent:main:telegram:dm:mallory",
        },
        current_access_scope="agent:main:telegram:dm:alice",
    ))

    assert result["success"] is False
    assert "selected-fact broker" in result["error"]
    assert "source_scope" not in result
    assert "target_scope" not in result
