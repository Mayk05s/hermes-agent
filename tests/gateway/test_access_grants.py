import pytest

from gateway.access_grants import (
    AccessGrantDecision,
    access_grant_key,
    check_scope_query_grant,
    record_scope_query_grant,
)
from tools import approval


@pytest.fixture(autouse=True)
def clear_approval_state():
    approval.clear_session("agent:main:telegram:dm:alice")
    approval.clear_session("agent:main:telegram:dm:bob")
    yield
    approval.clear_session("agent:main:telegram:dm:alice")
    approval.clear_session("agent:main:telegram:dm:bob")


def test_scope_query_grant_key_is_directional_and_not_a_read_grant():
    ask_key = access_grant_key(
        "scope_query",
        "agent:main:telegram:dm:alice",
        "agent:main:telegram:dm:bob",
    )
    reverse_key = access_grant_key(
        "scope_query",
        "agent:main:telegram:dm:bob",
        "agent:main:telegram:dm:alice",
    )
    read_key = access_grant_key(
        "scope_read",
        "agent:main:telegram:dm:alice",
        "agent:main:telegram:dm:bob",
    )

    assert ask_key == "scope_query:agent:main:telegram:dm:alice->agent:main:telegram:dm:bob"
    assert ask_key != reverse_key
    assert ask_key != read_key


def test_missing_grant_returns_requires_approval_payload():
    decision = check_scope_query_grant(
        source_scope="agent:main:telegram:dm:alice",
        target_scope="agent:main:telegram:dm:bob",
    )

    assert decision == AccessGrantDecision(
        allowed=False,
        requires_approval=True,
        grant_key="scope_query:agent:main:telegram:dm:alice->agent:main:telegram:dm:bob",
        reason="cross-scope query requires mediated approval",
    )


def test_session_grant_authorizes_asking_only_for_current_source_scope():
    record_scope_query_grant(
        source_scope="agent:main:telegram:dm:alice",
        target_scope="agent:main:telegram:dm:bob",
        lifetime="session",
    )

    assert check_scope_query_grant(
        source_scope="agent:main:telegram:dm:alice",
        target_scope="agent:main:telegram:dm:bob",
    ).allowed is True
    assert check_scope_query_grant(
        source_scope="agent:main:telegram:dm:bob",
        target_scope="agent:main:telegram:dm:alice",
    ).allowed is False


def test_deny_lifetime_never_records_an_approval():
    record_scope_query_grant(
        source_scope="agent:main:telegram:dm:alice",
        target_scope="agent:main:telegram:dm:bob",
        lifetime="deny",
    )

    assert check_scope_query_grant(
        source_scope="agent:main:telegram:dm:alice",
        target_scope="agent:main:telegram:dm:bob",
    ).allowed is False
