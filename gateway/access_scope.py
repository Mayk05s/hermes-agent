"""Canonical access-scope helpers for gateway-originated conversations.

An access scope is the security boundary for chat/topic isolation.  It is
intentionally represented by the same deterministic key the gateway already
uses to route/resume sessions, so future storage-layer filters can join against
one stable identifier instead of re-deriving platform-specific chat semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

from gateway.session import SessionSource, build_session_key

_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-=]*$")


@dataclass(frozen=True)
class AccessScope:
    """A normalized chat/topic/user security boundary."""

    key: str
    platform: str
    chat_id: str
    chat_type: str
    thread_id: Optional[str] = None
    user_id: Optional[str] = None

    @property
    def label(self) -> str:
        parts = [f"{self.platform}/{self.chat_type}"]
        if self.chat_id:
            parts.append(self.chat_id)
        if self.thread_id:
            parts.append(f"thread {self.thread_id}")
        return " ".join(parts)


def normalize_scope_key(value: object) -> Optional[str]:
    """Return a safe scope key or ``None`` for empty/malformed values."""

    if not isinstance(value, str):
        return None
    key = value.strip()
    if not key or ".." in key or not _SCOPE_RE.fullmatch(key):
        return None
    return key


def access_scope_from_source(
    source: SessionSource,
    *,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
) -> AccessScope:
    """Build the canonical access scope for a gateway ``SessionSource``."""

    return AccessScope(
        key=build_session_key(
            source,
            group_sessions_per_user=group_sessions_per_user,
            thread_sessions_per_user=thread_sessions_per_user,
        ),
        platform=source.platform.value,
        chat_id=str(source.chat_id or ""),
        chat_type=source.chat_type or "dm",
        thread_id=str(source.thread_id) if source.thread_id else None,
        user_id=str(source.user_id_alt or source.user_id) if (source.user_id_alt or source.user_id) else None,
    )
