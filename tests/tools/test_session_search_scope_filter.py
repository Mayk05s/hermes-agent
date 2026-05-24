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
