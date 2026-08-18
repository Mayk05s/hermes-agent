"""Profile-scoped bridge to the Telegram family archive backend."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from gateway.session_context import get_session_env
from tools.registry import registry, tool_error


_BACKEND = Path("/home/hermes/hermes-miniapps/scripts/family_archive_mcp.py")
_DB = Path(
    "/home/hermes/.hermes/profiles/family-chat/tenants/telegram_family/family.db"
)
_ARCHIVE_DIR = Path(
    "/home/hermes/.hermes/profiles/family-chat/tenants/telegram_family/archive"
)
_ALLOWED_SOURCE_ROOTS = os.pathsep.join(
    (
        "/home/hermes/.hermes/image_cache",
        str(_ARCHIVE_DIR),
    )
)


def _source_allowed() -> bool:
    return get_session_env("HERMES_SESSION_PROFILE_NAME", "").strip() == "family-chat"


def _backend_available() -> bool:
    return _BACKEND.is_file() and _DB.is_file()


def _call_backend(name: str, args: dict[str, Any]) -> str:
    if not _source_allowed():
        return tool_error("Family archive is available only inside the family-chat profile")
    if not _backend_available():
        return tool_error("Family archive backend is unavailable")

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    env = {
        **os.environ,
        "FAMILY_ARCHIVE_DB": str(_DB),
        "FAMILY_ARCHIVE_DIR": str(_ARCHIVE_DIR),
        "FAMILY_ARCHIVE_ALLOWED_SOURCE_ROOTS": _ALLOWED_SOURCE_ROOTS,
    }
    try:
        completed = subprocess.run(
            ["/usr/bin/python3", str(_BACKEND)],
            input=json.dumps(request, ensure_ascii=False) + "\n",
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return tool_error(f"Family archive backend failed ({exc.__class__.__name__})")
    if completed.returncode != 0:
        return tool_error("Family archive backend exited unsuccessfully")
    try:
        response = json.loads(completed.stdout.strip().splitlines()[-1])
        result = response.get("result") or {}
        data = result.get("structuredContent") or {}
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return tool_error("Family archive backend returned an invalid response")
    if not isinstance(data, dict) or not data.get("ok"):
        message = str(data.get("message") or data.get("error") or "archive operation failed")
        return tool_error(message)
    return json.dumps(data, ensure_ascii=False)


FAMILY_ARCHIVE_SAVE_SCHEMA = {
    "name": "family_archive_save",
    "description": (
        "Save a user-requested booking, ticket, trip confirmation, or family note "
        "to the family-chat archive. For a current image, pass the exact local path "
        "shown in [Image attached at: ...]. The operation copies the image to "
        "permanent storage and verifies the database row."
    ),
    "parameters": {
        "type": "object",
        "required": ["title"],
        "properties": {
            "title": {"type": "string"},
            "date": {"type": "string"},
            "description": {"type": "string"},
            "participants": {"type": "string"},
            "notes": {"type": "string"},
            "source_image_path": {"type": "string"},
        },
        "additionalProperties": False,
    },
}


FAMILY_ARCHIVE_LIST_SCHEMA = {
    "name": "family_archive_list",
    "description": (
        "Read saved bookings, tickets, trip confirmations, and notes from the "
        "family-chat archive."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "additionalProperties": False,
    },
}


registry.register(
    name="family_archive_save",
    toolset="family-archive",
    schema=FAMILY_ARCHIVE_SAVE_SCHEMA,
    handler=lambda args, **_: _call_backend("save_item", args),
    check_fn=_backend_available,
    emoji="🗄️",
)

registry.register(
    name="family_archive_list",
    toolset="family-archive",
    schema=FAMILY_ARCHIVE_LIST_SCHEMA,
    handler=lambda args, **_: _call_backend("list_items", args),
    check_fn=_backend_available,
    emoji="🗄️",
)
