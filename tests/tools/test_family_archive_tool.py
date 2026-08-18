import json
from types import SimpleNamespace

from gateway.session_context import clear_session_vars, set_session_vars
from tools import family_archive_tool as tool


def test_family_archive_fails_closed_outside_family_profile():
    tokens = set_session_vars(profile_name="default")
    try:
        result = json.loads(tool._call_backend("list_items", {}))
    finally:
        clear_session_vars(tokens)

    assert "error" in result
    assert "family-chat" in result["error"]


def test_family_archive_calls_isolated_backend_inside_family_profile(monkeypatch):
    completed = SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "structuredContent": {
                        "ok": True,
                        "verified": True,
                        "created": True,
                    }
                },
            }
        ),
    )
    monkeypatch.setattr(tool.subprocess, "run", lambda *args, **kwargs: completed)
    tokens = set_session_vars(profile_name="family-chat")
    try:
        result = json.loads(tool._call_backend("save_item", {"title": "Booking"}))
    finally:
        clear_session_vars(tokens)

    assert result == {"ok": True, "verified": True, "created": True}
