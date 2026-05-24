"""Mediated cross-scope query tool skeleton.

This tool deliberately does not read another chat's history or memory.  It only
checks whether the current/source scope is allowed to ask a target scope a
specific question, then delegates to a mediated runner which must execute inside
the target scope and return a final answer/summary.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from gateway.access_grants import check_scope_query_grant
from gateway.access_scope import normalize_scope_key

MediatedRunner = Callable[..., str]
GrantChecker = Callable[[str, str], bool]


def _default_mediated_runner(*, source_scope: str, target_scope: str, question: str) -> str:
    raise NotImplementedError(
        "mediated cross-scope agent execution is not wired yet; this foundational "
        "slice only enforces grants and the no-raw-read tool contract"
    )


def query_chat_agent(
    question: str,
    target_scope: str,
    source_scope: Optional[str] = None,
    *,
    mediated_runner: Optional[MediatedRunner] = None,
    grant_checker: Optional[GrantChecker] = None,
) -> str:
    """Ask another chat/topic agent a concrete question through a mediated grant.

    The response shape never includes raw messages/history/memory.  Missing
    grants return a structured ``requires_approval`` payload with the directional
    grant key the gateway/UX can approve using once/session/always/deny.
    """

    src = normalize_scope_key(source_scope or "")
    dst = normalize_scope_key(target_scope)
    if not src:
        return json.dumps({"success": False, "error": "source_scope is required"}, ensure_ascii=False)
    if not dst:
        return json.dumps({"success": False, "error": "target_scope is required"}, ensure_ascii=False)
    if not isinstance(question, str) or not question.strip():
        return json.dumps({"success": False, "error": "question is required"}, ensure_ascii=False)

    decision = check_scope_query_grant(src, dst)
    if grant_checker is not None:
        allowed = bool(grant_checker(src, dst))
    else:
        allowed = decision.allowed

    if not allowed:
        return json.dumps({
            "success": False,
            "requires_approval": True,
            "grant_key": decision.grant_key,
            "source_scope": src,
            "target_scope": dst,
            "reason": decision.reason,
            "message": "Approval permits asking the target chat agent; it does not permit reading raw history or memory.",
        }, ensure_ascii=False)

    runner = mediated_runner or _default_mediated_runner
    try:
        answer = runner(source_scope=src, target_scope=dst, question=question.strip())
    except NotImplementedError as exc:
        return json.dumps({
            "success": False,
            "error": str(exc),
            "source_scope": src,
            "target_scope": dst,
        }, ensure_ascii=False)

    return json.dumps({
        "success": True,
        "source_scope": src,
        "target_scope": dst,
        "answer": str(answer),
    }, ensure_ascii=False)


QUERY_CHAT_AGENT_SCHEMA = {
    "name": "query_chat_agent",
    "description": (
        "Ask another chat/topic agent a concrete question via a mediated cross-scope grant. "
        "This does not expose raw history, messages, or memory; the caller receives only the target agent's final answer/summary."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Specific question to ask the target chat/topic agent."},
            "target_scope": {"type": "string", "description": "Canonical target access scope key."},
            "source_scope": {"type": "string", "description": "Canonical current/source access scope key."},
        },
        "required": ["question", "target_scope"],
    },
}


from tools.registry import registry  # noqa: E402

registry.register(
    name="query_chat_agent",
    toolset="session_search",
    schema=QUERY_CHAT_AGENT_SCHEMA,
    handler=lambda args, **kw: query_chat_agent(
        question=args.get("question", ""),
        target_scope=args.get("target_scope", ""),
        source_scope=args.get("source_scope") or kw.get("current_access_scope"),
    ),
    emoji="🔐",
)
