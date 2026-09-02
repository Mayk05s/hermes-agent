import base64
from dataclasses import replace
import logging
from types import SimpleNamespace

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from agent.agent_init import _memory_provider_allowed_for_context
from gateway.config import Platform
from gateway.memory_access import parse_memory_access_policy, resolve_memory_access_context
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from run_agent import AIAgent


OPAQUE_BYTES = b"h1-test-opaque-reference-key-32b"
OPAQUE_ENV = base64.urlsafe_b64encode(OPAQUE_BYTES).rstrip(b"=").decode("ascii")


def access_config():
    return {
        "version": 1,
        "default_private_zone": "personal",
        "default_group_zone": "general",
        "groups": [
            {
                "id": "family",
                "platform": "telegram",
                "chat_id": "-100-family",
                "zone": "general",
            }
        ],
        "principals": [
            {
                "id": "p_k3m7w2r5t6y4q2zx",
                "identities": [{"platform": "telegram", "user_id": "1001"}],
                "dm_destinations": [{"platform": "telegram", "chat_id": "1001"}],
                "private_zones": ["personal", "planning"],
                "group_memberships": ["family"],
                "dm_rules": [
                    {
                        "scope": "personal",
                        "private_zones": ["personal", "planning"],
                        "group_memberships": ["family"],
                    }
                ],
            },
            {
                "id": "p_n4v2c7b5x6m3q2rs",
                "identities": [{"platform": "telegram", "user_id": "2002"}],
                "dm_destinations": [{"platform": "telegram", "chat_id": "2002"}],
                "private_zones": ["personal"],
                "group_memberships": ["family"],
                "dm_rules": [],
            },
        ],
    }


def source(
    *,
    chat_id="1001",
    chat_type="dm",
    user_id="1001",
    message_id="55",
    profile_name="Personal",
    memory_scope="personal",
):
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        message_id=message_id,
        profile_name=profile_name,
        scope_name=memory_scope,
        memory_scope=memory_scope,
    )


def context(src):
    return resolve_memory_access_context(
        src,
        parse_memory_access_policy(access_config()),
        request_authorized=True,
        principal_verified=src.user_id is not None,
        opaque_id_key=OPAQUE_BYTES,
    )


def signature(memory_context):
    return GatewayRunner._agent_config_signature(
        "model",
        {"provider": "test", "api_key": "key"},
        ["test"],
        "",
        memory_access_fingerprint=(
            memory_context.cache_fingerprint if memory_context is not None else ""
        ),
    )


class RecordingProvider(MemoryProvider):
    def __init__(self):
        self.contexts = []

    @property
    def name(self):
        return "recording"

    def is_available(self):
        return True

    def initialize(self, session_id: str, **kwargs):
        return None

    def get_tool_schemas(self):
        return []

    def set_access_context(self, value):
        self.contexts.append(value)
        super().set_access_context(value)


class EnforcingProvider(RecordingProvider):
    @property
    def enforces_trusted_access_context(self):
        return True


class RejectingContextProvider(EnforcingProvider):
    def set_access_context(self, value):
        if value is not None:
            raise RuntimeError("sensitive-token-value")
        super().set_access_context(value)


def agent_with_recording_provider():
    provider = RecordingProvider()
    manager = MemoryManager()
    manager.add_provider(provider)
    agent = AIAgent.__new__(AIAgent)
    agent._memory_manager = manager
    return agent, provider


def test_sequential_same_cache_messages_replace_source_provenance():
    first = context(source(message_id="55"))
    second = context(source(message_id="56"))

    assert first is not None and second is not None
    assert first.source_ref != second.source_ref
    assert first.cache_fingerprint == second.cache_fingerprint
    assert signature(first) == signature(second)

    agent, provider = agent_with_recording_provider()
    GatewayRunner._init_cached_agent_for_turn(agent, 0, first)
    GatewayRunner._init_cached_agent_for_turn(agent, 0, second)

    assert agent._memory_access_context is second
    assert provider.contexts[-2:] == [first, second]
    assert provider._memory_access_context.source_ref == second.source_ref


def test_different_group_participants_share_wing_but_bust_agent_cache():
    first = context(
        source(
            chat_id="-100-family",
            chat_type="group",
            user_id="1001",
            memory_scope="general",
        )
    )
    second = context(
        source(
            chat_id="-100-family",
            chat_type="group",
            user_id="2002",
            memory_scope="general",
        )
    )

    assert first is not None and second is not None
    assert first.read_wings == second.read_wings
    assert first.write_wing == second.write_wing
    assert first.actor_ref != second.actor_ref
    assert first.cache_fingerprint != second.cache_fingerprint
    assert signature(first) != signature(second)


def test_shared_group_pending_messages_from_different_actors_are_not_merged():
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = SimpleNamespace(_pending_messages={})
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._queued_events = {}
    shared = source(
        chat_id="-100-family",
        chat_type="group",
        user_id=None,
        memory_scope="general",
    )
    first = MessageEvent(
        text="first actor",
        source=shared,
        message_id="71",
        raw_message=SimpleNamespace(from_user=SimpleNamespace(id="1001")),
    )
    second = MessageEvent(
        text="second actor",
        source=shared,
        message_id="72",
        raw_message=SimpleNamespace(from_user=SimpleNamespace(id="2002")),
    )
    runner._stamp_event_memory_authorization(first, True)
    runner._stamp_event_memory_authorization(second, True)

    runner._queue_or_replace_pending_event("shared", first, merge_text=True)
    runner._queue_or_replace_pending_event("shared", second, merge_text=True)

    assert adapter._pending_messages["shared"] is first
    assert first.text == "first actor"
    assert runner._queued_events["shared"] == [second]


def test_pending_authorized_and_unstamped_events_are_not_merged():
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = SimpleNamespace(_pending_messages={})
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._queued_events = {}
    shared = source(message_id="81")
    authorized = MessageEvent(text="authorized", source=shared, message_id="81")
    internal = MessageEvent(text="internal", source=shared, message_id="81")
    runner._stamp_event_memory_authorization(authorized, True)

    runner._queue_or_replace_pending_event("shared", authorized, merge_text=True)
    runner._queue_or_replace_pending_event("shared", internal, merge_text=True)

    assert adapter._pending_messages["shared"] is authorized
    assert authorized.text == "authorized"
    assert runner._queued_events["shared"] == [internal]


def test_pending_same_actor_different_boundary_is_not_merged():
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = SimpleNamespace(_pending_messages={})
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._queued_events = {}
    first_source = source(message_id="82", profile_name="Personal")
    second_source = replace(
        first_source,
        thread_id="different-topic",
        profile_name="Planning",
        scope_name="planning",
        memory_scope="planning",
    )
    first = MessageEvent(text="personal", source=first_source, message_id="82")
    second = MessageEvent(text="planning", source=second_source, message_id="82")
    runner._stamp_event_memory_authorization(first, True)
    runner._stamp_event_memory_authorization(second, True)

    runner._queue_or_replace_pending_event("shared", first, merge_text=True)
    runner._queue_or_replace_pending_event("shared", second, merge_text=True)

    assert adapter._pending_messages["shared"] is first
    assert first.text == "personal"
    assert runner._queued_events["shared"] == [second]


def test_pending_distinct_message_ids_are_not_merged_under_first_provenance():
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = SimpleNamespace(_pending_messages={})
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._queued_events = {}
    shared = source(message_id="83")
    first = MessageEvent(text="first", source=shared, message_id="83")
    second = MessageEvent(text="second", source=shared, message_id="84")
    runner._stamp_event_memory_authorization(first, True)
    runner._stamp_event_memory_authorization(second, True)

    runner._queue_or_replace_pending_event("shared", first, merge_text=True)
    runner._queue_or_replace_pending_event("shared", second, merge_text=True)

    assert adapter._pending_messages["shared"] is first
    assert first.text == "first"
    assert runner._queued_events["shared"] == [second]


def test_dm_group_and_profile_zone_changes_bust_agent_cache():
    personal = context(source(profile_name="Personal", memory_scope="personal"))
    planning = context(source(profile_name="Planning", memory_scope="planning"))
    group = context(
        source(
            chat_id="-100-family",
            chat_type="group",
            memory_scope="general",
        )
    )

    assert personal is not None and planning is not None and group is not None
    assert len({signature(personal), signature(planning), signature(group)}) == 3


def test_missing_context_explicitly_clears_agent_and_provider():
    current = context(source())
    assert current is not None
    agent, provider = agent_with_recording_provider()

    GatewayRunner._bind_agent_memory_access_context(agent, current)
    GatewayRunner._bind_agent_memory_access_context(agent, None)

    assert agent._memory_access_context is None
    assert provider.contexts[-1] is None
    assert provider._memory_access_context is None

    GatewayRunner._bind_agent_memory_access_context(agent, {"write_wing": "private.fake"})
    assert agent._memory_access_context is None
    assert provider.contexts[-1] is None


def test_provider_context_failure_removes_provider_without_logging_details(caplog):
    manager = MemoryManager()
    provider = RejectingContextProvider()
    manager.add_provider(provider)
    trusted_context = context(source())
    assert trusted_context is not None

    with caplog.at_level(logging.WARNING):
        manager.set_access_context(trusted_context)

    assert provider not in manager.providers
    assert "RuntimeError" in caplog.text
    assert "sensitive-token-value" not in caplog.text
    assert provider._memory_access_context is None


def test_scoped_mempalace_provider_requires_trusted_gateway_context():
    from plugins.memory.mempalace import MemPalaceMemoryProvider

    trusted_context = context(source())
    provider = MemPalaceMemoryProvider()

    assert trusted_context is not None
    assert provider.enforces_trusted_access_context is True
    assert _memory_provider_allowed_for_context(
        "mempalace", provider, trusted_context, platform="telegram"
    ) is True
    assert _memory_provider_allowed_for_context(
        "mempalace", provider, None, platform="telegram"
    ) is False


def test_every_gateway_external_provider_requires_context_and_enforcement():
    group_context = context(
        source(
            chat_id="-100-family",
            chat_type="group",
            memory_scope="general",
        )
    )
    non_enforcing = RecordingProvider()
    enforcing = EnforcingProvider()

    assert group_context is not None
    assert _memory_provider_allowed_for_context(
        "other-memory", non_enforcing, group_context, platform="telegram"
    ) is False
    assert _memory_provider_allowed_for_context(
        "other-memory", enforcing, None, platform="telegram"
    ) is False
    assert _memory_provider_allowed_for_context(
        "other-memory", enforcing, group_context, platform="telegram"
    ) is True
    assert _memory_provider_allowed_for_context(
        "other-memory", non_enforcing, None, platform="cli"
    ) is True
    assert _memory_provider_allowed_for_context(
        "other-memory",
        non_enforcing,
        None,
        platform="cli",
        gateway_session_key="agent:main:telegram:dm:alice",
    ) is False


def test_aiagent_constructor_preserves_only_typed_context_and_alt_identity():
    trusted_context = context(source())
    assert trusted_context is not None
    agent = AIAgent(
        model="test-model",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
        user_id_alt="stable-alt",
        memory_access_context=trusted_context,
        skip_context_files=True,
        skip_memory=True,
    )
    try:
        assert agent._user_id_alt == "stable-alt"
        assert agent._memory_access_context is trusted_context
    finally:
        agent.close()

    untrusted = AIAgent(
        model="test-model",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
        memory_access_context={"read_wings": ["private.fake"]},
        skip_context_files=True,
        skip_memory=True,
    )
    try:
        assert untrusted._memory_access_context is None
    finally:
        untrusted.close()


def test_gateway_resolves_observed_group_actor_only_from_current_raw_event(
    monkeypatch,
):
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"memory": {"access": access_config()}},
    )
    monkeypatch.setenv("HERMES_MEMORY_OPAQUE_ID_KEY", OPAQUE_ENV)
    runner = GatewayRunner.__new__(GatewayRunner)
    shared = source(
        chat_id="-100-family",
        chat_type="group",
        user_id=None,
        memory_scope="general",
    )
    event = MessageEvent(
        text="addressed group message",
        source=shared,
        message_id="77",
        raw_message=SimpleNamespace(from_user=SimpleNamespace(id="1001")),
    )

    resolved = runner._memory_access_context_for_event(
        event,
        shared,
        request_authorized=True,
    )

    assert resolved is not None
    assert resolved.principal_id == "p_k3m7w2r5t6y4q2zx"
    assert resolved.actor_ref.startswith("a_")
    assert resolved.source_ref.startswith("m_")
    assert shared.user_id is None


def test_gateway_missing_key_invalid_config_or_untrusted_event_fails_closed(
    monkeypatch,
):
    runner = GatewayRunner.__new__(GatewayRunner)
    event_source = source()
    event = MessageEvent(text="hello", source=event_source, message_id="55")
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"memory": {"access": access_config()}},
    )
    monkeypatch.delenv("HERMES_MEMORY_OPAQUE_ID_KEY", raising=False)

    assert runner._memory_access_context_for_event(
        event, event_source, request_authorized=True
    ) is None

    monkeypatch.setenv("HERMES_MEMORY_OPAQUE_ID_KEY", OPAQUE_ENV)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"memory": {"access": {"version": 1}}},
    )
    assert runner._memory_access_context_for_event(
        event, event_source, request_authorized=True
    ) is None
    assert runner._memory_access_context_for_event(
        event, event_source, request_authorized=False
    ) is None


def test_context_resolution_failure_does_not_log_sensitive_details(
    monkeypatch,
    caplog,
):
    runner = GatewayRunner.__new__(GatewayRunner)
    event_source = source()
    event = MessageEvent(text="hello", source=event_source, message_id="55")
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"memory": {"access": access_config()}},
    )
    monkeypatch.setenv("HERMES_MEMORY_OPAQUE_ID_KEY", OPAQUE_ENV)

    def reject_policy(_raw):
        raise RuntimeError("sensitive-policy-value")

    monkeypatch.setattr("gateway.memory_access.parse_memory_access_policy", reject_policy)
    with caplog.at_level(logging.WARNING):
        resolved = runner._memory_access_context_for_event(
            event,
            event_source,
            request_authorized=True,
        )

    assert resolved is None
    assert "RuntimeError" in caplog.text
    assert "sensitive-policy-value" not in caplog.text


def test_input_supplied_authorization_marker_cannot_enable_memory(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    event_source = source()
    event = MessageEvent(text="hello", source=event_source, message_id="55")
    event._gateway_memory_authorized = True
    event._gateway_memory_authorization_stamp = True
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"memory": {"access": access_config()}},
    )
    monkeypatch.setenv("HERMES_MEMORY_OPAQUE_ID_KEY", OPAQUE_ENV)

    assert runner._memory_event_is_authorized(event) is False
    assert runner._memory_access_context_for_event(
        event,
        event_source,
        request_authorized=runner._memory_event_is_authorized(event),
    ) is None

    runner._stamp_event_memory_authorization(event, False)
    assert event._gateway_memory_authorization_stamp is None
    runner._stamp_event_memory_authorization(event, True)
    assert runner._memory_event_is_authorized(event) is True
