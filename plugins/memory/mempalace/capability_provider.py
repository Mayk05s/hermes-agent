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
        self._automatic_config = ""
        self._bound_evidence_json = None
        self._bound_evidence_batch = ()

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
            self._bound_evidence_json = None
            self._bound_evidence_batch = ()

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
            context = self._context()
            if self._inactive or not self._client.configured():
                return ""
        except MemPalaceClientError:
            return ""
        if not context.read_wings:
            return (
                "Durable memory is disabled for this unregistered chat topic. "
                "Do not claim to have searched or saved memory; ask the administrator "
                "to enroll this exact topic. Do not use legacy profile memory."
            )
        if context.destination_kind == "group" and context.write_wing is None:
            if any(g.type == "specialist_nutrition" for g in context.grants):
                return (
                    "Verified participant nutrition memory is read-only for this turn. "
                    "Search is restricted to this participant's private nutrition wing. "
                    "No authenticated typed fact evidence is available: do not claim "
                    "to save media, generated transcripts, tools, or model conclusions. "
                    "Every write and sync attempt is denied; legacy file memory is unavailable."
                )
            return (
                "MemPalace group memory is read-only for this explicit disclosure "
                "turn. Search is allowed only in the current group wing. Every "
                "memory write, update, delete, ingestion, or sync attempt is denied."
            )
        return (
            "MemPalace scoped memory is available. Use its search and read tools "
            "only when durable context would help. Access is fixed by the current "
            "verified chat; tool arguments cannot select a user or memory zone."
            " For newly learned facts use fact search/prefetch; the knowledge graph is historical "
            "structured context and is not a complete index of new facts."
            " When the user explicitly asks to remember or save information, "
            "use the scoped write tools and acknowledge storage only after a "
            "successful result. " + (
                "Useful facts from authenticated incoming messages are extracted in the background; "
                "queue acceptance is not proof of storage. For an explicit save, call a scoped write "
                "tool and report success only when committed=true and facts is positive. "
                "Credentials cannot be stored."
                if self._automatic_config else "Automatic background fact extraction is not enabled."
            )
        )

    def seal_authenticated_identity(self, identity):
        from gateway.memory_ingress import AuthenticatedMemoryIdentity
        from .fact_pipeline import sealed_admission, _context_dict
        if not self._automatic_config or type(identity) is not AuthenticatedMemoryIdentity:
            return None
        context = self._context()
        if context.grants or context.actor_ref == "unknown":
            return None
        return sealed_admission(self._automatic_config, {
            "kind": "authenticated_identity_v1", "source": identity.source.to_dict(),
            "config_path": identity.source_config, "context": _context_dict(context)})

    def verify_authenticated_identity(self, receipt):
        import hmac
        from .fact_pipeline import sealed_admission, _context_dict
        if not self._automatic_config or not isinstance(receipt, dict):
            return None
        packet = receipt.get("evidence")
        if not isinstance(packet, dict) or receipt.get("config") != self._automatic_config:
            return None
        expected = sealed_admission(self._automatic_config, packet)
        if not hmac.compare_digest(str(receipt.get("mac", "")), expected["mac"]):
            return None
        if packet.get("context") != _context_dict(self._context()):
            return None
        if packet.get("kind") == "authenticated_identity_v1" or packet.get("purpose") == "normal":
            return packet
        return None

    def on_authenticated_event(self, evidence) -> None:
        from gateway.memory_ingress import AuthenticatedMemoryInput
        from .fact_pipeline import FactOutbox, canonical, evidence_for, sealed_admission
        if isinstance(evidence, tuple):
            admissions, packets = [], []
            anchor = self._context()
            for original in evidence:
                if type(original) is not AuthenticatedMemoryInput:
                    continue
                # Every constituent retains its own event ID and current route.
                # A merged model prompt is never itself an extraction source.
                import yaml
                from pathlib import Path
                from gateway.memory_routing import resolve_memory_provider_route
                config = yaml.safe_load(Path(original.source_config).read_text()) or {}
                route = resolve_memory_provider_route(original.source, config, request_authorized=True,
                                                      principal_verified=anchor.principal_id is not None)
                if any(g.type == "specialist_nutrition" for g in anchor.grants):
                    from gateway.memory_specialist import resolve_nutrition_route
                    route = resolve_nutrition_route(original.source, config, parent_route=route)
                from dataclasses import replace
                if route is None or replace(route.context, source_ref=anchor.source_ref, grants=anchor.grants) != anchor or tuple(g.type for g in route.context.grants) != tuple(g.type for g in anchor.grants):
                    continue
                self.set_access_context(route.context)
                try:
                    admission = self.on_authenticated_event(original)
                    if admission:
                        admissions.append(admission)
                        packets.append(canonical(admission["evidence"]).decode())
                except Exception as exc:
                    logger.warning("Automatic memory constituent excluded (%s)", _safe_error_code(exc))
            self.set_access_context(anchor)
            with self._context_lock:
                self._bound_evidence_batch = tuple(packets)
                self._bound_evidence_json = packets[0] if packets else None
            return admissions
        if not self._automatic_config or type(evidence) is not AuthenticatedMemoryInput:
            return
        context = self._context()
        if not context.write_wing or any(g.type == "disclose_private_to_group" for g in context.grants):
            return
        purpose = "nutrition" if any(g.type == "specialist_nutrition" for g in context.grants) else "normal"
        packet = evidence_for(context, evidence.source, evidence.text, received_at=evidence.received_at,
                              config_path=evidence.source_config, purpose=purpose)
        admission = sealed_admission(self._automatic_config, packet)
        with self._context_lock:
            self._bound_evidence_json = canonical(packet).decode()
        try:
            outbox = FactOutbox(self._automatic_config)
            try:
                outbox._current_route(packet)
                outbox.enqueue(packet, admission=admission)
            finally:
                outbox.close()
        except Exception as exc:
            logger.warning("Automatic memory ingress deferred (%s)", _safe_error_code(exc))
        return admission

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._inactive or not isinstance(query, str) or not query.strip():
            return ""
        bounded = query.strip().encode("utf-8")[:2048].decode("utf-8", errors="ignore").strip()
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

    def private_disclosure(self, query: str) -> str:
        """Perform the one allowed bounded private recall for a group turn.

        This method is gateway-only: it is not present in ``TOOL_SCHEMAS`` or
        ``_REMOTE_TOOLS``, so the conversation model cannot invoke it or alter
        its ACL arguments.
        """
        if self._inactive or not isinstance(query, str):
            raise MemPalaceClientError("memory_access_denied")
        bounded = query.strip().encode("utf-8")[:2048].decode(
            "utf-8", errors="ignore"
        ).strip()
        if not bounded:
            raise MemPalaceClientError("invalid_tool_arguments")
        args = validate_tool_arguments(
            "mempalace_search",
            {"query": bounded, "limit": 5},
        )
        result = self._client.mcp_call(
            self._context(),
            "mempalace_search",
            args,
            operation="disclose",
        )
        return _result_text(result, maximum=16_000)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        # Read-only disclosure turns must remain non-persistent even if normal
        # sync is activated in a future rollout. The underlying ingest client
        # independently requires a write wing as a second enforcement layer.
        try:
            if self._context().write_wing is None:
                return None
        except MemPalaceClientError:
            return None
        # The original input and ACL were frozen at authenticated ingress.
        # user_content, assistant_content and transcript text are NOT evidence.
        with self._context_lock:
            packet = self._bound_evidence_json
            packets = self._bound_evidence_batch or ((packet,) if packet else ())
        if self._automatic_config and packet:
            from .fact_pipeline import FactOutbox
            outbox = FactOutbox(self._automatic_config)
            try:
                for original in packets:
                    outbox.enqueue(json.loads(original))
            finally:
                outbox.close()
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
            if operation == "write":
                from .fact_pipeline import detected_secret
                if any(detected_secret(value) for value in validated.values() if isinstance(value, str)):
                    return json.dumps({"success": False, "committed": False, "error": "Credentials cannot be stored in memory"})
            with self._context_lock:
                packet = self._bound_evidence_json
                packets = self._bound_evidence_batch or ((packet,) if packet else ())
            if operation == "write" and self._automatic_config and not packet:
                return json.dumps({"success": False, "committed": False, "error": "Original authenticated evidence unavailable"})
            if operation == "write" and self._automatic_config and tool_name not in {"mempalace_add_drawer", "put_page", "mempalace_kg_add"}:
                return json.dumps({"success": False, "committed": False, "error": "Use an original explicit correction or forgetting request"})
            if self._automatic_config and packet and tool_name in {"mempalace_add_drawer", "put_page", "mempalace_kg_add"}:
                # Explicit save and automatic accumulation have one source-bound
                # commit path. Untrusted model paraphrases cannot create a second
                # copy or introduce a fact absent from the original message.
                from .fact_pipeline import FactOutbox
                outbox = FactOutbox(self._automatic_config)
                try:
                    results = []
                    for original in packets:
                        receipt = outbox.enqueue(json.loads(original))
                        results.append(outbox.process_one(receipt["job_id"]))
                    if len(results) == 1:
                        return json.dumps(results[0])
                    return json.dumps({"status": "done" if all(r.get("status") == "done" for r in results) else "pending",
                                       "committed": all(r.get("status") == "done" and (r.get("committed") is True or r.get("facts", 0) == 0) for r in results) and any(r.get("committed") is True for r in results),
                                       "partial_committed": any(r.get("committed") is True for r in results),
                                       "facts": sum(r.get("facts", 0) for r in results), "events": results})
                finally:
                    outbox.close()
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

    def handle_builtin_memory(self, args):
        context = self._context()
        if not any(g.type == "specialist_nutrition" for g in context.grants):
            return None
        from .fact_pipeline import detected_secret
        if any(detected_secret(v) for v in args.values() if isinstance(v, str)):
            return json.dumps({"success": False, "committed": False, "error": "Credentials cannot be stored in memory"})
        if args.get("action") not in {"add", "replace", "remove"} or args.get("target", "user") not in {"memory", "user"}:
            return json.dumps({"success": False, "error": "Unsupported participant memory action"})
        with self._context_lock:
            bound = self._bound_evidence_json
        if not bound or not self._automatic_config:
            return json.dumps({"success": False, "committed": False, "error": "Original participant evidence unavailable"})
        result = json.loads(self.handle_tool_call("put_page", {"slug": "current-participant-request", "content": args.get("content") or "Retract the supported original fact"}))
        result["success"] = result.get("committed") is True and result.get("facts", 0) > 0
        return json.dumps(result)

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
