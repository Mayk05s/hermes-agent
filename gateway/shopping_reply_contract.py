"""Hard postcondition for shopping-list actions in gateway replies.

The decision is made exclusively from current-turn structured tool results. User
text and model wording are intentionally not inputs to target selection.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


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
_LIST_LAUNCH_FIND_RE = re.compile(
    r"https://t\.me/TripiooBot\?startapp=shopping_(?:buy|take)_[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
_UNTRUSTED_RESULT_RE = re.compile(
    r'^\s*<untrusted_tool_result\s+source="(?P<source>[A-Za-z0-9_.:-]+)">'
    r'(?P<body>.*)</untrusted_tool_result>\s*$',
    re.DOTALL,
)


def _current_turn(messages: Iterable[Dict[str, Any]], history_offset: int) -> List[Dict[str, Any]]:
    materialized = list(messages or [])
    if history_offset < 0 or history_offset > len(materialized):
        return []
    return materialized[history_offset:]


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
        if normalized_tool == action or normalized_tool.endswith(f"-{action}"):
            return action if not payload_action or payload_action == action else None
    return None


def _result_decision(
    tool_name: str, message: Dict[str, Any]
) -> Tuple[bool, Optional[str], bool]:
    """Return (eligible_operation, unique_target, ambiguity_veto)."""
    if message.get("is_error") is True or message.get("isError") is True:
        return False, None, False
    protected_content = _unwrap_protected_result(message.get("content"), tool_name)
    payload = _structured_payload(protected_content)
    if payload is None or payload.get("ok") is False or payload.get("success") is False or payload.get("error"):
        return False, None, False
    action = _canonical_action(tool_name, payload)
    if action is None:
        return False, None, False
    miniapp = payload.get("_miniapp")
    if not isinstance(miniapp, dict):
        return False, None, False

    if action in _MUTATION_ACTIONS:
        eligible = miniapp.get("changed") is True
    else:
        eligible = miniapp.get("open") is True
    if not eligible:
        return False, None, False

    launch_urls = miniapp.get("launch_urls")
    if isinstance(launch_urls, list) and launch_urls:
        # An explicitly multi-list result vetoes targets from all other calls.
        return True, None, True

    launch_url = str(miniapp.get("launch_url") or "").strip()
    if not _LIST_LAUNCH_RE.fullmatch(launch_url):
        return True, None, True
    return True, launch_url, False


def _shopping_reply_resolution(
    messages: Iterable[Dict[str, Any]], history_offset: int = 0
) -> Tuple[bool, Optional[str]]:
    """Return whether this turn had an eligible operation and its sole target."""
    current = _current_turn(messages, history_offset)
    names_by_id = _tool_names_by_call_id(current)
    targets: List[str] = []
    had_eligible_operation = False
    for message in current:
        if message.get("role") not in {"tool", "function"}:
            continue
        call_id = str(message.get("tool_call_id") or message.get("call_id") or "")
        if call_id:
            # Trust the assistant tool_call binding, never a result-authored name.
            tool_name = names_by_id.get(call_id, "")
        else:
            tool_name = str(message.get("tool_name") or message.get("name") or "")
        eligible, target, veto = _result_decision(tool_name, message)
        had_eligible_operation = had_eligible_operation or eligible
        if veto:
            return True, None
        if target and target not in targets:
            targets.append(target)
    return had_eligible_operation, targets[0] if len(targets) == 1 else None


def shopping_reply_target_url(
    messages: Iterable[Dict[str, Any]], history_offset: int = 0
) -> Optional[str]:
    """Return one unambiguous current-turn list URL from trusted tool results."""
    return _shopping_reply_resolution(messages, history_offset)[1]


def enforce_shopping_reply_contract(
    response_text: str,
    messages: Iterable[Dict[str, Any]],
    history_offset: int = 0,
) -> str:
    """Strip model-authored deep links; Telegram renders the trusted button."""
    had_operation, _target = _shopping_reply_resolution(messages, history_offset)
    if not had_operation:
        return response_text

    text = str(response_text or "")
    text = re.sub(
        rf"\[[^\]]*\]\({_LIST_LAUNCH_FIND_RE.pattern}\)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = _LIST_LAUNCH_FIND_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


__all__ = ["enforce_shopping_reply_contract", "shopping_reply_target_url"]
