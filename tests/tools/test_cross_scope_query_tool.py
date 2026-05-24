import json

from tools.cross_scope_query_tool import query_chat_agent
from tools.registry import registry


def test_query_chat_agent_requires_grant_and_exposes_no_history():
    result = json.loads(query_chat_agent(
        question="What did you decide about the deploy?",
        target_scope="agent:main:telegram:dm:bob",
        source_scope="agent:main:telegram:dm:alice",
    ))

    assert result["success"] is False
    assert result["requires_approval"] is True
    assert result["grant_key"] == "scope_query:agent:main:telegram:dm:alice->agent:main:telegram:dm:bob"
    assert "messages" not in result
    assert "history" not in result
    assert "memory" not in result


def test_query_chat_agent_with_grant_invokes_mediated_runner_only():
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

    assert result == {
        "success": True,
        "source_scope": "agent:main:telegram:dm:alice",
        "target_scope": "agent:main:telegram:dm:bob",
        "answer": "Final answer from target scope, not raw transcript.",
    }
    assert seen == {
        "source_scope": "agent:main:telegram:dm:alice",
        "target_scope": "agent:main:telegram:dm:bob",
        "question": "Summarize the deployment decision",
    }


def test_registry_query_chat_agent_uses_trusted_scope_not_model_args(monkeypatch):
    seen = {}

    def fake_grant(source_scope, target_scope):
        seen["grant"] = (source_scope, target_scope)
        return type("Decision", (), {"allowed": True, "grant_key": "k", "reason": "ok"})()

    def fake_runner(*, source_scope, target_scope, question):
        seen["runner"] = (source_scope, target_scope, question)
        return "trusted answer"

    monkeypatch.setattr("tools.cross_scope_query_tool.check_scope_query_grant", fake_grant)
    monkeypatch.setattr("tools.cross_scope_query_tool._default_mediated_runner", fake_runner)

    result = json.loads(registry.dispatch(
        "query_chat_agent",
        {
            "question": "What is the decision?",
            "target_scope": "agent:main:telegram:dm:bob",
            "source_scope": "agent:main:telegram:dm:mallory",
        },
        current_access_scope="agent:main:telegram:dm:alice",
    ))

    assert result["success"] is True
    assert result["source_scope"] == "agent:main:telegram:dm:alice"
    assert seen["grant"] == ("agent:main:telegram:dm:alice", "agent:main:telegram:dm:bob")
    assert seen["runner"] == (
        "agent:main:telegram:dm:alice",
        "agent:main:telegram:dm:bob",
        "What is the decision?",
    )
