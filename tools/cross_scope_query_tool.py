"""Disabled cross-scope query tool placeholder.

The former skeleton accepted a model-selected target and a generic directional
grant.  That is not a safe disclosure boundary: private-to-group disclosure
requires a separate selected-fact broker, not target-agent transcript access.
Keep the registered handler fail-closed until that broker exists.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

MediatedRunner = Callable[..., str]
GrantChecker = Callable[[str, str], bool]


def query_chat_agent(
    question: str,
    target_scope: str,
    source_scope: Optional[str] = None,
    *,
    mediated_runner: Optional[MediatedRunner] = None,
    grant_checker: Optional[GrantChecker] = None,
) -> str:
    """Fail closed regardless of target, grants, or injected runner.

    Parameters remain for source compatibility only.  In particular, neither a
    model-selected ``target_scope`` nor a generic grant can enable access.
    """

    del question, target_scope, source_scope, mediated_runner, grant_checker
    return json.dumps(
        {
            "success": False,
            "error": (
                "Cross-scope query is unavailable until a selected-fact broker "
                "is configured."
            ),
        },
        ensure_ascii=False,
    )


QUERY_CHAT_AGENT_SCHEMA = {
    "name": "query_chat_agent",
    "description": (
        "Reserved selected-fact broker operation. This capability is currently "
        "disabled and never reads another chat's transcript or memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Specific question to ask the target chat/topic agent."},
            "target_scope": {"type": "string", "description": "Canonical target access scope key."},
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
        # Source scope is trusted runtime context, not model-supplied input.
        # Ignore any stale/injected args["source_scope"] value.
        source_scope=kw.get("current_access_scope"),
    ),
    emoji="🔐",
)
