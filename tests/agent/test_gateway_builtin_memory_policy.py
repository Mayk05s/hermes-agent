"""Gateway guards for legacy profile-global MEMORY.md and USER.md."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from run_agent import AIAgent
from agent.conversation_loop import _restore_or_build_system_prompt
from tools.memory_tool import MemoryStore


PRIVATE_MEMORY_CANARY = "PRIVATE_MEMORY_CANARY_MIKHAIL"
PRIVATE_USER_CANARY = "PRIVATE_USER_CANARY_MIKHAIL"


def _tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _access_policy(*, nonempty: bool) -> dict:
    policy = {
        "version": 1,
        "default_private_zone": "personal",
        "default_group_zone": "general",
        "groups": [],
        "principals": [],
    }
    if nonempty:
        policy["principals"] = [
            {
                "id": "p_k3m7w2r5t6y4q2zx",
                "identities": [{"platform": "telegram", "user_id": "1001"}],
                "dm_destinations": [{"platform": "telegram", "chat_id": "1001"}],
                "private_zones": ["personal"],
                "group_memberships": [],
                "dm_rules": [],
            }
        ]
    return policy


def _config(access: object) -> dict:
    return {
        "memory": {
            "memory_enabled": True,
            "user_profile_enabled": True,
            "access": access,
        }
    }


def _make_agent(
    config: dict,
    *,
    platform: str,
    gateway_session_key: str | None,
    memory_access_context=None,
    parent_session_id: str | None = None,
) -> AIAgent:
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_tool_defs("memory", "query_chat_agent"),
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value=config),
        patch("run_agent.OpenAI"),
    ):
        return AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            platform=platform,
            gateway_session_key=gateway_session_key,
            memory_access_context=memory_access_context,
            parent_session_id=parent_session_id,
        )


@pytest.fixture()
def legacy_memory_files(tmp_path, monkeypatch):
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    memory_path = memory_dir / "MEMORY.md"
    user_path = memory_dir / "USER.md"
    memory_path.write_text(PRIVATE_MEMORY_CANARY, encoding="utf-8")
    user_path.write_text(PRIVATE_USER_CANARY, encoding="utf-8")

    path_lookups = []

    def memory_dir_spy():
        path_lookups.append(memory_dir)
        return memory_dir

    monkeypatch.setattr("tools.memory_tool.get_memory_dir", memory_dir_spy)
    return memory_path, user_path, path_lookups


@pytest.mark.parametrize(
    ("label", "memory_context", "gateway_session_key"),
    [
        (
            "verified-private-dm",
            SimpleNamespace(destination_kind="dm"),
            "agent:main:telegram:dm:1001",
        ),
        (
            "verified-group",
            SimpleNamespace(destination_kind="group"),
            "agent:main:telegram:group:family",
        ),
        (
            "unverified-or-ambiguous",
            None,
            "agent:main:telegram:dm:unknown",
        ),
    ],
)
def test_nonempty_policy_never_loads_or_exposes_legacy_files(
    label,
    memory_context,
    gateway_session_key,
    legacy_memory_files,
):
    del label
    memory_path, user_path, path_lookups = legacy_memory_files
    agent = _make_agent(
        _config(_access_policy(nonempty=True)),
        platform="telegram",
        gateway_session_key=gateway_session_key,
        memory_access_context=memory_context,
    )

    assert path_lookups == []
    assert agent._memory_store is None
    assert "memory" not in agent.valid_tool_names
    assert "query_chat_agent" not in agent.valid_tool_names
    assert PRIVATE_MEMORY_CANARY not in agent._build_system_prompt()
    assert PRIVATE_USER_CANARY not in agent._build_system_prompt()

    result = json.loads(agent._invoke_tool("memory", {"action": "read"}, "task"))
    assert result == {
        "success": False,
        "error": "Built-in memory is unavailable in this context.",
    }
    assert PRIVATE_MEMORY_CANARY not in json.dumps(result)
    assert PRIVATE_USER_CANARY not in json.dumps(result)
    assert memory_path.read_text(encoding="utf-8") == PRIVATE_MEMORY_CANARY
    assert user_path.read_text(encoding="utf-8") == PRIVATE_USER_CANARY


def test_invalid_gateway_policy_fails_closed_without_touching_legacy_files(
    legacy_memory_files,
):
    _memory_path, _user_path, path_lookups = legacy_memory_files
    agent = _make_agent(
        _config({"version": 1}),
        platform="telegram",
        gateway_session_key="agent:main:telegram:dm:1001",
    )

    assert path_lookups == []
    assert agent._memory_store is None
    assert "memory" not in agent.valid_tool_names


def test_empty_policy_preserves_gateway_legacy_compatibility(legacy_memory_files):
    _memory_path, _user_path, path_lookups = legacy_memory_files
    agent = _make_agent(
        _config(_access_policy(nonempty=False)),
        platform="telegram",
        gateway_session_key="agent:main:telegram:dm:1001",
    )

    assert path_lookups
    assert isinstance(agent._memory_store, MemoryStore)
    assert "memory" in agent.valid_tool_names
    prompt = agent._build_system_prompt()
    assert PRIVATE_MEMORY_CANARY in prompt
    assert PRIVATE_USER_CANARY in prompt


def test_explicit_local_cli_preserves_legacy_memory_with_active_policy(
    legacy_memory_files,
):
    _memory_path, _user_path, path_lookups = legacy_memory_files
    agent = _make_agent(
        _config(_access_policy(nonempty=True)),
        platform="cli",
        gateway_session_key=None,
    )

    assert path_lookups
    assert "memory" in agent.valid_tool_names
    assert PRIVATE_MEMORY_CANARY in agent._build_system_prompt()


def test_cli_label_with_gateway_key_is_not_treated_as_local_cli(
    legacy_memory_files,
):
    _memory_path, _user_path, path_lookups = legacy_memory_files
    agent = _make_agent(
        _config(_access_policy(nonempty=True)),
        platform="cli",
        gateway_session_key="agent:main:telegram:dm:1001",
    )

    assert path_lookups == []
    assert agent._memory_store is None
    assert "memory" not in agent.valid_tool_names
    assert "query_chat_agent" not in agent.valid_tool_names


def test_scoped_policy_does_not_restore_persisted_prompt_with_private_canary(
    legacy_memory_files,
):
    _memory_path, _user_path, _path_lookups = legacy_memory_files
    agent = _make_agent(
        _config(_access_policy(nonempty=True)),
        platform="telegram",
        gateway_session_key="agent:main:telegram:group:family",
    )
    stored_reads = []

    class FakeSessionDB:
        def get_session(self, _session_id):
            stored_reads.append(True)
            return {
                "system_prompt": (
                    f"{PRIVATE_MEMORY_CANARY} {PRIVATE_USER_CANARY}"
                )
            }

        def update_system_prompt(self, _session_id, _prompt):
            return None

    agent._session_db = FakeSessionDB()
    agent._cached_system_prompt = None
    _restore_or_build_system_prompt(
        agent,
        system_message=None,
        conversation_history=[{"role": "user", "content": "prior"}],
    )

    assert stored_reads == []
    assert PRIVATE_MEMORY_CANARY not in agent._cached_system_prompt
    assert PRIVATE_USER_CANARY not in agent._cached_system_prompt


@pytest.mark.parametrize(
    ("platform", "parent_session_id"),
    [("cron", None), ("telegram", "parent-session")],
)
def test_cron_and_subagent_never_load_or_advertise_legacy_memory(
    platform,
    parent_session_id,
    legacy_memory_files,
):
    _memory_path, _user_path, path_lookups = legacy_memory_files
    agent = _make_agent(
        _config(_access_policy(nonempty=False)),
        platform=platform,
        gateway_session_key=(
            "agent:main:telegram:dm:1001" if platform == "telegram" else None
        ),
        parent_session_id=parent_session_id,
    )

    assert path_lookups == []
    assert agent._memory_store is None
    assert "memory" not in agent.valid_tool_names
