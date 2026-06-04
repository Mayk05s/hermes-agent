"""MemPalace memory provider.

This provider is intentionally local-only. It reads the profile-scoped
MemPalace SQLite graphs managed by :mod:`hermes_cli.mempalace`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from hermes_cli import mempalace
from tools.registry import tool_error

logger = logging.getLogger(__name__)


SEARCH_SCHEMA = {
    "name": "mempalace_search",
    "description": (
        "Search the profile-scoped MemPalace knowledge graph. Use when past "
        "chat context, durable user facts, project facts, or cross-chat memory "
        "would help answer the current request."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language search text."},
            "palace": {
                "type": "string",
                "description": "Optional palace/group name, e.g. telegram_health.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum result count, default 20, max 100.",
            },
        },
        "required": ["query"],
    },
}


GRAPH_SCHEMA = {
    "name": "mempalace_graph",
    "description": (
        "Load graph nodes, edges, and attributes for a MemPalace palace. "
        "Pass query to focus on matching entities."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "palace": {"type": "string", "description": "Palace/group name."},
            "query": {"type": "string", "description": "Optional entity/topic filter."},
            "node_limit": {"type": "integer", "description": "Max nodes, default 80."},
            "edge_limit": {"type": "integer", "description": "Max edges, default 160."},
        },
        "required": ["palace"],
    },
}


class MemPalaceMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._profile = "default"
        self._profile_home: Optional[Path] = None
        self._inactive = False

    def _refresh_if_due(self, *, force: bool = False) -> None:
        if self._inactive:
            return
        try:
            result = mempalace.refresh_if_due(
                profile=self._profile,
                profile_home=self._profile_home,
                force=force,
            )
            if result.get("refreshed"):
                logger.info("MemPalace refreshed for profile=%s reason=%s", self._profile, result.get("reason"))
        except Exception as exc:
            logger.debug("MemPalace refresh check failed: %s", exc)

    @property
    def name(self) -> str:
        return "mempalace"

    def is_available(self) -> bool:
        try:
            return bool(mempalace.list_palaces(include_stats=False))
        except Exception:
            # The provider is still selectable before data is imported; initialize
            # will simply stay quiet until palaces exist.
            return True

    def initialize(self, session_id: str, **kwargs) -> None:
        agent_context = kwargs.get("agent_context", "")
        platform = kwargs.get("platform", "cli")
        if agent_context in {"cron", "flush"} or platform == "cron":
            self._inactive = True
            return
        self._profile = str(kwargs.get("agent_identity") or "default")
        home = kwargs.get("hermes_home")
        if home:
            self._profile_home = Path(str(home)).resolve()
        try:
            palace_count = len(
                mempalace.list_palaces(
                    profile=self._profile,
                    profile_home=self._profile_home,
                    include_stats=False,
                )
            )
            logger.info("MemPalace initialized for profile=%s palaces=%d", self._profile, palace_count)
        except Exception as exc:
            logger.debug("MemPalace initialize probe failed: %s", exc)

    def system_prompt_block(self) -> str:
        if self._inactive:
            return ""
        return (
            "MemPalace memory is available. Use mempalace_search for durable "
            "profile-scoped facts from prior chats when relevant; do not search "
            "on every turn."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._inactive or not query.strip():
            return ""
        try:
            return mempalace.recall_context(
                query,
                profile=self._profile,
                profile_home=self._profile_home,
                max_items=10,
            )
        except Exception as exc:
            logger.debug("MemPalace prefetch failed: %s", exc)
            return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        return None

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, GRAPH_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            if tool_name == "mempalace_search":
                query = str(args.get("query") or "").strip()
                if not query:
                    return tool_error("query is required")
                result = mempalace.search(
                    query,
                    profile=self._profile,
                    profile_home=self._profile_home,
                    palace=str(args.get("palace") or ""),
                    limit=int(args.get("limit") or 20),
                )
                return json.dumps(result, ensure_ascii=False)
            if tool_name == "mempalace_graph":
                palace = str(args.get("palace") or "").strip()
                if not palace:
                    return tool_error("palace is required")
                result = mempalace.load_graph(
                    palace,
                    profile=self._profile,
                    profile_home=self._profile_home,
                    query=str(args.get("query") or ""),
                    node_limit=int(args.get("node_limit") or 80),
                    edge_limit=int(args.get("edge_limit") or 160),
                )
                return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return tool_error(f"MemPalace error: {exc}")
        return tool_error(f"Unknown MemPalace tool: {tool_name}")

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._refresh_if_due(force=True)
        return None


def register(ctx) -> None:
    ctx.register_memory_provider(MemPalaceMemoryProvider())
