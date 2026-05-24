from gateway.config import Platform
from gateway.session import SessionSource
from gateway.access_scope import AccessScope, access_scope_from_source, normalize_scope_key


def test_access_scope_matches_gateway_session_key_for_same_topic():
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="group",
        user_id="user-1",
        thread_id="topic-7",
    )

    scope = access_scope_from_source(source)

    assert scope == AccessScope(
        key="agent:main:telegram:group:chat-1:topic-7",
        platform="telegram",
        chat_id="chat-1",
        chat_type="group",
        thread_id="topic-7",
        user_id="user-1",
    )
    assert scope.label == "telegram/group chat-1 thread topic-7"


def test_access_scope_separates_group_participants_outside_shared_threads():
    a = access_scope_from_source(SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="group",
        user_id="alice",
    ))
    b = access_scope_from_source(SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="group",
        user_id="bob",
    ))

    assert a.key.endswith(":alice")
    assert b.key.endswith(":bob")
    assert a.key != b.key


def test_normalize_scope_key_rejects_empty_or_malformed_values():
    assert normalize_scope_key(" agent:main:telegram:dm:42 ") == "agent:main:telegram:dm:42"
    assert normalize_scope_key("") is None
    assert normalize_scope_key("../agent:main:telegram:dm:42") is None
    assert normalize_scope_key("agent:main:telegram:\nsecret") is None
