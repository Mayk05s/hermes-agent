"""Turn-scoped Mini App buttons from executed, successful health-actions calls.

Collect before tool output is truncated or conversation history is compressed.
No model prose, call arguments, historical results, or global/chat state select
the destination. Failed attempts cannot cancel a later successful operation.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from gateway.shopping_reply_contract import (
    _current_turn, _decode_json, _normalized_tool_name,
    _tool_names_by_call_id, _unwrap_protected_result,
)


_SECTIONS = {
    "nutrition_history": (
        "record-nutrition-meal", "replace-nutrition-meals", "list-nutrition-journal",
    ),
    "stats": (
        "get-nutrition-statistics", "get-group-nutrition-statistics",
        "render-group-nutrition-chart", "record-body-weight", "list-body-weight-history",
    ),
    "meds": (
        "find-medication-rules", "upsert-medication-reminder-rule",
        "record-medication-intake", "confirm-medication-intake",
    ),
    "fitness": (
        "create-workout-slots", "materialize-gtg-day", "record-completed-set",
        "record-cardio-activity", "restore-cancelled-workout-slot",
        "repair-workout-history-facts", "move-workout-slot", "cancel-workout-slot",
        "cleanup-cancelled-workout-slots",
    ),
    "planning": (
        "create-planning-task", "list-planning-tasks", "update-planning-task",
        "delete-planning-task",
    ),
    "shopping": (
        "list-shopping-items", "show-shopping-list", "rollover-shopping-list", "refine-shopping-item", "add-shopping-items", "delete-shopping-items",
        "set-shopping-items-bought", "categorize-shopping-items",
        "compose-shopping-items", "dissolve-shopping-composite", "move-shopping-items",
    ),
    "wishlist": (
        "add-wishlist-items", "list-wishlist-items", "update-wishlist-items",
        "archive-wishlist-items",
    ),
    "menu": ("add-menu-entries", "list-menu-entries", "replace-menu-entries"),
}
_ACTIONS = {action: section for section, actions in _SECTIONS.items() for action in actions}
_PARAM = re.compile(r"[A-Za-z0-9_-]{1,64}")
_URL = re.compile(r"https://t\.me/TripiooBot\?startapp=([A-Za-z0-9_-]+)", re.I)


def _payload(value: Any, depth: int = 0) -> dict | None:
    if depth > 8:
        return None
    value = _decode_json(value)
    if not isinstance(value, dict):
        return None
    if value.get("isError") or value.get("is_error") or value.get("error"):
        return None
    if "ok" in value:
        return value if value["ok"] is True and value.get("success") is not False else None
    for key in ("structuredContent", "result", "data", "output"):
        found = _payload(value.get(key), depth + 1)
        if found is not None:
            return found
    for block in value.get("content") or []:
        if isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
            found = _payload(block.get("text"), depth + 1)
            if found is not None:
                return found
    return None


def _operation(tool_name: str, result: Any, is_error: bool = False) -> dict | None:
    name = _normalized_tool_name(tool_name)
    prefix = "mcp-health-actions-"
    action = name[len(prefix):] if name.startswith(prefix) else ""
    section = _ACTIONS.get(action)
    if not section:
        return None
    record = {"section": section, "mutation": action != "show-shopping-list" and not action.startswith(("list-", "get-", "find-")), "display": action == "show-shopping-list", "params": []}
    if is_error:
        return record
    data = _payload(_unwrap_protected_result(result, tool_name))
    if data is None or data.get("action") != action:
        return record
    record["success"] = True
    if section == "shopping":
        rows = data.get("items") or data.get("movedItems") or data.get("deletedItems") or []
        if not record["mutation"]:
            miniapp=data.get("_miniapp") or {}
            target=miniapp.get("start_param") or ""
            if not target:
                match=_URL.fullmatch(str(miniapp.get("launch_url") or ""))
                target=match[1] if match else ""
            record["exact_read"]=bool(re.fullmatch(r"shopping_(buy|take)_[A-Za-z0-9_-]+",target)
                and not miniapp.get("launch_urls") and len(miniapp.get("start_params") or [])<=1)
            generation=re.fullmatch(r"shopping_(?:buy|take)_g([0-9a-f]{32})(?:_c[0-9]+)?",target)
            if generation:
                rows=[row for row in rows if isinstance(row,dict)
                      and str(row.get("liveListId") or row.get("live_list_id") or "").replace("-", "")==generation[1]]
            elif data.get("listName") or data.get("activeListName"):
                name=data.get("listName") or data.get("activeListName")
                rows=[row for row in rows if isinstance(row,dict) and (row.get("listName") or row.get("list_name"))==name]
        row_ids = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            value = row.get("shoppingId") or row.get("id")
            match = re.fullmatch(r"(?:shopping-)?([1-9]\d*)", str(value or ""))
            if match:
                row_ids.append(int(match[1]))
        if action == "rollover-shopping-list" and all(re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",str(data.get(k) or "")) for k in ("oldListId","liveListId")):
            record["rollover"] = {"oldListId":data["oldListId"],"liveListId":data["liveListId"]}
        record["row_ids"] = sorted(set(row_ids))
        record["list_ids"] = sorted({str(row.get("liveListId") or row.get("live_list_id")) for row in rows
            if isinstance(row,dict) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",str(row.get("liveListId") or row.get("live_list_id") or ""))})
    miniapp = data.get("_miniapp") or {}
    if not isinstance(miniapp, dict):
        return record
    params = []
    for url in [miniapp.get("launch_url"), *(miniapp.get("launch_urls") or [])]:
        match = _URL.fullmatch(str(url or ""))
        if match:
            params.append(match[1])
    params.extend([miniapp.get("start_param"), *(miniapp.get("start_params") or [])])
    if section == "shopping":
        params = [p for p in params if isinstance(p, str) and _PARAM.fullmatch(p)
                  and (p == "shopping" or re.fullmatch(r"shopping_(buy|take)(?:_[A-Za-z0-9_-]+)?", p))]
    else:
        # The executed action supplies the fallback even for reads whose older
        # MCP versions do not emit _miniapp. Keep navigation within its section.
        allowed = {
            "fitness": {"fitness", "fitness_day", "fitness_week", "fitness_history", "fitness_planning"},
            "menu": {"menu", "dishes"},
        }.get(section, {section})
        params = [p for p in params if isinstance(p, str) and _PARAM.fullmatch(p)
                  and (p in allowed or (section in {"planning", "menu"}
                       and re.sub(r"_c[1-9]\d{0,18}$", "", p) in allowed))]
    record["params"] = list(dict.fromkeys(params or [section]))
    return record


class MiniappReplyCollector:
    """One collector per gateway invocation, shared only with its children."""

    def __init__(self):
        self._lock = threading.Lock()
        self._records: list[dict] = []

    def record(self, tool_name: str, result: Any, *, is_error: bool = False):
        operation = _operation(tool_name, result, is_error)
        if operation is not None:
            with self._lock:
                self._records.append(operation)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(record, params=list(record["params"])) for record in self._records]


def miniapp_reply_records(messages, history_offset: int = 0) -> list[dict]:
    current = _current_turn(messages, history_offset)
    names = _tool_names_by_call_id(current)
    collector = MiniappReplyCollector()
    for message in current:
        if message.get("role") not in {"tool", "function"}:
            continue
        call_id = str(message.get("tool_call_id") or message.get("call_id") or "")
        if call_id in names:
            collector.record(names[call_id], message.get("content"),
                             is_error=bool(message.get("is_error") or message.get("isError")))
    return collector.snapshot()


def miniapp_reply_params(records: list[dict]) -> list[str]:
    params = []
    shopping_writes = []
    shopping_read = []
    for record in records:
        targets = record.get("params") or []
        if record.get("section") == "shopping":
            if record.get("mutation"):
                shopping_writes.extend(targets)
            elif targets:
                shopping_read = targets
        else:
            params.extend(targets)
    shopping = shopping_writes or shopping_read
    exact = [p for p in shopping if re.fullmatch(r"shopping_(buy|take)_.+", p)]
    params.extend(exact or shopping)
    return list(dict.fromkeys(params))


def apply_miniapp_reply_contract(response: str, records: list[dict]) -> tuple[str, list[str]]:
    """Return prose and mandatory destinations as separate transport values."""
    if not records:
        return response, []
    sections = {record["section"] for record in records}

    def keep_link(match):
        url_match = _URL.search(match.group(0))
        param = url_match[1] if url_match else ""
        section = "shopping" if param.startswith("shopping") else param.split("_")[0]
        if section == "nutrition":
            section = "nutrition_history"
        if section == "dishes":
            section = "menu"
        return "" if section in sections else match.group(0)

    text = re.sub(r"\[[^\]]*\]\(" + _URL.pattern + r"\)", keep_link, str(response or ""), flags=re.I)
    text = _URL.sub(keep_link, text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), miniapp_reply_params(records)
