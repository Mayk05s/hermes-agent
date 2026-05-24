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
