from types import SimpleNamespace

import model_tools

from gateway import run as gateway_run
from gateway.session_context import clear_session_vars, set_session_vars
from tools.registry import invalidate_check_fn_cache, registry


def _source(*, user_id=""):
    return SimpleNamespace(
        profile_name="personal",
        user_id=user_id,
        chat_id="-1003735932411",
        thread_id="313",
    )


def test_gateway_has_no_calendar_semantic_interceptor():
    """Calendar meaning belongs to the model, not Telegram phrase matching."""
    assert not hasattr(gateway_run, "_should_prefetch_personal_calendar")
    assert not hasattr(gateway_run, "_calendar_prefetch_task")
    assert not hasattr(gateway_run, "_prefetch_personal_calendar_context")


def test_gateway_keeps_calendar_toolset_for_allowed_current_requester():
    source = _source()
    tokens = set_session_vars(
        platform="telegram",
        profile_name="personal",
        requester_user_id="179555559",
        chat_id=source.chat_id,
        thread_id=source.thread_id,
    )
    try:
        scoped = gateway_run._scope_gateway_toolsets_for_source(
            ["messaging", "codex_calendar"],
            {},
            "telegram",
            source,
        )
    finally:
        clear_session_vars(tokens)

    assert "codex_calendar" in scoped


def test_gateway_does_not_semantically_or_per_user_filter_calendar_toolset():
    source = _source()
    tokens = set_session_vars(
        platform="telegram",
        profile_name="personal",
        requester_user_id="367599252",
        chat_id=source.chat_id,
        thread_id=source.thread_id,
    )
    try:
        scoped = gateway_run._scope_gateway_toolsets_for_source(
            ["messaging", "codex_calendar"],
            {},
            "telegram",
            source,
        )
    finally:
        clear_session_vars(tokens)

    assert "codex_calendar" in scoped


def test_google_calendar_is_a_normal_model_visible_tool(monkeypatch):
    """The agent receives google_calendar in its ordinary tools array."""
    entry = registry.get_entry("google_calendar")
    assert entry is not None
    monkeypatch.setattr(entry, "check_fn", lambda: True)
    invalidate_check_fn_cache()
    model_tools._clear_tool_defs_cache()

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=["codex_calendar"],
        quiet_mode=True,
    )
    names = {
        definition["function"]["name"]
        for definition in definitions
    }

    assert "google_calendar" in names
    assert "tool_call" not in names
