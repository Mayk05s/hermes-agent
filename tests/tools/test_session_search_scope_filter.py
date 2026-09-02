import json

from tools.session_search_tool import session_search


class ScopedFakeDB:
    def __init__(self):
        self.search_kwargs = None
        self.list_kwargs = None

    def search_messages(self, **kwargs):
        self.search_kwargs = kwargs
        return []

    def list_sessions_rich(self, **kwargs):
        self.list_kwargs = kwargs
        return []


class UnscopedLegacyFakeDB:
    def search_messages(self, **kwargs):
        if "scope_filter" in kwargs:
            raise TypeError("unexpected keyword argument 'scope_filter'")
        return [{"session_id": "leaked", "id": 1, "role": "user", "snippet": "secret"}]

    def list_sessions_rich(self, **kwargs):
        if "scope_filter" in kwargs:
            raise TypeError("unexpected keyword argument 'scope_filter'")
        return [{"id": "leaked", "source": "telegram", "preview": "secret"}]


class ScrollFakeDB:
    def get_session(self, session_id):
        return {"id": session_id, "access_scope": "agent:main:telegram:dm:bob"}

    def get_messages_around(self, session_id, around_message_id, window=5):
        return {
            "window": [{"id": around_message_id, "role": "user", "content": "bob secret"}],
            "messages_before": 0,
            "messages_after": 0,
        }


class NeverCalledDB:
    def __getattr__(self, name):
        raise AssertionError(f"DB must not be used without a trusted scope: {name}")


def test_session_search_passes_current_scope_filter_to_db_discovery():
    db = ScopedFakeDB()

    result = json.loads(session_search(
        query="deploy",
        db=db,
        current_access_scope="agent:main:telegram:dm:alice",
    ))

    assert result["success"] is True
    assert db.search_kwargs["scope_filter"] == "agent:main:telegram:dm:alice"


def test_session_search_browse_passes_current_scope_filter_to_db():
    db = ScopedFakeDB()

    result = json.loads(session_search(
        db=db,
        current_access_scope="agent:main:telegram:dm:alice",
    ))

    assert result["success"] is True
    assert db.list_kwargs["scope_filter"] == "agent:main:telegram:dm:alice"


def test_scoped_discovery_fails_closed_when_db_lacks_scope_filter():
    result = json.loads(session_search(
        query="secret",
        db=UnscopedLegacyFakeDB(),
        current_access_scope="agent:main:telegram:dm:alice",
    ))

    assert result["success"] is False
    assert "scope" in result["error"].lower()


def test_scoped_browse_fails_closed_when_db_lacks_scope_filter():
    result = json.loads(session_search(
        db=UnscopedLegacyFakeDB(),
        current_access_scope="agent:main:telegram:dm:alice",
    ))

    assert result["success"] is False
    assert "scope" in result["error"].lower()


def test_scoped_scroll_denies_raw_cross_scope_read():
    result = json.loads(session_search(
        session_id="bob-session",
        around_message_id=1,
        db=ScrollFakeDB(),
        current_access_scope="agent:main:telegram:dm:alice",
    ))

    assert result["success"] is False
    assert "scope" in result["error"].lower()
    assert "bob secret" not in json.dumps(result)


def test_gateway_search_without_scope_fails_before_opening_db():
    result = json.loads(session_search(
        query="private canary",
        db=NeverCalledDB(),
        current_access_scope=None,
        require_access_scope=True,
    ))

    assert result == {
        "success": False,
        "error": "Session history is unavailable for the current access scope.",
    }


def test_gateway_search_with_malformed_scope_fails_before_query():
    result = json.loads(session_search(
        query="private canary",
        db=NeverCalledDB(),
        current_access_scope="../../other-user",
        require_access_scope=True,
    ))

    assert result == {
        "success": False,
        "error": "Session history is unavailable for the current access scope.",
    }


def test_group_search_never_returns_private_dm_canary(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        private_scope = "agent:main:telegram:dm:mikhail"
        group_scope = "agent:main:telegram:group:family"
        db.create_session("private", "telegram", access_scope=private_scope)
        db.append_message(
            "private", "user", "PRIVATE_SESSION_CANARY_MIKHAIL"
        )
        db.create_session("group", "telegram", access_scope=group_scope)
        db.append_message("group", "user", "ordinary group context")

        result = json.loads(session_search(
            query="PRIVATE_SESSION_CANARY_MIKHAIL",
            db=db,
            current_session_id="group",
            current_access_scope=group_scope,
            require_access_scope=True,
        ))

        assert result["success"] is True
        assert result["results"] == []
        assert "PRIVATE_SESSION_CANARY_MIKHAIL" not in json.dumps(
            result["results"]
        )
    finally:
        db.close()
