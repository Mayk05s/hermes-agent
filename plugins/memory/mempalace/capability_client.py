"""Strict MemPalace capability signer and HTTP transport.

This module implements the frozen Hermes/MemPalace v1 wire contract.  It has
no local-storage fallback: every content operation requires an immutable
``MemoryAccessContext`` issued by the authenticated gateway path.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import time
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from gateway.memory_access import MemoryAccessContext


CAPABILITY_HEADER = "X-MemPalace-Capability"
REQUEST_ID_HEADER = "X-MemPalace-Request-ID"
_SAFE_DOTTED = re.compile(r"^[A-Za-z0-9._-]+$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_JTI = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_DOC_ID = re.compile(r"^[0-9a-f]{8,64}$")
_PRINCIPAL_ID = re.compile(r"^p_[a-z2-7]{16,52}$")
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


# Frozen by tests/fixtures/memory_capability_mcp_surface_v1.json. These are
# semantic model arguments only; the bridge injects all ACL and provenance.
TOOL_ARGUMENT_SPECS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "get_document": (frozenset({"doc_id"}), frozenset({"doc_id"})),
    "list_wings": (frozenset(), frozenset()),
    "mempalace_add_drawer": (
        frozenset({"content", "room"}),
        frozenset({"content", "room"}),
    ),
    "mempalace_delete_drawer": (
        frozenset({"drawer_id"}),
        frozenset({"drawer_id"}),
    ),
    "mempalace_get_drawer": (
        frozenset({"drawer_id"}),
        frozenset({"drawer_id"}),
    ),
    "mempalace_get_taxonomy": (frozenset(), frozenset()),
    "mempalace_kg_add": (
        frozenset({"object", "predicate", "subject", "valid_from"}),
        frozenset({"object", "predicate", "subject"}),
    ),
    "mempalace_kg_invalidate": (
        frozenset({"ended", "object", "predicate", "subject"}),
        frozenset({"object", "predicate", "subject"}),
    ),
    "mempalace_kg_query": (
        frozenset({"as_of", "direction", "entity"}),
        frozenset({"entity"}),
    ),
    "mempalace_kg_stats": (frozenset(), frozenset()),
    "mempalace_kg_timeline": (frozenset({"entity"}), frozenset()),
    "mempalace_list_drawers": (
        frozenset({"limit", "offset", "room"}),
        frozenset(),
    ),
    "mempalace_list_rooms": (frozenset(), frozenset()),
    "mempalace_list_wings": (frozenset(), frozenset()),
    "mempalace_search": (
        frozenset(
            {"context", "limit", "max_distance", "min_similarity", "query", "room"}
        ),
        frozenset({"query"}),
    ),
    "mempalace_update_drawer": (
        frozenset({"content", "drawer_id", "room"}),
        frozenset({"drawer_id"}),
    ),
    "put_page": (
        frozenset({"content", "slug"}),
        frozenset({"content", "slug"}),
    ),
    "query": (frozenset({"limit", "query"}), frozenset({"query"})),
    "upload_document": (
        frozenset({"content_base64", "filename"}),
        frozenset({"content_base64", "filename"}),
    ),
}

TOOL_OPERATIONS: dict[str, str] = {
    "get_document": "read",
    "list_wings": "read",
    "mempalace_add_drawer": "write",
    "mempalace_delete_drawer": "write",
    "mempalace_get_drawer": "read",
    "mempalace_get_taxonomy": "read",
    "mempalace_kg_add": "write",
    "mempalace_kg_invalidate": "write",
    "mempalace_kg_query": "read",
    "mempalace_kg_stats": "read",
    "mempalace_kg_timeline": "read",
    "mempalace_list_drawers": "read",
    "mempalace_list_rooms": "read",
    "mempalace_list_wings": "read",
    "mempalace_search": "read",
    "mempalace_update_drawer": "write",
    "put_page": "write",
    "query": "read",
    "upload_document": "write",
}

RESERVED_MODEL_ARGUMENTS = frozenset(
    {
        "_access_provenance",
        "_authorized_wing",
        "actor_ref",
        "added_by",
        "capability",
        "grants",
        "metadata",
        "palace",
        "palace_path",
        "principal_id",
        "read_wings",
        "source",
        "source_closet",
        "source_file",
        "source_ref",
        "source_wing",
        "sub",
        "target_wing",
        "wing",
        "wings",
        "write_wing",
    }
)

STRING_MAX_UTF8_BYTES: dict[str, int] = {
    "as_of": 64,
    "content": 100_000,
    "content_base64": 22_020_096,
    "context": 4096,
    "direction": 16,
    "doc_id": 128,
    "drawer_id": 128,
    "ended": 64,
    "entity": 128,
    "filename": 255,
    "object": 128,
    "predicate": 128,
    "query": 250,
    "room": 128,
    "slug": 120,
    "subject": 128,
    "valid_from": 64,
}


class MemPalaceClientError(RuntimeError):
    """A classified error whose text never contains credentials or content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class MemPalaceClientConfig:
    base_url: str
    bearer_token: str = field(repr=False)
    bearer_kid: str
    issuer: str
    audience: str
    hmac_secret: str = field(repr=False)
    policy_fingerprint: str
    ttl_seconds: int = 30
    timeout_seconds: float = 8.0


@dataclass(frozen=True)
class MemPalaceHTTPResponse:
    status: int
    body: bytes
    content_type: str = ""


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize the exact JSON bytes used for body and capability payload."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise MemPalaceClientError("invalid_json") from exc


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _required_env(environ: Mapping[str, str], name: str, *, maximum: int) -> str:
    value = environ.get(name)
    if not isinstance(value, str) or not value or value != value.strip():
        raise MemPalaceClientError("client_config_unavailable")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise MemPalaceClientError("client_config_unavailable") from exc
    if len(encoded) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MemPalaceClientError("client_config_unavailable")
    return value


def _safe_dotted_env(environ: Mapping[str, str], name: str) -> str:
    value = _required_env(environ, name, maximum=64)
    if _SAFE_DOTTED.fullmatch(value) is None:
        raise MemPalaceClientError("client_config_unavailable")
    return value


def _validated_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MemPalaceClientError("client_config_unavailable") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is not None and not 1 <= port <= 65535
    ):
        raise MemPalaceClientError("client_config_unavailable")
    if parsed.scheme == "http":
        hostname = parsed.hostname.lower()
        is_loopback = hostname == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise MemPalaceClientError("insecure_base_url")
    return value.rstrip("/")


def load_client_config(
    environ: Optional[Mapping[str, str]] = None,
) -> MemPalaceClientConfig:
    source = os.environ if environ is None else environ
    base_url = _validated_base_url(
        _required_env(source, "MEMPALACE_BASE_URL", maximum=2048)
    )
    bearer = _required_env(source, "MEMPALACE_BEARER_TOKEN", maximum=4096)
    secret = _required_env(
        source,
        "MEMPALACE_CAPABILITY_HMAC_SECRET_HERMES",
        maximum=4096,
    )
    if len(bearer.encode("utf-8")) < 32 or len(secret.encode("utf-8")) < 32:
        raise MemPalaceClientError("weak_client_credential")
    if hmac.compare_digest(bearer.encode("utf-8"), secret.encode("utf-8")):
        raise MemPalaceClientError("credential_classes_not_independent")
    fingerprint = _required_env(
        source,
        "MEMPALACE_POLICY_FINGERPRINT",
        maximum=71,
    )
    if _FINGERPRINT.fullmatch(fingerprint) is None:
        raise MemPalaceClientError("client_config_unavailable")
    ttl_raw = source.get("MEMPALACE_CAPABILITY_TTL_SECONDS", "30")
    if not isinstance(ttl_raw, str) or not ttl_raw.isascii() or not ttl_raw.isdigit():
        raise MemPalaceClientError("client_config_unavailable")
    ttl = int(ttl_raw)
    if not 1 <= ttl <= 60:
        raise MemPalaceClientError("client_config_unavailable")
    return MemPalaceClientConfig(
        base_url=base_url,
        bearer_token=bearer,
        bearer_kid=_safe_dotted_env(source, "MEMPALACE_BEARER_KID"),
        issuer=_safe_dotted_env(source, "MEMPALACE_CAPABILITY_ISSUER"),
        audience=_safe_dotted_env(source, "MEMPALACE_CAPABILITY_AUDIENCE"),
        hmac_secret=secret,
        policy_fingerprint=fingerprint,
        ttl_seconds=ttl,
    )


def _new_jti() -> str:
    return "r_" + _base64url(secrets.token_bytes(18))


def _new_rpc_id() -> str:
    return "rpc_" + _base64url(secrets.token_bytes(12))


def _operation_scope(
    context: MemoryAccessContext,
    operation: str,
) -> tuple[list[str], Optional[str], Optional[str]]:
    if operation == "control":
        return [], None, None
    if operation == "read":
        if not context.read_wings:
            raise MemPalaceClientError("memory_access_denied")
        return list(context.read_wings), None, None
    if operation == "write":
        if context.write_wing is None or context.source_ref is None:
            raise MemPalaceClientError("memory_access_denied")
        return [], context.write_wing, context.source_ref
    raise MemPalaceClientError("invalid_operation")


def _validate_context_boundary(context: MemoryAccessContext) -> None:
    """Reject structurally impossible ACLs before signing any request."""
    destination = context.destination
    if (
        not isinstance(context.read_wings, tuple)
        or any(not isinstance(wing, str) or not wing for wing in context.read_wings)
        or context.write_wing is not None
        and (not isinstance(context.write_wing, str) or not context.write_wing)
    ):
        raise MemPalaceClientError("memory_access_denied")
    if destination.kind == "group":
        prefix = (
            f"group.{destination.platform}.{destination.chat_id}."
            f"{destination.thread_id}."
        )
        if (
            context.grants
            or len(context.read_wings) != 1
            or any(not wing.startswith(prefix) for wing in context.read_wings)
            or context.write_wing is not None
            and context.write_wing != context.read_wings[0]
            or any(wing.startswith("private.") for wing in context.read_wings)
        ):
            raise MemPalaceClientError("memory_access_denied")
        return

    if destination.kind != "dm" or context.principal_id is None:
        raise MemPalaceClientError("memory_access_denied")
    if _PRINCIPAL_ID.fullmatch(context.principal_id) is None:
        raise MemPalaceClientError("memory_access_denied")
    if context.authorization_subject != f"principal:{context.principal_id}":
        raise MemPalaceClientError("memory_access_denied")
    private_prefix = f"private.{context.principal_id}."
    if any(
        wing.startswith("private.") and not wing.startswith(private_prefix)
        for wing in context.read_wings
    ):
        raise MemPalaceClientError("memory_access_denied")
    if context.write_wing is not None and not context.write_wing.startswith(
        private_prefix
    ):
        raise MemPalaceClientError("memory_access_denied")
    if any(grant.type != "read_from_group" for grant in context.grants):
        raise MemPalaceClientError("memory_access_denied")
    private_wings = [
        wing for wing in context.read_wings if wing.startswith("private.")
    ]
    group_wings = [wing for wing in context.read_wings if wing.startswith("group.")]
    if len(private_wings) + len(group_wings) != len(context.read_wings):
        raise MemPalaceClientError("memory_access_denied")
    if len(group_wings) != len(context.grants):
        raise MemPalaceClientError("memory_access_denied")


def build_capability(
    context: MemoryAccessContext,
    config: MemPalaceClientConfig,
    *,
    operation: str,
    method: str,
    raw_target: str,
    body: bytes,
    now: Optional[int] = None,
    jti: Optional[str] = None,
) -> tuple[str, str, dict[str, Any]]:
    """Build one exact, request-bound capability from trusted context only."""
    if type(context) is not MemoryAccessContext or context.version != 1:
        raise MemPalaceClientError("memory_access_denied")
    _validate_context_boundary(context)
    if context.policy_fingerprint != config.policy_fingerprint:
        raise MemPalaceClientError("stale_policy_fingerprint")
    if not isinstance(body, bytes):
        raise MemPalaceClientError("invalid_body")
    if method not in {"GET", "POST"}:
        raise MemPalaceClientError("invalid_method")
    if method == "GET" and body:
        raise MemPalaceClientError("invalid_body")
    if (
        not isinstance(raw_target, str)
        or not raw_target.startswith("/")
        or not 1 <= len(raw_target.encode("utf-8")) <= 2048
        or "#" in raw_target
        or any(ord(char) < 32 or ord(char) == 127 for char in raw_target)
    ):
        raise MemPalaceClientError("invalid_target")
    issued_at = int(time.time()) if now is None else now
    if isinstance(issued_at, bool) or not isinstance(issued_at, int) or issued_at < 0:
        raise MemPalaceClientError("invalid_clock")
    request_id = _new_jti() if jti is None else jti
    if not isinstance(request_id, str) or _JTI.fullmatch(request_id) is None:
        raise MemPalaceClientError("invalid_jti")
    read_wings, write_wing, source_ref = _operation_scope(context, operation)
    claims = {
        "v": 1,
        "iss": config.issuer,
        "aud": config.audience,
        "sub": context.authorization_subject,
        "principal_id": context.principal_id,
        "actor_ref": context.actor_ref,
        "source_ref": source_ref,
        "destination": {
            "kind": context.destination.kind,
            "platform": context.destination.platform,
            "chat_id": context.destination.chat_id,
            "thread_id": context.destination.thread_id,
        },
        "read_wings": read_wings,
        "write_wing": write_wing,
        "grants": [
            {"id": grant.id, "type": grant.type}
            for grant in sorted(context.grants, key=lambda grant: (grant.type, grant.id))
        ],
        "policy_fingerprint": context.policy_fingerprint,
        "iat": issued_at,
        "exp": issued_at + config.ttl_seconds,
        "jti": request_id,
        "bearer_kid": config.bearer_kid,
        "method": method,
        "path": raw_target,
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }
    payload_segment = _base64url(canonical_json_bytes(claims))
    signing_input = f"v1.{payload_segment}".encode("ascii")
    signature = hmac.new(
        config.hmac_secret.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    header = f"v1.{payload_segment}.{_base64url(signature)}"
    if len(header.encode("ascii")) > 16 * 1024:
        raise MemPalaceClientError("capability_too_large")
    return header, request_id, claims


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise MemPalaceClientError("response_too_large")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MemPalaceClientError("invalid_response")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise MemPalaceClientError("invalid_response")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=no_duplicates,
            parse_constant=reject_constant,
        )
    except MemPalaceClientError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MemPalaceClientError("invalid_response") from exc
    if not isinstance(value, dict):
        raise MemPalaceClientError("invalid_response")
    return value


def _default_transport(
    method: str,
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
) -> MemPalaceHTTPResponse:
    try:
        import requests

        with requests.request(
            method,
            url,
            data=body if method == "POST" else None,
            headers=dict(headers),
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        ) as response:
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    raise MemPalaceClientError("response_too_large")
                chunks.append(chunk)
            return MemPalaceHTTPResponse(
                status=response.status_code,
                body=b"".join(chunks),
                content_type=str(response.headers.get("Content-Type") or ""),
            )
    except MemPalaceClientError:
        raise
    except Exception as exc:
        raise MemPalaceClientError("network_error") from exc


class MemPalaceCapabilityClient:
    """One-shot request client; it never caches a capability or ACL context."""

    def __init__(
        self,
        *,
        environ: Optional[Mapping[str, str]] = None,
        clock: Callable[[], float] = time.time,
        jti_factory: Callable[[], str] = _new_jti,
        rpc_id_factory: Callable[[], str] = _new_rpc_id,
        transport: Callable[
            [str, str, bytes, Mapping[str, str], float], MemPalaceHTTPResponse
        ] = _default_transport,
    ) -> None:
        self._environ = environ
        self._clock = clock
        self._jti_factory = jti_factory
        self._rpc_id_factory = rpc_id_factory
        self._transport = transport

    def configured(self) -> bool:
        try:
            load_client_config(self._environ)
            return True
        except MemPalaceClientError:
            return False

    def request(
        self,
        context: MemoryAccessContext,
        *,
        operation: str,
        method: str,
        raw_target: str,
        body: bytes = b"",
        expect_json: bool = True,
    ) -> Any:
        config = load_client_config(self._environ)
        capability, request_id, _claims = build_capability(
            context,
            config,
            operation=operation,
            method=method,
            raw_target=raw_target,
            body=body,
            now=int(self._clock()),
            jti=self._jti_factory(),
        )
        headers = {
            "Authorization": f"Bearer {config.bearer_token}",
            CAPABILITY_HEADER: capability,
            REQUEST_ID_HEADER: request_id,
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"
        try:
            response = self._transport(
                method,
                config.base_url + raw_target,
                body,
                headers,
                config.timeout_seconds,
            )
        except MemPalaceClientError:
            raise
        except Exception as exc:
            raise MemPalaceClientError("network_error") from exc
        if (
            not isinstance(response, MemPalaceHTTPResponse)
            or type(response.status) is not int
            or not isinstance(response.body, bytes)
        ):
            raise MemPalaceClientError("invalid_transport_response")
        if not 200 <= response.status < 300:
            raise MemPalaceClientError("request_denied")
        if expect_json:
            return _strict_json_object(response.body)
        if len(response.body) > _MAX_RESPONSE_BYTES:
            raise MemPalaceClientError("response_too_large")
        return response.body

    def mcp_call(
        self,
        context: MemoryAccessContext,
        remote_tool: str,
        arguments: dict[str, Any],
        *,
        operation: str,
    ) -> Any:
        rpc_id = self._rpc_id_factory()
        body = canonical_json_bytes(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "tools/call",
                "params": {"name": remote_tool, "arguments": arguments},
            }
        )
        response = self.request(
            context,
            operation=operation,
            method="POST",
            raw_target="/mcp",
            body=body,
        )
        if response.get("jsonrpc") != "2.0" or response.get("id") != rpc_id:
            raise MemPalaceClientError("invalid_rpc_response")
        if "error" in response or "result" not in response:
            raise MemPalaceClientError("rpc_denied")
        result = response["result"]
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            raise MemPalaceClientError("invalid_rpc_response")
        is_error = result.get("isError", False)
        if type(is_error) is not bool:
            raise MemPalaceClientError("invalid_rpc_response")
        if is_error:
            raise MemPalaceClientError("rpc_tool_error")
        return result

    def ingest(
        self,
        context: MemoryAccessContext,
        messages: list[dict[str, str]],
        *,
        kind: str = "chunk",
    ) -> dict[str, Any]:
        if kind not in {"chunk", "summary"}:
            raise MemPalaceClientError("invalid_ingest")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 1000:
            raise MemPalaceClientError("invalid_ingest")
        normalized = []
        for item in messages:
            if not isinstance(item, dict) or set(item) != {"sender", "content"}:
                raise MemPalaceClientError("invalid_ingest")
            sender = item.get("sender")
            content = item.get("content")
            if (
                not isinstance(sender, str)
                or len(sender.encode("utf-8")) > 256
                or not isinstance(content, str)
                or not content
                or len(content.encode("utf-8")) > 200_000
            ):
                raise MemPalaceClientError("invalid_ingest")
            normalized.append({"sender": sender, "content": content})
        body = canonical_json_bytes({"messages": normalized, "kind": kind})
        if len(body) > 5 * 1024 * 1024:
            raise MemPalaceClientError("invalid_ingest")
        response = self.request(
            context,
            operation="write",
            method="POST",
            raw_target="/ingest",
            body=body,
        )
        if response != {"queued": len(normalized)}:
            raise MemPalaceClientError("invalid_ingest_response")
        return response


def validate_tool_arguments(tool_name: str, args: Any) -> dict[str, Any]:
    """Validate the exact capability-safe model argument surface."""
    if not isinstance(args, dict):
        raise MemPalaceClientError("invalid_tool_arguments")
    if tool_name not in TOOL_ARGUMENT_SPECS:
        raise MemPalaceClientError("unknown_tool")
    if any(not isinstance(key, str) for key in args):
        raise MemPalaceClientError("invalid_tool_arguments")
    if RESERVED_MODEL_ARGUMENTS & set(args):
        raise MemPalaceClientError("invalid_tool_arguments")
    allowed, required = TOOL_ARGUMENT_SPECS[tool_name]
    if not required <= set(args) or not set(args) <= allowed:
        raise MemPalaceClientError("invalid_tool_arguments")
    out = dict(args)
    for key, value in out.items():
        if key in {"limit", "offset"}:
            if isinstance(value, bool) or not isinstance(value, int):
                raise MemPalaceClientError("invalid_tool_arguments")
            if key == "limit" and not 1 <= value <= 50:
                raise MemPalaceClientError("invalid_tool_arguments")
            if key == "offset" and not 0 <= value <= 49:
                raise MemPalaceClientError("invalid_tool_arguments")
        elif key in {"max_distance", "min_similarity"}:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise MemPalaceClientError("invalid_tool_arguments")
            if key == "max_distance" and not 0 <= value <= 2:
                raise MemPalaceClientError("invalid_tool_arguments")
            if key == "min_similarity" and not -1 <= value <= 1:
                raise MemPalaceClientError("invalid_tool_arguments")
        elif (
            not isinstance(value, str)
            or len(value.encode("utf-8")) > STRING_MAX_UTF8_BYTES[key]
        ):
            raise MemPalaceClientError("invalid_tool_arguments")
    for key in required:
        if not out[key]:
            raise MemPalaceClientError("invalid_tool_arguments")
    if tool_name == "mempalace_list_drawers":
        if out.get("limit", 20) + out.get("offset", 0) > 50:
            raise MemPalaceClientError("invalid_tool_arguments")
    if tool_name == "mempalace_update_drawer" and not (
        "content" in out or "room" in out
    ):
        raise MemPalaceClientError("invalid_tool_arguments")
    if tool_name == "get_document" and _DOC_ID.fullmatch(out["doc_id"]) is None:
        raise MemPalaceClientError("invalid_tool_arguments")
    if tool_name == "upload_document":
        filename = out["filename"]
        encoded = out["content_base64"]
        if "\x00" in filename or os.path.basename(filename) != filename:
            raise MemPalaceClientError("invalid_tool_arguments")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise MemPalaceClientError("invalid_tool_arguments") from exc
        if (
            not decoded
            or len(decoded) > 15 * 1024 * 1024
            or base64.b64encode(decoded).decode("ascii") != encoded
        ):
            raise MemPalaceClientError("invalid_tool_arguments")
    return out
