"""Hard postcondition for shopping-list links in gateway replies.

The decision is made exclusively from current-turn structured tool results.  User
text and model wording are intentionally not inputs to target selection.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional


_MUTATION_ACTIONS = frozenset({
    "add-shopping-items",
    "update-shopping-items",
    "move-shopping-items",
    "categorize-shopping-items",
    "compose-shopping-items",
    "dissolve-shopping-composite",
    "complete-shopping-items",
    "mark-shopping-items",
    "delete-shopping-items",
})
_READ_ACTION = "list-shopping-items"
_KNOWN_ACTIONS = _MUTATION_ACTIONS | {_READ_ACTION}
_LIST_LAUNCH_RE = re.compile(
    r"^https://t\.me/TripiooBot\?startapp=shopping_(?:buy|take)_[A-Za-z0-9_-]+$",
    re.IGNORECASE,
)
_UNTRUSTED_RESULT_RE = re.compile(
    r'^\s*<untrusted_tool_result\s+source="(?P<source>[A-Za-z0-9_.:-]+)">'
    r'(?P<body>.*)</untrusted_tool_result>\s*$',
    re.DOTALL,
)


def _current_turn(messages: Iterable[Dict[str, Any]], history_offset: int) -> List[Dict[str, Any]]:
    materialized = list(messages or [])
    if history_offset > 0 and len(materialized) >= history_offset:
        return materialized[history_offset:]
    return materialized


def _tool_names_by_call_id(messages: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id") or call.get("call_id")
            function = call.get("function") or {}
            name = function.get("name") or call.get("name")
            if call_id and name:
                names[str(call_id)] = str(name)
    return names


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    return value


def _normalized_tool_name(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _unwrap_protected_result(value: Any, trusted_tool_name: str) -> Any:
    """Decode one exact gateway-protected tool-result envelope safely."""
    if not isinstance(value, str):
        return value
    match = _UNTRUSTED_RESULT_RE.fullmatch(value)
    if not match:
        return value
    if _normalized_tool_name(match.group("source")) != _normalized_tool_name(trusted_tool_name):
        return None
    return _decode_json(match.group("body").strip())


def _structured_payload(value: Any) -> Optional[Dict[str, Any]]:
    """Unwrap common MCP/tool envelopes without scraping arbitrary prose."""
    value = _decode_json(value)
    if isinstance(value, dict):
        if isinstance(value.get("_miniapp"), dict):
            return value
        # Protected MCP transcripts carry machine-readable data in
        # structuredContent and commonly repeat its JSON string in result.
        for key in ("structuredContent", "result", "data", "output"):
            payload = _structured_payload(value.get(key))
            if payload is not None:
                return payload
        content = value.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
                    payload = _structured_payload(block.get("text"))
                    if payload is not None:
                        return payload
    return None


def _canonical_action(tool_name: str, payload: Dict[str, Any]) -> Optional[str]:
    normalized_tool = _normalized_tool_name(tool_name)
    payload_action = str(payload.get("action") or "").strip().lower().replace("_", "-")
    for action in _KNOWN_ACTIONS:
        # The trusted tool identity must match; payload metadata alone is not enough.
        if normalized_tool == action or normalized_tool.endswith(f"-{action}"):
            return action if not payload_action or payload_action == action else None
    return None


def _result_target(tool_name: str, message: Dict[str, Any]) -> Optional[str]:
    if message.get("is_error") is True or message.get("isError") is True:
        return None
    protected_content = _unwrap_protected_result(message.get("content"), tool_name)
    payload = _structured_payload(protected_content)
    if payload is None or payload.get("ok") is False or payload.get("success") is False or payload.get("error"):
        return None
    action = _canonical_action(tool_name, payload)
    if action is None:
        return None
    miniapp = payload.get("_miniapp")
    if not isinstance(miniapp, dict):
        return None

    if action in _MUTATION_ACTIONS:
        eligible = miniapp.get("changed") is True
    else:
        # A read is eligible only when the service explicitly resolved an open
        # target; generic shopping roots and multi-list launch_urls are rejected.
        eligible = miniapp.get("open") is True
    if not eligible or miniapp.get("launch_urls"):
        return None

    launch_url = str(miniapp.get("launch_url") or "").strip()
    return launch_url if _LIST_LAUNCH_RE.fullmatch(launch_url) else None


def shopping_reply_target_url(
    messages: Iterable[Dict[str, Any]], history_offset: int = 0
) -> Optional[str]:
    """Return one unambiguous current-turn list URL from trusted tool results."""
    current = _current_turn(messages, history_offset)
    names_by_id = _tool_names_by_call_id(current)
    targets: List[str] = []
    for message in current:
        if message.get("role") not in {"tool", "function"}:
            continue
        call_id = str(message.get("tool_call_id") or message.get("call_id") or "")
        tool_name = str(message.get("tool_name") or message.get("name") or names_by_id.get(call_id) or "")
        target = _result_target(tool_name, message)
        if target and target not in targets:
            targets.append(target)
    return targets[0] if len(targets) == 1 else None


def enforce_shopping_reply_contract(
    response_text: str,
    messages: Iterable[Dict[str, Any]],
    history_offset: int = 0,
) -> str:
    """Ensure exactly one named link when structured operations resolve one list."""
    target = shopping_reply_target_url(messages, history_offset)
    if not target:
        return response_text

    text = str(response_text or "")
    escaped = re.escape(target)
    text = re.sub(rf"\[[^\]]*\]\({escaped}\)", "", text)
    text = re.sub(escaped, "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    link = f"[Открыть список]({target})"
    return f"{text}\n\n{link}" if text else link


__all__ = ["enforce_shopping_reply_contract", "shopping_reply_target_url"]
