"""Strict privacy boundary for cross-profile technical incidents.

Only normalized tokens from this module may cross profile boundaries.  It has no
session/chat/history APIs and deliberately never accepts arbitrary text.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

CATEGORIES = frozenset({"bug", "availability", "data_integrity", "performance", "security"})
COMPONENTS = frozenset({"agent", "gateway", "provider", "routing", "storage", "tool", "delivery", "unknown"})
STATUSES = frozenset({"observed", "intermittent", "blocked", "failed", "exception"})
ORIGINS = frozenset({"explicit", "automatic"})
_GW_RE = re.compile(r"^gw_[0-9a-f]{32}$")
_TOKEN_RE = re.compile(r"[^a-z0-9_.-]+")
_HTTP_STATUS_RE = re.compile(r"\b(?:HTTP\s*)?([45]\d\d)\b", re.IGNORECASE)
_ERROR_CODE_RE = re.compile(
    r"\b(?:code|errno)\s*[=:]?\s*([A-Za-z][A-Za-z0-9_.-]{0,31}|\d{1,6})\b",
    re.IGNORECASE,
)


def normalize_choice(value: Any, *, allowed: frozenset[str], field: str) -> str:
    token = _TOKEN_RE.sub("_", str(value or "").strip().lower()).strip("_.-")
    if token not in allowed:
        raise ValueError(f"invalid_{field}")
    return token


def normalize_code(value: Any) -> str:
    token = _TOKEN_RE.sub("_", str(value or "").strip().lower()).strip("_.-")[:48]
    if not token:
        raise ValueError("invalid_code")
    return token


def normalize_job_id(value: Any) -> str:
    token = str(value or "").strip().lower()
    if not _GW_RE.fullmatch(token):
        raise ValueError("invalid_gateway_job")
    return token


def explicit_fields(args: Mapping[str, Any]) -> dict[str, str]:
    return {
        "category": normalize_choice(args.get("category"), allowed=CATEGORIES, field="category"),
        "component": normalize_choice(args.get("component"), allowed=COMPONENTS, field="component"),
        "incident_status": normalize_choice(args.get("status"), allowed=STATUSES, field="status"),
        "code": normalize_code(args.get("code")),
        "origin": "explicit",
    }


def automatic_fields(
    *,
    agent_result: Optional[Mapping[str, Any]] = None,
    exception: Optional[BaseException] = None,
    retryable_provider_failure: bool = False,
) -> dict[str, str]:
    """Derive only allowlisted tokens; raw errors and tracebacks are discarded."""
    result = agent_result if isinstance(agent_result, Mapping) else {}
    if exception is not None:
        code = normalize_code(type(exception).__name__)
        component = "gateway"
        status = "exception"
    elif result.get("compression_exhausted"):
        code, component, status = "context_exhausted", "agent", "failed"
    elif retryable_provider_failure:
        code, component, status = "provider_unavailable", "provider", "failed"
    else:
        code, component, status = "agent_failed", "agent", "failed"
    # Retain a numeric provider status only; never retain surrounding error text.
    raw = str(result.get("error") or "")
    match = _HTTP_STATUS_RE.search(raw) or _ERROR_CODE_RE.search(raw)
    if match:
        code = normalize_code(f"{code}_{match.group(1)}")
    return {
        "category": "availability" if component == "provider" else "bug",
        "component": component,
        "incident_status": status,
        "code": code,
        "origin": "automatic",
    }


def render_owner_notification(incident: Mapping[str, Any]) -> str:
    """Render only columns from the sanitized incident table."""
    return "\n".join(
        (
            "⚠️ Hermes technical incident",
            f"incident_ref: {incident['incident_id']}",
            f"job_id: {incident['source_job_id']}",
            f"source_profile: {incident['source_profile']}",
            f"category: {incident['category']}",
            f"component: {incident['component']}",
            f"status: {incident['incident_status']}",
            f"code: {incident['code']}",
            "privacy: sanitized lifecycle only; no chat, user, payload, path, or raw error",
        )
    )


def safe_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "incident_id", "source_job_id", "source_profile", "category", "component",
        "incident_status", "code", "origin", "delivery_status", "delivery_attempts",
        "created_at", "updated_at", "first_attempt_at", "delivered_at",
        "next_attempt_at", "last_delivery_code",
    )
    return {key: row.get(key) for key in allowed}
