#!/usr/bin/env python3
"""Explicit cross-chat recall grants for session_search.

Gateway recall is fail-closed to the current chat/topic.  This tool lets the
agent ask the user for a narrow temporary or persistent exception.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_DURATIONS = {"one_turn", "session", "persistent"}
_RUNTIME_LOCK = threading.RLock()
_RUNTIME_GRANTS: Dict[str, List[Dict[str, Any]]] = {}
_CYRILLIC_TRANSLITERATION = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
})
_TRANSFER_INTENT_RE = re.compile(
    r"(?:"
    r"перенес\w*|перевед\w*|перекин\w*|подтян\w*|забер\w*|"
    r"возьм\w*|использ\w*|прочита\w*|вспомн\w*|импорт\w*|"
    r"transfer\w*|import\w*|bring\w*|pull\w*|use\w*|read\w*"
    r")",
    re.IGNORECASE,
)
_CONTEXT_OBJECT_RE = re.compile(
    r"(?:"
    r"контекст\w*|диалог\w*|истори\w*|переписк\w*|сообщени\w*|"
    r"топик\w*|тем\w*|чат\w*|topic\w*|thread\w*|context\w*|"
    r"history\w*|conversation\w*|messages?\w*"
    r")",
    re.IGNORECASE,
)


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _normalize_alias(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean(value)).casefold().replace("ё", "е")
    text = text.translate(_CYRILLIC_TRANSLITERATION)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _alias_similarity(left: Any, right: Any) -> float:
    left_norm = _normalize_alias(left)
    right_norm = _normalize_alias(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    if left_norm in right_norm or right_norm in left_norm:
        shorter = min(len(left_norm), len(right_norm))
        longer = max(len(left_norm), len(right_norm))
        if shorter >= 4:
            return max(0.9, shorter / longer)
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def _norm_filter(raw: Dict[str, Any]) -> Dict[str, str]:
    normalized = {
        "platform": _clean(raw.get("platform")).lower(),
        "chat_id": _clean(raw.get("chat_id")),
        "thread_id": _clean(raw.get("thread_id")),
        "label": _clean(raw.get("label")),
    }
    # Most grants target one concrete topic and predate an explicit mode.  Keep
    # that serialized shape stable, but preserve broader, consented scopes when
    # they are present.
    mode = _clean(raw.get("mode")).lower()
    if mode:
        normalized["mode"] = mode
    profile_name = _clean(raw.get("profile_name"))
    if profile_name:
        normalized["profile_name"] = profile_name
    return normalized


def _filter_key(raw: Dict[str, Any]) -> tuple[str, str, str, str]:
    flt = _norm_filter(raw)
    return (
        flt.get("mode") or "topic",
        flt["platform"],
        flt["chat_id"],
        flt["thread_id"],
    )


def _same_filter(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return _filter_key(left) == _filter_key(right)


def _current_base_filter() -> Optional[Dict[str, str]]:
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return None

    platform = _clean(get_session_env("HERMES_SESSION_PLATFORM", "")).lower()
    chat_id = _clean(get_session_env("HERMES_SESSION_CHAT_ID", ""))
    if not platform or not chat_id or platform in {"local", "cli"}:
        return None
    return {
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": _clean(get_session_env("HERMES_SESSION_THREAD_ID", "")),
        "profile_name": (
            _clean(get_session_env("HERMES_SESSION_PROFILE_NAME", "")) or "default"
        ),
        "label": "current chat/topic",
    }


def _all_topics_target(base: Dict[str, str]) -> Dict[str, str]:
    """Return a consent scope covering sibling topics in this chat/profile."""
    return {
        "mode": "chat",
        "platform": _clean(base.get("platform")).lower(),
        "chat_id": _clean(base.get("chat_id")),
        "thread_id": "",
        "profile_name": _clean(base.get("profile_name")) or "default",
        "label": "all other topics in this chat",
    }


def _is_all_topics_alias(value: Any) -> bool:
    normalized = _normalize_alias(value)
    return normalized in {
        "other topics",
        "all topics",
        "other topics in this chat",
        "all topics in this chat",
        "same chat topics",
        "drugie topiki",
        "vse topiki",
        "ostalnye topiki",
        "drugie temy",
        "vse temy",
    }


def _session_key() -> str:
    try:
        from gateway.session_context import get_session_env
        return _clean(get_session_env("HERMES_SESSION_KEY", ""))
    except Exception:
        return ""


def _message_id() -> str:
    try:
        from gateway.session_context import get_session_env
        return _clean(get_session_env("HERMES_SESSION_MESSAGE_ID", ""))
    except Exception:
        return ""


def _load_active_config() -> tuple[Dict[str, Any], Optional[Path]]:
    """Load the active profile config and return (config, path_if_profile_file)."""
    profile = "default"
    try:
        from gateway.session_context import get_session_env
        profile = _clean(get_session_env("HERMES_SESSION_PROFILE_NAME", "")) or "default"
    except Exception:
        profile = "default"

    if profile == "default":
        from hermes_cli.config import get_config_path, load_config
        return load_config() or {}, get_config_path()

    try:
        import yaml
        from hermes_cli import profiles as profiles_mod

        config_path = profiles_mod.get_profile_dir(profile) / "config.yaml"
        if not config_path.exists():
            return {}, config_path
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}, config_path
    except Exception:
        logger.debug("Failed to load active profile config", exc_info=True)
        return {}, None


def _load_active_raw_config() -> tuple[Dict[str, Any], Optional[Path]]:
    """Load raw config.yaml for minimal persistent grant writes."""
    profile = "default"
    try:
        from gateway.session_context import get_session_env
        profile = _clean(get_session_env("HERMES_SESSION_PROFILE_NAME", "")) or "default"
    except Exception:
        profile = "default"

    if profile == "default":
        from hermes_cli.config import get_config_path, read_raw_config
        return read_raw_config() or {}, get_config_path()

    try:
        import yaml
        from hermes_cli import profiles as profiles_mod

        config_path = profiles_mod.get_profile_dir(profile) / "config.yaml"
        if not config_path.exists():
            return {}, config_path
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}, config_path
    except Exception:
        logger.debug("Failed to load active raw profile config", exc_info=True)
        return {}, None


def _save_active_config(config: Dict[str, Any], config_path: Optional[Path]) -> None:
    if config_path is None:
        raise RuntimeError("active config path is unavailable")
    from utils import atomic_yaml_write
    atomic_yaml_write(Path(config_path), config, sort_keys=False)


def _iter_config_targets(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []

    def add(platform: Any, chat_id: Any, thread_id: Any = "", label: Any = "", aliases: list[str] | None = None):
        platform_s = _clean(platform).lower()
        chat_s = _clean(chat_id)
        if not platform_s or not chat_s:
            return
        alias_values = [_clean(alias) for alias in (aliases or []) if _clean(alias)]
        label_s = _clean(label)
        if label_s:
            alias_values.append(label_s)
        target = {
            "platform": platform_s,
            "chat_id": chat_s,
            "thread_id": _clean(thread_id),
            "label": label_s,
            "aliases": alias_values,
        }
        targets.append(target)

    profile_scopes = (config.get("profile_scopes") or {}).get("scopes") or []
    if isinstance(profile_scopes, list):
        for item in profile_scopes:
            if not isinstance(item, dict) or item.get("enabled", True) is False:
                continue
            aliases = [
                _clean(item.get("id")),
                _clean(item.get("label")),
                _clean(item.get("scope")),
                _clean(item.get("memory_scope")),
            ]
            aliases.extend(_clean(alias) for alias in (item.get("aliases") or []))
            add(item.get("platform"), item.get("chat_id"), item.get("thread_id"), item.get("label") or item.get("id"), aliases)

    routes = (config.get("profile_routes") or {}).get("routes") or []
    if isinstance(routes, list):
        for item in routes:
            if not isinstance(item, dict) or item.get("enabled", True) is False:
                continue
            aliases = [_clean(item.get("id")), _clean(item.get("label")), _clean(item.get("profile"))]
            aliases.extend(_clean(alias) for alias in (item.get("aliases") or []))
            add(item.get("platform"), item.get("chat_id"), item.get("thread_id"), item.get("label") or item.get("id"), aliases)

    telegram = config.get("telegram") or {}
    extra = telegram.get("extra") if isinstance(telegram, dict) else {}
    groups = (extra or {}).get("group_topics") or []
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            chat_id = group.get("chat_id")
            for topic in group.get("topics") or []:
                if not isinstance(topic, dict):
                    continue
                aliases = [_clean(topic.get("name")), _clean(topic.get("skill"))]
                aliases.extend(_clean(alias) for alias in (topic.get("aliases") or []))
                for skill in topic.get("skills") or []:
                    aliases.append(_clean(skill))
                add("telegram", chat_id, topic.get("thread_id"), topic.get("name"), aliases)

    try:
        from gateway.channel_directory import load_directory

        directory = load_directory()
        for platform_name, channels in (directory.get("platforms") or {}).items():
            for channel in channels or []:
                if not isinstance(channel, dict):
                    continue
                entry_id = _clean(channel.get("id"))
                thread_id = _clean(channel.get("thread_id"))
                chat_id = entry_id
                if thread_id and entry_id.endswith(f":{thread_id}"):
                    chat_id = entry_id[: -(len(thread_id) + 1)]
                add(
                    platform_name,
                    chat_id,
                    thread_id,
                    channel.get("name"),
                    [channel.get("name"), entry_id],
                )
    except Exception:
        logger.debug("Failed to load channel-directory recall aliases", exc_info=True)

    return targets


def _parse_explicit_target(target: str) -> Optional[Dict[str, str]]:
    parts = [part.strip() for part in target.split(":")]
    if len(parts) < 2:
        return None
    platform = parts[0].lower()
    chat_id = parts[1]
    thread_id = parts[2] if len(parts) >= 3 else ""
    if not platform or not chat_id:
        return None
    return {
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "label": target,
    }


def _decorate_target_with_known_aliases(target_filter: Dict[str, Any]) -> Dict[str, Any]:
    """Restore a human topic label when the model supplied raw chat/thread ids."""
    try:
        config, _path = _load_active_config()
        matches = [
            item for item in _iter_config_targets(config)
            if _filter_key(item) == _filter_key(target_filter)
        ]
    except Exception:
        logger.debug("Failed to resolve aliases for explicit recall target", exc_info=True)
        return target_filter

    if not matches:
        return target_filter

    aliases: List[str] = []
    for item in matches:
        aliases.extend(_clean(alias) for alias in (item.get("aliases") or []))
        if _clean(item.get("label")):
            aliases.append(_clean(item.get("label")))

    decorated = dict(target_filter)
    preferred = next(
        (_clean(item.get("label")) for item in matches if _clean(item.get("label"))),
        "",
    )
    if preferred:
        decorated["label"] = preferred
    decorated["aliases"] = list(dict.fromkeys(alias for alias in aliases if alias))
    return decorated


def resolve_recall_target(
    target: str = "",
    *,
    platform: str = "",
    chat_id: str = "",
    thread_id: str = "",
) -> Dict[str, str]:
    """Resolve a target alias or explicit platform/chat/thread into a filter."""
    platform = _clean(platform).lower()
    chat_id = _clean(chat_id)
    thread_id = _clean(thread_id)
    target_clean = _clean(target)

    # This is intentionally narrower than profile-wide recall: the user is
    # approving a search across sibling topics of the current chat, not across
    # DMs or other groups that happen to use the same profile.
    base = _current_base_filter() or {}
    if not chat_id and _is_all_topics_alias(target_clean):
        if not base.get("platform") or not base.get("chat_id"):
            raise ValueError("The current gateway chat is unavailable.")
        return _all_topics_target(base)

    if chat_id:
        return _decorate_target_with_known_aliases({
            "platform": platform or _clean(base.get("platform")).lower() or "telegram",
            "chat_id": chat_id,
            "thread_id": thread_id,
            "label": target_clean or f"{platform or base.get('platform') or 'telegram'}:{chat_id}:{thread_id}",
        })

    explicit = _parse_explicit_target(target_clean)
    if explicit:
        return _decorate_target_with_known_aliases(explicit)

    config, _path = _load_active_config()
    candidates = _iter_config_targets(config)
    if base.get("platform") and base.get("chat_id"):
        candidates.sort(key=lambda item: (
            item["platform"] != base["platform"],
            item["chat_id"] != base["chat_id"],
        ))

    scored: List[tuple[float, Dict[str, Any], str]] = []
    for item in candidates:
        aliases = list(item.get("aliases") or [])
        aliases.append(_clean(item.get("label")))
        aliases.append(f"{item['platform']}:{item['chat_id']}:{item['thread_id']}".rstrip(":"))
        best_alias = ""
        best_score = 0.0
        for alias in aliases:
            score = _alias_similarity(target_clean, alias)
            if score > best_score:
                best_score = score
                best_alias = alias
        if best_score:
            same_chat_bonus = (
                0.03
                if item["platform"] == base.get("platform") and item["chat_id"] == base.get("chat_id")
                else 0.0
            )
            scored.append((min(1.0, best_score + same_chat_bonus), item, best_alias))

    scored.sort(key=lambda row: row[0], reverse=True)
    if scored and scored[0][0] >= 0.78:
        top_score, top_item, matched_alias = scored[0]
        competing = [
            row for row in scored[1:]
            if _filter_key(row[1]) != _filter_key(top_item) and row[0] >= top_score - 0.05
        ]
        if not competing:
            return {
                "platform": top_item["platform"],
                "chat_id": top_item["chat_id"],
                "thread_id": top_item["thread_id"],
                "label": top_item.get("label") or target_clean,
                "matched_alias": matched_alias,
            }

    available_same_chat = [
        item.get("label") or f"topic {item.get('thread_id')}"
        for item in candidates
        if item.get("platform") == base.get("platform")
        and item.get("chat_id") == base.get("chat_id")
        and item.get("thread_id")
    ]
    hint = ""
    if available_same_chat:
        names = ", ".join(dict.fromkeys(available_same_chat[:8]))
        hint = f" Known topics in the current chat: {names}."

    raise ValueError(
        "Could not resolve recall target by name. Use a known topic name, "
        "target='platform:chat_id:thread_id', or pass platform/chat_id/thread_id "
        f"explicitly.{hint}"
    )


def _explicit_same_chat_transfer_requested(
    user_request: str,
    base: Dict[str, str],
    target_filter: Dict[str, str],
) -> bool:
    """Treat the user's current transfer request as the approval itself."""
    request = _clean(user_request)
    if not request:
        return False
    if (
        target_filter.get("platform") != base.get("platform")
        or target_filter.get("chat_id") != base.get("chat_id")
        or not target_filter.get("thread_id")
    ):
        return False
    if not _TRANSFER_INTENT_RE.search(request) or not _CONTEXT_OBJECT_RE.search(request):
        return False

    requested_aliases = [
        target_filter.get("label"),
        target_filter.get("matched_alias"),
        target_filter.get("thread_id"),
    ]
    requested_aliases.extend(target_filter.get("aliases") or [])
    request_norm = _normalize_alias(request)
    for alias in requested_aliases:
        alias_norm = _normalize_alias(alias)
        if not alias_norm:
            continue
        if alias_norm in request_norm or _alias_similarity(alias_norm, request_norm) >= 0.72:
            return True
        alias_tokens = alias_norm.split()
        if alias_tokens and all(
            any(SequenceMatcher(None, token, word).ratio() >= 0.82 for word in request_norm.split())
            for token in alias_tokens
        ):
            return True
    return False


def _runtime_grants_for(base_filter: Dict[str, str]) -> List[Dict[str, str]]:
    session_key = _session_key()
    if not session_key:
        return []
    current_message = _message_id()
    with _RUNTIME_LOCK:
        grants = list(_RUNTIME_GRANTS.get(session_key) or [])
        kept: List[Dict[str, Any]] = []
        targets: List[Dict[str, str]] = []
        for grant in grants:
            duration = grant.get("duration")
            if not _same_filter(grant.get("source") or {}, base_filter):
                kept.append(grant)
                continue
            if duration == "one_turn" and grant.get("message_id") != current_message:
                continue
            kept.append(grant)
            target = grant.get("target") or {}
            if target:
                targets.append(_norm_filter(target))
        _RUNTIME_GRANTS[session_key] = kept
        return targets


def _persistent_grants_for(base_filter: Dict[str, str]) -> List[Dict[str, str]]:
    config, _path = _load_active_config()
    grants = ((config.get("recall_access") or {}).get("grants") or [])
    if not isinstance(grants, list):
        return []
    targets: List[Dict[str, str]] = []
    for grant in grants:
        if not isinstance(grant, dict):
            continue
        if not _same_filter(grant.get("source") or {}, base_filter):
            continue
        target = grant.get("target") or {}
        if isinstance(target, dict):
            targets.append(_norm_filter(target))
    return targets


def get_granted_access_filters(base_filter: Dict[str, str]) -> List[Dict[str, str]]:
    """Return extra target filters granted for the active gateway session."""
    if not base_filter:
        return []
    seen = set()
    results: List[Dict[str, str]] = []
    for target in _runtime_grants_for(base_filter) + _persistent_grants_for(base_filter):
        key = _filter_key(target)
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        results.append(target)
    return results


def _add_runtime_grant(source: Dict[str, str], target: Dict[str, str], duration: str, reason: str) -> None:
    session_key = _session_key()
    if not session_key:
        raise RuntimeError("No active gateway session key")
    grant = {
        "source": _norm_filter(source),
        "target": _norm_filter(target),
        "duration": duration,
        "reason": _clean(reason),
        "message_id": _message_id(),
        "created_at": time.time(),
    }
    with _RUNTIME_LOCK:
        grants = _RUNTIME_GRANTS.setdefault(session_key, [])
        grants[:] = [
            item for item in grants
            if not (
                _same_filter(item.get("source") or {}, grant["source"])
                and _same_filter(item.get("target") or {}, grant["target"])
                and item.get("duration") == duration
            )
        ]
        grants.append(grant)


def _add_persistent_grant(source: Dict[str, str], target: Dict[str, str], reason: str) -> None:
    config, path = _load_active_raw_config()
    recall_cfg = config.setdefault("recall_access", {})
    grants = recall_cfg.setdefault("grants", [])
    if not isinstance(grants, list):
        grants = []
        recall_cfg["grants"] = grants

    source_norm = _norm_filter(source)
    target_norm = _norm_filter(target)
    for item in grants:
        if not isinstance(item, dict):
            continue
        if _same_filter(item.get("source") or {}, source_norm) and _same_filter(item.get("target") or {}, target_norm):
            item["reason"] = _clean(reason)
            item["updated_at"] = time.time()
            _save_active_config(config, path)
            return

    grants.append({
        "source": source_norm,
        "target": target_norm,
        "reason": _clean(reason),
        "created_at": time.time(),
    })
    _save_active_config(config, path)


def _choice_to_duration(choice: str, requested: str) -> Optional[str]:
    normalized = _clean(choice).lower()
    if not normalized:
        return None
    if (
        "deny" in normalized
        or "cancel" in normalized
        or "не искать" in normalized
        or "отказать" in normalized
        or "no" == normalized
    ):
        return None
    if "always" in normalized or "persist" in normalized or "всегда" in normalized:
        return "persistent"
    if "session" in normalized or "сесси" in normalized:
        return "session"
    if "once" in normalized or "turn" in normalized or "один раз" in normalized:
        return "one_turn"
    if normalized in {"1", "one", "yes", "y", "ok", "grant"}:
        return requested
    return None


def recall_access_tool(
    target: str = "",
    reason: str = "",
    duration: str = "one_turn",
    platform: str = "",
    chat_id: str = "",
    thread_id: str = "",
    callback: Optional[Callable[[str, list[str]], str]] = None,
    user_request: str = "",
) -> str:
    base = _current_base_filter()
    if not base:
        return json.dumps({
            "success": False,
            "error": "recall_access is only needed in gateway chat contexts.",
        }, ensure_ascii=False)

    duration = _clean(duration) or "one_turn"
    if duration not in _DURATIONS:
        duration = "one_turn"

    try:
        target_filter = resolve_recall_target(
            target,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
        )
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)

    if _same_filter(base, target_filter):
        return json.dumps({
            "success": True,
            "granted": True,
            "duration": "current",
            "target": _norm_filter(target_filter),
            "message": "Target is the current chat/topic; no extra grant was needed.",
        }, ensure_ascii=False)

    explicitly_requested = (
        duration != "persistent"
        and _explicit_same_chat_transfer_requested(user_request, base, target_filter)
    )
    if explicitly_requested:
        try:
            _add_runtime_grant(base, target_filter, duration, _clean(reason) or user_request)
        except Exception as exc:
            return json.dumps({"success": False, "error": f"Could not save grant: {exc}"}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "granted": True,
            "duration": duration,
            "source": _norm_filter(base),
            "target": _norm_filter(target_filter),
            "approval": "explicit_current_user_request",
            "message": (
                "Recall access granted from the user's explicit request. Continue "
                "with session_search now; do not ask the user to confirm again."
            ),
        }, ensure_ascii=False)

    if callback is None:
        return json.dumps({
            "success": False,
            "error": "recall_access requires an interactive user confirmation callback.",
        }, ensure_ascii=False)

    reason_clean = _clean(reason) or "No reason provided"
    target_label = target_filter.get("label") or f"{target_filter['platform']}:{target_filter['chat_id']}:{target_filter['thread_id']}"
    if target_filter.get("mode") == "chat":
        question = (
            "Искать нужную информацию в других топиках этого чата?\n\n"
            "Текущий топик останется отдельным контекстом; разрешение только "
            "временно расширит поиск по общей истории.\n"
            f"Причина: {reason_clean}"
        )
        choices = ["Искать один раз", "На эту сессию", "Разрешать всегда", "Не искать"]
    else:
        question = (
            "Allow this bot to read recall/history from another chat/topic?\n\n"
            f"Current: {base['platform']}:{base['chat_id']}:{base.get('thread_id') or '-'}\n"
            f"Target: {target_label} ({target_filter['platform']}:{target_filter['chat_id']}:{target_filter.get('thread_id') or '-'})\n"
            f"Reason: {reason_clean}"
        )
        choices = ["Grant once", "Grant for session", "Always allow", "Deny"]
    try:
        user_choice = callback(question, choices)
    except Exception as exc:
        return json.dumps({"success": False, "error": f"Approval failed: {exc}"}, ensure_ascii=False)

    approved_duration = _choice_to_duration(user_choice, duration)
    if not approved_duration:
        return json.dumps({
            "success": True,
            "granted": False,
            "target": _norm_filter(target_filter),
            "message": "Recall access denied by the user.",
        }, ensure_ascii=False)

    try:
        if approved_duration == "persistent":
            _add_persistent_grant(base, target_filter, reason_clean)
        else:
            _add_runtime_grant(base, target_filter, approved_duration, reason_clean)
    except Exception as exc:
        return json.dumps({"success": False, "error": f"Could not save grant: {exc}"}, ensure_ascii=False)

    return json.dumps({
        "success": True,
        "granted": True,
        "duration": approved_duration,
        "source": _norm_filter(base),
        "target": _norm_filter(target_filter),
        "message": (
            "Recall access granted. When using this external context, explicitly "
            "tell the user which outside chat/topic was consulted."
        ),
    }, ensure_ascii=False)


def check_recall_access_requirements() -> bool:
    return True


RECALL_ACCESS_SCHEMA = {
    "name": "recall_access",
    "description": (
        "Grant session_search narrow access to another gateway chat/topic. Use "
        "target='other_topics' when a fact was not found in the current topic "
        "and may be in another topic of this same chat; this opens an approval "
        "prompt and, if granted, searches the shared chat history without merging "
        "the topics' live context. If "
        "the user's current message explicitly asks to transfer, pull, read, or "
        "use context from a named topic in the same chat, that request is already "
        "the approval: call this tool immediately and do not ask them to confirm "
        "again. Otherwise this tool opens an approval prompt. Never silently "
        "bypass topic isolation. Durations: one_turn, session, or persistent. "
        "Use session for requests such as 'bring that topic here' so follow-up "
        "turns can keep using the imported context. After using external context, "
        "say which chat/topic was used."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Use 'other_topics' for all sibling topics in the current "
                    "chat/profile, a target alias from config (for example "
                    "'planning' or 'health'), or explicit "
                    "'platform:chat_id:thread_id'."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Why this other chat/topic is needed for the user's request.",
            },
            "duration": {
                "type": "string",
                "enum": ["one_turn", "session", "persistent"],
                "default": "one_turn",
                "description": "Requested grant duration. The user can override this in the approval prompt.",
            },
            "platform": {
                "type": "string",
                "description": "Optional explicit target platform when target alias is not enough.",
            },
            "chat_id": {
                "type": "string",
                "description": "Optional explicit target chat id.",
            },
            "thread_id": {
                "type": "string",
                "description": "Optional explicit target thread/topic id.",
            },
        },
        "required": ["reason"],
    },
}


from tools.registry import registry

registry.register(
    name="recall_access",
    toolset="session_search",
    schema=RECALL_ACCESS_SCHEMA,
    handler=lambda args, **kw: recall_access_tool(
        target=args.get("target", ""),
        reason=args.get("reason", ""),
        duration=args.get("duration", "one_turn"),
        platform=args.get("platform", ""),
        chat_id=args.get("chat_id", ""),
        thread_id=args.get("thread_id", ""),
        callback=kw.get("callback"),
        user_request=kw.get("user_task", ""),
    ),
    check_fn=check_recall_access_requirements,
    emoji="",
)
