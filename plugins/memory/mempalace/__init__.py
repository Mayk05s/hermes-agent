"""Capability-scoped HTTP/MCP adapter for the MemPalace service.

The model sees semantic arguments only. Principal, destination, wings,
provenance, grants, credentials, and capability claims come exclusively from
the immutable gateway ``MemoryAccessContext`` and are signed by the client.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from gateway.memory_access import MemoryAccessContext
from tools.registry import tool_error

from .capability_client import (
    MemPalaceCapabilityClient,
    MemPalaceClientError,
    STRING_MAX_UTF8_BYTES,
    TOOL_ARGUMENT_SPECS,
    TOOL_OPERATIONS,
    validate_tool_arguments,
)


logger = logging.getLogger(__name__)


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, MemPalaceClientError):
        return exc.code
    return type(exc).__name__


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_TOOL_DESCRIPTIONS = {
    "get_document": "Read one authorized stored document by ID.",
    "list_wings": "List memory zones authorized for the current chat.",
    "mempalace_add_drawer": "Add a drawer to the current authorized memory zone.",
    "mempalace_delete_drawer": "Delete an authorized memory drawer by ID.",
    "mempalace_get_drawer": "Read one authorized memory drawer by ID.",
    "mempalace_get_taxonomy": "Read the authorized memory taxonomy.",
    "mempalace_kg_add": "Add a scoped knowledge-graph fact.",
    "mempalace_kg_invalidate": "Invalidate a scoped knowledge-graph fact.",
    "mempalace_kg_query": "Query scoped knowledge-graph facts for an entity.",
    "mempalace_kg_stats": "Read scoped knowledge-graph statistics.",
    "mempalace_kg_timeline": "Read a scoped knowledge-graph timeline.",
    "mempalace_list_drawers": "List authorized memory drawers within a room.",
    "mempalace_list_rooms": "List memory rooms authorized for the current chat.",
    "mempalace_list_wings": "List memory zones authorized for the current chat.",
    "mempalace_search": "Search durable memory authorized for the current chat.",
    "mempalace_update_drawer": "Update an authorized memory drawer.",
    "put_page": "Store a page in the current authorized memory zone.",
    "query": "Search durable memory authorized for the current chat.",
    "upload_document": "File a base64-encoded document in the current authorized memory zone.",
}


def _argument_schema(name: str, *, required: bool) -> dict[str, Any]:
    if name == "limit":
        return {"type": "integer", "minimum": 1, "maximum": 50}
    if name == "offset":
        return {"type": "integer", "minimum": 0, "maximum": 49}
    if name == "max_distance":
        return {"type": "number", "minimum": 0, "maximum": 2}
    if name == "min_similarity":
        return {"type": "number", "minimum": -1, "maximum": 1}
    schema: dict[str, Any] = {
        "type": "string",
        "maxLength": STRING_MAX_UTF8_BYTES[name],
    }
    if required:
        schema["minLength"] = 1
    if name == "doc_id":
        schema["pattern"] = "^[0-9a-f]{8,64}$"
    return schema


def _tool_schema(name: str) -> dict[str, Any]:
    allowed, required = TOOL_ARGUMENT_SPECS[name]
    properties = {
        argument: _argument_schema(argument, required=argument in required)
        for argument in sorted(allowed)
    }
    description = _TOOL_DESCRIPTIONS[name]
    if name == "mempalace_update_drawer":
        description += " Supply at least one of content or room."
    return {
        "name": name,
        "description": description,
        "parameters": _object_schema(properties, sorted(required)),
    }


TOOL_SCHEMAS: List[Dict[str, Any]] = [
    _tool_schema(name) for name in TOOL_ARGUMENT_SPECS
]


_REMOTE_TOOLS: dict[str, tuple[str, str]] = {
    name: (name, TOOL_OPERATIONS[name]) for name in TOOL_ARGUMENT_SPECS
}


def _result_text(result: Any, *, maximum: int = 256_000) -> str:
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = [
                item.get("text")
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ]
            if parts:
                return "\n".join(parts)[:maximum]
    return json.dumps(result, ensure_ascii=False, allow_nan=False)[:maximum]


class MemPalaceMemoryProvider(MemoryProvider):
    def __init__(self, *, client: Optional[MemPalaceCapabilityClient] = None) -> None:
        self._client = client or MemPalaceCapabilityClient()
        self._context_lock = threading.Lock()
        self._memory_access_context: Optional[MemoryAccessContext] = None
        self._inactive = False

    @property
    def name(self) -> str:
        return "mempalace"

    @property
    def enforces_trusted_access_context(self) -> bool:
        return True

    def set_access_context(self, context: Any) -> None:
        trusted = context if type(context) is MemoryAccessContext else None
        with self._context_lock:
            self._memory_access_context = trusted

    def _context(self) -> MemoryAccessContext:
        with self._context_lock:
            context = self._memory_access_context
        if type(context) is not MemoryAccessContext:
            raise MemPalaceClientError("memory_access_denied")
        return context

    def is_available(self) -> bool:
        return self._client.configured()

    def initialize(self, session_id: str, **kwargs) -> None:
        agent_context = kwargs.get("agent_context", "")
        platform = kwargs.get("platform", "")
        self._inactive = agent_context in {"cron", "flush", "subagent"} or platform == "cron"
        self.set_access_context(None if self._inactive else kwargs.get("memory_access_context"))

    def system_prompt_block(self) -> str:
        try:
            self._context()
            if self._inactive or not self._client.configured():
                return ""
        except MemPalaceClientError:
            return ""
        return (
            "MemPalace scoped memory is available. Use its search and read tools "
            "only when durable context would help. Access is fixed by the current "
            "verified chat; tool arguments cannot select a user or memory zone."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._inactive or not isinstance(query, str) or not query.strip():
            return ""
        bounded = query.strip().encode("utf-8")[:250].decode("utf-8", errors="ignore").strip()
        if not bounded:
            return ""
        try:
            args = validate_tool_arguments(
                "mempalace_search",
                {"query": bounded, "limit": 10},
            )
            result = self._client.mcp_call(
                self._context(),
                "mempalace_search",
                args,
                operation="read",
            )
            return _result_text(result, maximum=24_000)
        except Exception as exc:
            logger.debug(
                "MemPalace prefetch unavailable (%s)",
                _safe_error_code(exc),
            )
            return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        # H2 deliberately does not activate ingestion. The HTTP transport is
        # implemented by ``ingest_messages`` below, but the current MemPalace
        # daemon must first pass P3 wing/provenance isolation review.
        return None

    def ingest_messages(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        """Prepared H2 transport; runtime sync remains disabled until P3."""
        return self._client.ingest(self._context(), messages, kind="chunk")

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # AIAgent freezes schemas and MemoryManager's routing map at agent
        # construction, while per-event source_ref is deliberately excluded
        # from the cache key. Keep the frozen semantic surface stable; every
        # invocation re-checks the current typed context before transport.
        return list(TOOL_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            validated = validate_tool_arguments(tool_name, args)
            remote_tool, operation = _REMOTE_TOOLS[tool_name]
            result = self._client.mcp_call(
                self._context(),
                remote_tool,
                validated,
                operation=operation,
            )
            return _result_text(result)
        except Exception as exc:
            logger.warning(
                "MemPalace tool request denied (%s)",
                _safe_error_code(exc),
            )
            return tool_error("MemPalace memory request unavailable")

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        # Do not mirror profile-global MEMORY.md/USER.md writes. H3 will guard
        # those built-in files separately; P3 gates scoped ingestion.
        return None

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "base_url",
                "description": "MemPalace bridge base URL",
                "required": True,
                "env_var": "MEMPALACE_BASE_URL",
            },
            {
                "key": "bearer_token",
                "description": "Independent coarse capability-tier bearer",
                "required": True,
                "secret": True,
                "env_var": "MEMPALACE_BEARER_TOKEN",
            },
            {
                "key": "bearer_kid",
                "description": "Coarse bearer policy key ID",
                "required": True,
                "env_var": "MEMPALACE_BEARER_KID",
            },
            {
                "key": "issuer",
                "description": "Capability issuer",
                "required": True,
                "default": "hermes",
                "env_var": "MEMPALACE_CAPABILITY_ISSUER",
            },
            {
                "key": "audience",
                "description": "Capability audience",
                "required": True,
                "default": "mempalace",
                "env_var": "MEMPALACE_CAPABILITY_AUDIENCE",
            },
            {
                "key": "hmac_secret",
                "description": "Independent 256-bit capability signing secret",
                "required": True,
                "secret": True,
                "env_var": "MEMPALACE_CAPABILITY_HMAC_SECRET_HERMES",
            },
            {
                "key": "policy_fingerprint",
                "description": "Allowed memory access policy fingerprint",
                "required": True,
                "env_var": "MEMPALACE_POLICY_FINGERPRINT",
            },
            {
                "key": "ttl_seconds",
                "description": "One-shot capability lifetime (1-60 seconds)",
                "required": True,
                "default": "30",
                "env_var": "MEMPALACE_CAPABILITY_TTL_SECONDS",
            },
        ]


def register(ctx) -> None:
    ctx.register_memory_provider(MemPalaceMemoryProvider())
