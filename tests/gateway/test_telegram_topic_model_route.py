"""Telegram topic-level model routing must not leak to sibling topics."""

from gateway.config import Platform
from gateway.session import SessionSource


def _source(thread_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1003735932411",
        chat_type="group",
        thread_id=thread_id,
    )


def _runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner._last_resolved_model = {}
    return runner


def _config():
    return {
        "model": {"default": "gpt-5.6-sol", "provider": "openai-codex"},
        "telegram": {
            "extra": {
                "group_topics": [
                    {
                        "chat_id": -1003735932411,
                        "topics": [
                            {"name": "system", "thread_id": 518, "model": "astra"},
                            {"name": "planning", "thread_id": 313},
                        ],
                    }
                ]
            }
        },
    }


def _patch_runtime(monkeypatch):
    import gateway.run as run

    monkeypatch.setattr(run, "_resolve_gateway_model", lambda cfg=None: "gpt-5.6-sol")
    monkeypatch.setattr(
        run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"provider": "openai-codex", "api_mode": "codex_responses"},
    )


def test_matching_topic_uses_its_model(monkeypatch):
    _patch_runtime(monkeypatch)
    model, runtime = _runner()._resolve_session_agent_runtime(
        source=_source("518"), session_key="system-session", user_config=_config()
    )
    assert model == "astra"
    assert runtime["provider"] == "openai-codex"


def test_sibling_topic_keeps_profile_default(monkeypatch):
    _patch_runtime(monkeypatch)
    model, _ = _runner()._resolve_session_agent_runtime(
        source=_source("313"), session_key="planning-session", user_config=_config()
    )
    assert model == "gpt-5.6-sol"


def test_live_session_override_wins_over_topic_default(monkeypatch):
    _patch_runtime(monkeypatch)
    runner = _runner()
    runner._session_model_overrides["system-session"] = {
        "model": "gpt-5.6-sol",
        "provider": "openai-codex",
        "api_mode": "codex_responses",
    }
    model, _ = runner._resolve_session_agent_runtime(
        source=_source("518"), session_key="system-session", user_config=_config()
    )
    assert model == "gpt-5.6-sol"