"""Mediated cross-scope access grant primitives.

Grants authorize one scope to *ask* another scope a question.  They never grant
raw transcript/history/memory reads; callers must route through a mediated
query executor that returns only a final answer/summary from the target scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from gateway.access_scope import normalize_scope_key
from tools import approval

GrantLifetime = Literal["once", "session", "always", "deny"]


@dataclass(frozen=True)
class AccessGrantDecision:
    allowed: bool
    requires_approval: bool
    grant_key: str
    reason: str = ""


def access_grant_key(kind: str, source_scope: str, target_scope: str) -> str:
    """Build a directional grant key suitable for approval.py storage."""

    src = normalize_scope_key(source_scope)
    dst = normalize_scope_key(target_scope)
    if not src or not dst:
        raise ValueError("source_scope and target_scope must be valid scope keys")
    if not kind or not isinstance(kind, str) or not kind.replace("_", "").isalnum():
        raise ValueError("grant kind must be alphanumeric/underscore")
    return f"{kind}:{src}->{dst}"


def check_scope_query_grant(source_scope: str, target_scope: str) -> AccessGrantDecision:
    """Check whether ``source_scope`` may ask ``target_scope`` a mediated question."""

    grant_key = access_grant_key("scope_query", source_scope, target_scope)
    allowed = approval.is_approved(source_scope, grant_key)
    if allowed:
        return AccessGrantDecision(
            allowed=True,
            requires_approval=False,
            grant_key=grant_key,
            reason="cross-scope query grant approved",
        )
    return AccessGrantDecision(
        allowed=False,
        requires_approval=True,
        grant_key=grant_key,
        reason="cross-scope query requires mediated approval",
    )


def record_scope_query_grant(source_scope: str, target_scope: str, lifetime: GrantLifetime) -> str:
    """Record a mediated query grant using approval.py lifetimes.

    ``once`` is intentionally not persisted: the caller may proceed with the
    current mediated query, but no future raw/read capability is stored.
    """

    grant_key = access_grant_key("scope_query", source_scope, target_scope)
    if lifetime == "session":
        approval.approve_session(source_scope, grant_key)
    elif lifetime == "always":
        approval.approve_permanent(grant_key)
    elif lifetime in {"once", "deny"}:
        pass
    else:
        raise ValueError("lifetime must be one of: once, session, always, deny")
    return grant_key
