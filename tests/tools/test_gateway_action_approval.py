"""Focused tests for generic gateway action approval persistence."""

from tools import approval


def _reset_action_state(session_key: str, action_key: str) -> None:
    approval.unregister_gateway_notify(session_key)
    approval.clear_session(session_key)
    with approval._lock:
        approval._permanent_approved.discard(action_key)


def test_gateway_action_approval_once_does_not_persist(monkeypatch):
    session_key = "action-approval-once"
    action_key = "send_message:cross_chat:telegram:1->telegram:2"
    _reset_action_state(session_key, action_key)
    monkeypatch.setattr(
        approval,
        "get_current_session_key",
        lambda default="default": session_key,
    )
    approval.register_gateway_notify(session_key, lambda _data: None)
    monkeypatch.setattr(
        approval,
        "_await_gateway_decision",
        lambda *_args, **_kwargs: {"resolved": True, "choice": "once"},
    )

    result = approval.check_gateway_action_approval(
        action_key=action_key,
        action_preview="send_message cross-chat",
        description="cross-chat delivery",
    )

    assert result["approved"] is True
    assert result["approval_scope"] == "once"
    assert approval.is_approved(session_key, action_key) is False
    _reset_action_state(session_key, action_key)


def test_gateway_action_approval_session_is_temporary(monkeypatch):
    session_key = "action-approval-session"
    action_key = "send_message:cross_chat:telegram:1->telegram:2"
    _reset_action_state(session_key, action_key)
    monkeypatch.setattr(
        approval,
        "get_current_session_key",
        lambda default="default": session_key,
    )
    approval.register_gateway_notify(session_key, lambda _data: None)
    monkeypatch.setattr(
        approval,
        "_await_gateway_decision",
        lambda *_args, **_kwargs: {"resolved": True, "choice": "session"},
    )

    result = approval.check_gateway_action_approval(
        action_key=action_key,
        action_preview="send_message cross-chat",
        description="cross-chat delivery",
    )

    assert result["approved"] is True
    assert approval.is_approved(session_key, action_key) is True
    approval.clear_session(session_key)
    assert approval.is_approved(session_key, action_key) is False
    _reset_action_state(session_key, action_key)


def test_gateway_action_approval_always_persists_route(monkeypatch):
    session_key = "action-approval-always"
    action_key = "send_message:cross_chat:telegram:1->telegram:2"
    _reset_action_state(session_key, action_key)
    monkeypatch.setattr(
        approval,
        "get_current_session_key",
        lambda default="default": session_key,
    )
    approval.register_gateway_notify(session_key, lambda _data: None)
    monkeypatch.setattr(
        approval,
        "_await_gateway_decision",
        lambda *_args, **_kwargs: {"resolved": True, "choice": "always"},
    )
    saved = []
    monkeypatch.setattr(
        approval,
        "save_permanent_allowlist",
        lambda patterns: saved.append(set(patterns)),
    )

    result = approval.check_gateway_action_approval(
        action_key=action_key,
        action_preview="send_message cross-chat",
        description="cross-chat delivery",
    )

    approval.clear_session(session_key)
    assert result["approved"] is True
    assert approval.is_approved(session_key, action_key) is True
    assert saved and action_key in saved[-1]
    _reset_action_state(session_key, action_key)
