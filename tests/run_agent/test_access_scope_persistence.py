"""Access-scope persistence for runtime-created session rows."""

from types import SimpleNamespace


def test_ensure_db_session_persists_gateway_access_scope(tmp_path):
    from hermes_state import SessionDB
    from run_agent import AIAgent

    db = SessionDB(db_path=tmp_path / "sessions.db")
    agent = object.__new__(AIAgent)
    agent._session_db_created = False
    agent._session_db = db
    agent.session_id = "gateway-session"
    agent.platform = "telegram"
    agent.model = "test/model"
    agent._session_init_model_config = {"model": "test/model"}
    agent._cached_system_prompt = "system"
    agent._parent_session_id = None
    agent._gateway_session_key = "agent:main:telegram:dm:alice"

    agent._ensure_db_session()

    row = db.get_session("gateway-session")
    assert row is not None
    assert row["access_scope"] == "agent:main:telegram:dm:alice"


def test_compression_continuation_preserves_parent_access_scope(tmp_path):
    from agent.conversation_compression import compress_context
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session(
        session_id="parent-session",
        source="telegram",
        access_scope="agent:main:telegram:dm:alice",
    )

    class Compressor:
        _last_compress_aborted = False
        _last_summary_error = None
        _last_aux_model_failure_model = None
        _last_aux_model_failure_error = None
        compression_count = 1
        last_prompt_tokens = 0
        last_completion_tokens = 0

        def compress(self, messages, current_tokens=None, focus_topic=None, force=False):
            return [
                {"role": "user", "content": "[CONTEXT COMPACTION] Summary"},
                messages[-1],
            ]

    agent = SimpleNamespace(
        _compression_feasibility_checked=True,
        session_id="parent-session",
        model="test/model",
        _emit_status=lambda *_args, **_kwargs: None,
        _emit_warning=lambda *_args, **_kwargs: None,
        _memory_manager=None,
        context_compressor=Compressor(),
        _todo_store=SimpleNamespace(format_for_injection=lambda: None),
        _invalidate_system_prompt=lambda: None,
        _build_system_prompt=lambda _system_message: "new system prompt",
        _cached_system_prompt="old system prompt",
        _session_db=db,
        commit_memory_session=lambda _messages: None,
        platform="telegram",
        _session_init_model_config={"model": "test/model"},
        _gateway_session_key=None,
        _last_flushed_db_idx=7,
        tools=[],
        log_prefix="",
        _vprint=lambda *_args, **_kwargs: None,
    )

    compress_context(
        agent,
        [{"role": "user", "content": "hello"}],
        "system",
        approx_tokens=123,
    )

    assert agent.session_id != "parent-session"
    parent = db.get_session("parent-session")
    child = db.get_session(agent.session_id)
    assert parent is not None
    assert child is not None
    assert parent["end_reason"] == "compression"
    assert child["parent_session_id"] == "parent-session"
    assert child["access_scope"] == "agent:main:telegram:dm:alice"
    assert agent._session_db_created is True
    assert agent._last_flushed_db_idx == 0
