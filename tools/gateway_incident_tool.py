"""Gateway-native structured incident registration and owner lookup tools."""
from __future__ import annotations

import json

from gateway.profile_incidents import CATEGORIES, COMPONENTS, STATUSES, explicit_fields
from gateway.session_context import get_session_env
from tools.registry import registry


def _gateway_context_available() -> bool:
    job_id = get_session_env("HERMES_JOB_ID", "")
    profile = get_session_env("HERMES_SESSION_PROFILE_NAME", "")
    return bool(job_id.startswith("gw_") and profile)


def _runner():
    from gateway.run import _gateway_runner_ref

    runner = _gateway_runner_ref()
    if runner is None:
        raise RuntimeError("gateway_runtime_unavailable")
    return runner


async def report_gateway_incident(args: dict, **_kwargs) -> str:
    """Register against runtime-attributed job/profile; caller supplies no routing."""
    allowed = {"category", "component", "status", "code"}
    if set(args) != allowed:
        return json.dumps({"error": "invalid_incident_fields"})
    try:
        result = await _runner()._register_profile_incident(
            source_profile=get_session_env("HERMES_SESSION_PROFILE_NAME", ""),
            job_id=get_session_env("HERMES_JOB_ID", ""),
            fields=explicit_fields(args),
        )
    except (KeyError, PermissionError, ValueError, RuntimeError) as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(
        {
            "acknowledged": True,
            "job_id": result["source_job_id"],
            "incident_ref": result["incident_id"],
            "delivery_status": result["delivery_status"],
        }
    )


async def lookup_gateway_incident(args: dict, **_kwargs) -> str:
    if set(args) != {"job_id"}:
        return json.dumps({"error": "invalid_lookup_fields"})
    try:
        rows = _runner()._owner_incident_lookup(
            job_id=args.get("job_id"),
            profile=get_session_env("HERMES_SESSION_PROFILE_NAME", ""),
            requester=get_session_env("HERMES_SESSION_REQUESTER_USER_ID", ""),
        )
    except (PermissionError, ValueError, RuntimeError) as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps({"job_id": args["job_id"], "incidents": rows})


REPORT_SCHEMA = {
    "name": "report_gateway_incident",
    "description": (
        "Register a sanitized technical incident for the current trusted gateway job. "
        "No destination, narrative, chat data, paths, IDs, or raw errors are accepted."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "category": {"type": "string", "enum": sorted(CATEGORIES)},
            "component": {"type": "string", "enum": sorted(COMPONENTS)},
            "status": {"type": "string", "enum": sorted(STATUSES)},
            "code": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,48}$"},
        },
        "required": ["category", "component", "status", "code"],
    },
}

LOOKUP_SCHEMA = {
    "name": "lookup_gateway_incident",
    "description": "Personal owner-only exact sanitized incident lifecycle lookup by gw_*.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "job_id": {"type": "string", "pattern": "^gw_[0-9a-f]{32}$"},
        },
        "required": ["job_id"],
    },
}

registry.register(
    name="report_gateway_incident",
    toolset="gateway-incidents",
    schema=REPORT_SCHEMA,
    handler=report_gateway_incident,
    check_fn=_gateway_context_available,
    is_async=True,
    description=REPORT_SCHEMA["description"],
    emoji="⚠️",
)
registry.register(
    name="lookup_gateway_incident",
    toolset="gateway-incidents",
    schema=LOOKUP_SCHEMA,
    handler=lookup_gateway_incident,
    check_fn=_gateway_context_available,
    is_async=True,
    description=LOOKUP_SCHEMA["description"],
    emoji="🔎",
)
