"""Cron access scoping.

Cron jobs created from a gateway chat must keep the same profile, scoped
memory, skill visibility, and toolset ceiling as the chat/topic that created
them.  The LLM is not allowed to pick those fields via a tool call.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

ACCESS_CONTEXT_VERSION = 1


class CronAccessError(ValueError):
    """Raised when a cron job tries to cross its creator access boundary."""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _ordered_unique(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_items = [item.strip() for item in values.split(",")]
    elif isinstance(values, (list, tuple, set)):
        raw_items = [_clean(item) for item in values]
    else:
        raw_items = [_clean(values)]
    result: list[str] = []
    for item in raw_items:
        text = item.strip().strip("/")
        if text and text not in result:
            result.append(text)
    return result


def _skill_aliases(name: str) -> set[str]:
    text = _clean(name).strip("/")
    if not text:
        return set()
    aliases = {text}
    if "/" in text:
        aliases.add(text.rsplit("/", 1)[-1])
    return aliases


def _skill_allowed(name: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    aliases = _skill_aliases(name)
    allowed_aliases: set[str] = set()
    for item in allowed:
        allowed_aliases.update(_skill_aliases(item))
    return bool(aliases & allowed_aliases)


def _canonical_skill(name: str, allowed: list[str]) -> str:
    text = _clean(name).strip("/")
    if not allowed:
        return text
    bare = text.rsplit("/", 1)[-1]
    full_matches = [item for item in allowed if "/" in item and item.rsplit("/", 1)[-1] == bare]
    if "/" not in text and len(full_matches) == 1:
        return full_matches[0]
    if text in allowed:
        return text
    if len(full_matches) == 1:
        return full_matches[0]
    return text


def canonicalize_skills_for_context(skills: Any, access_context: dict[str, Any]) -> list[str]:
    """Return requested skills after enforcing the creator skill boundary."""
    requested = _ordered_unique(skills)
    if not requested:
        return []
    allowed = _ordered_unique(access_context.get("allowed_skills"))
    blocked = [name for name in requested if not _skill_allowed(name, allowed)]
    if blocked:
        raise CronAccessError(
            "Cron job cannot use skill(s) outside the creator chat/topic: "
            + ", ".join(blocked)
        )
    result: list[str] = []
    for name in requested:
        canonical = _canonical_skill(name, allowed)
        if canonical and canonical not in result:
            result.append(canonical)
    return result


def normalize_access_context(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {
        "version": int(raw.get("version") or ACCESS_CONTEXT_VERSION),
        "source": _clean(raw.get("source") or "local"),
        "platform": _clean(raw.get("platform")).lower(),
        "chat_id": _clean(raw.get("chat_id")),
        "thread_id": _clean(raw.get("thread_id")),
        "chat_name": _clean(raw.get("chat_name")),
        "profile": _clean(raw.get("profile") or "default"),
        "scope": _clean(raw.get("scope") or "default"),
        "memory_scope": _clean(raw.get("memory_scope") or raw.get("scope") or "default"),
        "allowed_skills": _ordered_unique(raw.get("allowed_skills")),
        "enabled_toolsets": sorted(_ordered_unique(raw.get("enabled_toolsets"))),
    }


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to read config %s: %s", path, exc)
        return {}


def _default_root() -> Path:
    from hermes_cli.profiles import _get_default_hermes_home

    return _get_default_hermes_home()


def _profile_home(profile: str) -> Path:
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name

    name = normalize_profile_name(profile or "default")
    if name == "default":
        return _default_root()
    return get_profile_dir(name)


def _load_profile_config(profile: str) -> dict[str, Any]:
    return _read_yaml(_profile_home(profile) / "config.yaml")


def _source_obj(
    *,
    platform: str = "",
    chat_id: str = "",
    thread_id: str = "",
    chat_name: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        platform=_clean(platform).lower(),
        chat_id=_clean(chat_id),
        thread_id=_clean(thread_id),
        chat_name=_clean(chat_name),
    )


def _telegram_topic_binding(config: dict[str, Any], source: SimpleNamespace) -> dict[str, Any] | None:
    try:
        chat_id = _clean(getattr(source, "chat_id", ""))
        thread_id = _clean(getattr(source, "thread_id", ""))
        if not chat_id or not thread_id:
            return None
        chat_base = chat_id.split(":", 1)[0]
        extra = ((config.get("telegram") or {}).get("extra") or {})
        for group in extra.get("group_topics") or []:
            if not isinstance(group, dict):
                continue
            if _clean(group.get("chat_id")) != chat_base:
                continue
            for topic in group.get("topics") or []:
                if isinstance(topic, dict) and _clean(topic.get("thread_id")) == thread_id:
                    return topic
    except Exception:
        logger.debug("Could not resolve Telegram topic binding", exc_info=True)
    return None


def _telegram_topic_skills(topic: dict[str, Any] | None) -> list[str]:
    if not isinstance(topic, dict):
        return []
    raw = topic.get("skills")
    if raw is None:
        raw = topic.get("skill")
    return _ordered_unique(raw)


def _telegram_source_allows_homeassistant(config: dict[str, Any], source: SimpleNamespace) -> bool:
    try:
        chat_id = _clean(getattr(source, "chat_id", ""))
        thread_id = _clean(getattr(source, "thread_id", ""))
        chat_base = chat_id.split(":", 1)[0]
        extra = ((config.get("telegram") or {}).get("extra") or {})
        explicit = extra.get("homeassistant_allowed_chats") or extra.get("ha_allowed_chats")
        allowed = set(_ordered_unique(explicit))
        if chat_id in allowed or (thread_id and f"{chat_base}:{thread_id}" in allowed):
            return True
        topic = _telegram_topic_binding(config, source)
        if topic:
            skills = _telegram_topic_skills(topic)
            name = _clean(topic.get("name"))
            return any("telegram_homeassistant" in skill for skill in skills) or name == "homeassistant"
    except Exception:
        logger.debug("Could not resolve Telegram HA tool scope", exc_info=True)
    return False


def _resolve_toolsets(profile_config: dict[str, Any], platform: str, source: SimpleNamespace) -> list[str]:
    platform_key = _clean(platform).lower() or "cli"
    try:
        from hermes_cli.tools_config import _get_platform_tools

        toolsets = set(_get_platform_tools(profile_config or {}, platform_key))
    except Exception as exc:
        logger.warning("Failed to resolve cron creator toolsets for %s: %s", platform_key, exc)
        toolsets = set()
    if platform_key == "telegram":
        if _telegram_source_allows_homeassistant(profile_config or {}, source):
            toolsets.add("homeassistant")
        else:
            toolsets.discard("homeassistant")
    return sorted(toolsets)


def _scope_allowed_skills(profile_config: dict[str, Any], source: SimpleNamespace) -> list[str]:
    try:
        from gateway.profile_scopes import normalize_profile_scopes_config, resolve_scope_for_source

        scope_cfg = normalize_profile_scopes_config((profile_config or {}).get("profile_scopes"))
        scope = resolve_scope_for_source(scope_cfg, source)  # type: ignore[arg-type]
        skill_sets = getattr(scope, "skill_sets", None) or {}
        if isinstance(skill_sets, dict) and _clean(skill_sets.get("mode") or "allow").lower() == "allow":
            names = _ordered_unique(skill_sets.get("names"))
            if names:
                return names
    except Exception:
        logger.debug("Could not resolve profile-scope skill allowlist", exc_info=True)
    if _clean(getattr(source, "platform", "")).lower() == "telegram":
        return _telegram_topic_skills(_telegram_topic_binding(profile_config or {}, source))
    return []


def _profile_scope(profile_config: dict[str, Any], source: SimpleNamespace) -> tuple[str, str]:
    try:
        from gateway.profile_scopes import normalize_profile_scopes_config, resolve_scope_for_source

        scope_cfg = normalize_profile_scopes_config((profile_config or {}).get("profile_scopes"))
        scope = resolve_scope_for_source(scope_cfg, source)  # type: ignore[arg-type]
        scope_name = _clean(getattr(scope, "scope", "")) or "default"
        memory_scope = _clean(getattr(scope, "memory_scope", "")) or scope_name
        return scope_name, memory_scope
    except Exception:
        logger.debug("Could not resolve profile scope", exc_info=True)
        return "default", "default"


def _profile_for_origin(source: SimpleNamespace) -> str:
    try:
        from gateway.profile_routing import normalize_profile_routes_config, resolve_profile_for_source

        root_cfg = _read_yaml(_default_root() / "config.yaml")
        route_cfg = normalize_profile_routes_config(root_cfg.get("profile_routes"))
        return resolve_profile_for_source(route_cfg, source)  # type: ignore[arg-type]
    except Exception:
        logger.debug("Could not resolve profile route for cron origin", exc_info=True)
        return "default"


def current_access_context(*, requested_profile: str | None = None) -> dict[str, Any]:
    """Build a protected access context from the current gateway/CLI session."""
    from gateway.session_context import get_session_env
    from hermes_cli.profiles import get_active_profile_name, normalize_profile_name

    platform = _clean(get_session_env("HERMES_SESSION_PLATFORM", "")).lower()
    chat_id = _clean(get_session_env("HERMES_SESSION_CHAT_ID", ""))
    thread_id = _clean(get_session_env("HERMES_SESSION_THREAD_ID", ""))
    chat_name = _clean(get_session_env("HERMES_SESSION_CHAT_NAME", ""))
    session_profile = _clean(get_session_env("HERMES_SESSION_PROFILE_NAME", ""))
    source = "gateway" if platform and chat_id else "local"

    if session_profile:
        profile = normalize_profile_name(session_profile)
        if requested_profile and normalize_profile_name(requested_profile) != profile:
            raise CronAccessError(
                f"Cron job profile is bound to creator chat profile '{profile}', "
                f"not '{requested_profile}'."
            )
    elif requested_profile:
        profile = normalize_profile_name(requested_profile)
    else:
        profile = normalize_profile_name(get_active_profile_name() or "default")

    source_for_scope = _source_obj(
        platform=platform or "cli",
        chat_id=chat_id,
        thread_id=thread_id,
        chat_name=chat_name,
    )
    profile_config = _load_profile_config(profile)
    session_scope = _clean(get_session_env("HERMES_SESSION_SCOPE_NAME", ""))
    session_memory_scope = _clean(get_session_env("HERMES_SESSION_MEMORY_SCOPE", ""))
    resolved_scope, resolved_memory_scope = _profile_scope(profile_config, source_for_scope)
    scope = session_scope or resolved_scope
    memory_scope = session_memory_scope or resolved_memory_scope
    allowed_skills = _ordered_unique(get_session_env("HERMES_SESSION_ALLOWED_SKILLS", ""))
    if not allowed_skills:
        allowed_skills = _scope_allowed_skills(profile_config, source_for_scope)
    enabled_toolsets = _resolve_toolsets(profile_config, platform or "cli", source_for_scope)

    return normalize_access_context(
        {
            "version": ACCESS_CONTEXT_VERSION,
            "source": source,
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "chat_name": chat_name,
            "profile": profile,
            "scope": scope,
            "memory_scope": memory_scope,
            "allowed_skills": allowed_skills,
            "enabled_toolsets": enabled_toolsets,
        }
    )


def access_context_for_origin(origin: Any, *, fallback_profile: str | None = None) -> dict[str, Any]:
    """Resolve the current route/scope/tool ceiling for a stored job origin."""
    if not isinstance(origin, dict) or not _clean(origin.get("platform")) or not _clean(origin.get("chat_id")):
        from hermes_cli.profiles import get_active_profile_name, normalize_profile_name

        profile = normalize_profile_name(fallback_profile or get_active_profile_name() or "default")
        profile_config = _load_profile_config(profile)
        source = _source_obj(platform="cli")
        return normalize_access_context(
            {
                "source": "local",
                "profile": profile,
                "scope": "default",
                "memory_scope": "default",
                "enabled_toolsets": _resolve_toolsets(profile_config, "cli", source),
            }
        )

    source = _source_obj(
        platform=origin.get("platform"),
        chat_id=origin.get("chat_id"),
        thread_id=origin.get("thread_id"),
        chat_name=origin.get("chat_name"),
    )
    profile = _profile_for_origin(source)
    profile_config = _load_profile_config(profile)
    scope, memory_scope = _profile_scope(profile_config, source)
    return normalize_access_context(
        {
            "version": ACCESS_CONTEXT_VERSION,
            "source": "gateway",
            "platform": source.platform,
            "chat_id": source.chat_id,
            "thread_id": source.thread_id,
            "chat_name": source.chat_name,
            "profile": profile,
            "scope": scope,
            "memory_scope": memory_scope,
            "allowed_skills": _scope_allowed_skills(profile_config, source),
            "enabled_toolsets": _resolve_toolsets(profile_config, source.platform, source),
        }
    )


def origin_from_session() -> dict[str, Any] | None:
    from gateway.session_context import get_session_env

    platform = _clean(get_session_env("HERMES_SESSION_PLATFORM", ""))
    chat_id = _clean(get_session_env("HERMES_SESSION_CHAT_ID", ""))
    if not platform or not chat_id:
        return None
    return {
        "platform": platform,
        "chat_id": chat_id,
        "chat_name": _clean(get_session_env("HERMES_SESSION_CHAT_NAME", "")) or None,
        "thread_id": _clean(get_session_env("HERMES_SESSION_THREAD_ID", "")) or None,
        "profile": _clean(get_session_env("HERMES_SESSION_PROFILE_NAME", "")) or None,
        "scope": _clean(get_session_env("HERMES_SESSION_SCOPE_NAME", "")) or None,
        "memory_scope": _clean(get_session_env("HERMES_SESSION_MEMORY_SCOPE", "")) or None,
    }


def _context_identity(ctx: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    normalized = normalize_access_context(ctx)
    return (
        normalized.get("source", ""),
        normalized.get("platform", ""),
        normalized.get("chat_id", ""),
        normalized.get("thread_id", ""),
        normalized.get("profile", ""),
        normalized.get("scope", ""),
    )


def contexts_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_norm = normalize_access_context(left)
    right_norm = normalize_access_context(right)
    if _context_identity(left_norm) != _context_identity(right_norm):
        return False
    return left_norm.get("memory_scope") == right_norm.get("memory_scope")


def job_access_context(job: dict[str, Any]) -> dict[str, Any]:
    stored = normalize_access_context(job.get("access_context"))
    if stored:
        return stored
    return access_context_for_origin(job.get("origin"), fallback_profile=job.get("profile"))


def ensure_job_manage_allowed(job: dict[str, Any], caller_context: dict[str, Any]) -> None:
    caller = normalize_access_context(caller_context)
    # Local/CLI contexts are administrative for the active profile. Gateway
    # contexts may only see jobs from the exact same chat/topic/profile scope.
    if caller.get("source") != "gateway":
        return
    target = job_access_context(job)
    if not contexts_match(target, caller):
        raise CronAccessError(
            "This cron job belongs to another chat/topic/profile and is not visible here."
        )


def filter_jobs_for_context(jobs: list[dict[str, Any]], caller_context: dict[str, Any]) -> list[dict[str, Any]]:
    caller = normalize_access_context(caller_context)
    if caller.get("source") != "gateway":
        return jobs
    visible = []
    for job in jobs:
        try:
            ensure_job_manage_allowed(job, caller)
        except CronAccessError:
            continue
        visible.append(job)
    return visible


def _require_stored_subset(stored: list[str], current: list[str], label: str) -> None:
    if not stored:
        return
    if not current:
        raise CronAccessError(
            f"Cron job creator {label} could not be resolved for the current route."
        )
    missing = sorted(set(stored) - set(current))
    if missing:
        raise CronAccessError(
            f"Cron job creator {label} is no longer allowed by the current route: "
            + ", ".join(missing)
        )


def _require_stored_skill_subset(stored: list[str], current: list[str]) -> None:
    """Require the original skill ceiling without treating aliases as new skills."""
    if not stored:
        return
    if not current:
        raise CronAccessError(
            "Cron job creator skill allowlist could not be resolved for the current route."
        )
    missing = sorted(skill for skill in stored if not _skill_allowed(skill, current))
    if missing:
        raise CronAccessError(
            "Cron job creator skill allowlist is no longer allowed by the current route: "
            + ", ".join(missing)
        )


def prepare_job_for_run(job: dict[str, Any]) -> dict[str, Any]:
    """Return a job copy with enforced profile/tool/skill/memory context."""
    prepared = dict(job)
    stored = normalize_access_context(job.get("access_context"))
    current = access_context_for_origin(job.get("origin"), fallback_profile=job.get("profile"))
    effective = stored or current

    if stored and not contexts_match(stored, current):
        raise CronAccessError(
            "Cron job access context no longer matches its origin route; "
            "refusing to run with another chat/profile memory boundary."
        )

    _require_stored_skill_subset(
        _ordered_unique(effective.get("allowed_skills")),
        _ordered_unique(current.get("allowed_skills")),
    )
    _require_stored_subset(
        _ordered_unique(effective.get("enabled_toolsets")),
        _ordered_unique(current.get("enabled_toolsets")),
        "toolset allowlist",
    )

    skills = prepared.get("skills")
    if skills is None:
        legacy = prepared.get("skill")
        skills = [legacy] if legacy else []
    prepared["skills"] = canonicalize_skills_for_context(skills, effective)
    prepared["skill"] = prepared["skills"][0] if prepared["skills"] else None
    # Legacy local jobs had no creator chat boundary and may intentionally
    # carry per-job toolsets/profile overrides. Keep that behavior. Gateway
    # jobs (or any new job with access_context) are always bound to the
    # resolved creator context.
    if stored or effective.get("source") == "gateway":
        prepared["profile"] = effective.get("profile") or "default"
        prepared["enabled_toolsets"] = _ordered_unique(effective.get("enabled_toolsets"))
        prepared["access_context"] = effective
    return prepared


def access_context_note(access_context: dict[str, Any]) -> str:
    ctx = normalize_access_context(access_context)
    parts = [f"profile={ctx.get('profile') or 'default'}"]
    if ctx.get("scope"):
        parts.append(f"scope={ctx['scope']}")
    if ctx.get("memory_scope"):
        parts.append(f"memory_scope={ctx['memory_scope']}")
    if ctx.get("platform") and ctx.get("chat_id"):
        target = f"{ctx['platform']}:{ctx['chat_id']}"
        if ctx.get("thread_id"):
            target += f":{ctx['thread_id']}"
        parts.append(f"origin={target}")
    return ", ".join(parts)
