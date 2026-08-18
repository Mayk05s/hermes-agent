"""Semantic routing for durable gateway jobs.

The transport layer durably records every inbound message as an inbox event.
This module decides whether that event changes an already-active unit of work
or starts an independent job.  Routing is deliberately semantic: arrival time
alone is never used as evidence that two messages belong together.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobRouteDecision:
    action: str
    job_id: Optional[str] = None
    confidence: float = 0.0
    reason: str = ""
    parent_job_id: Optional[str] = None


_CONTINUATION_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"и\b|а\s+ещ[её]\b|ещ[её]\b|также\b|добав(?:ь|ьте)\b|"
    r"не\s+забуд(?:ь|ьте)\b|учт(?:и|ите)\b|исправ(?:ь|ьте)\b|"
    r"продолж(?:и|ите)\b|кстати\b|только\b|"
    r"and\b|also\b|add\b|don't\s+forget\b|do\s+not\s+forget\b|"
    r"continue\b|include\b|actually\b"
    r")",
    re.IGNORECASE,
)


def _candidate_payload(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for job in candidates:
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        try:
            source = json.loads(str(job.get("source_json") or "{}"))
            if not isinstance(source, dict):
                source = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            source = {}
        result.append(
            {
                "job_id": job_id,
                "status": str(job.get("status") or ""),
                "request": str(job.get("request_text") or "")[-1000:],
                "summary": str(job.get("routing_summary") or "")[-400:],
                "result": str(job.get("result_text") or "")[-1200:],
                "input_version": int(job.get("input_version") or 1),
                "requester": {
                    "user_id": str(source.get("user_id") or ""),
                    "user_name": str(source.get("user_name") or ""),
                },
            }
        )
    return result


def _extract_json_object(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


async def _classify_with_llm(
    message: str,
    candidates: list[dict[str, Any]],
    recent_jobs: list[dict[str, Any]],
    sender: dict[str, str],
    *,
    message_type: str = "text",
    media_count: int = 0,
) -> JobRouteDecision:
    from agent.auxiliary_client import async_call_llm

    candidate_ids = {str(item["job_id"]) for item in candidates}
    recent_ids = {str(item["job_id"]) for item in recent_jobs}
    response = await async_call_llm(
        task="job_routing",
        messages=[
            {
                "role": "system",
                "content": (
                    "You route a new chat message to active AI jobs. Decide whether "
                    "the message changes/continues exactly one active job or starts "
                    "independent work. Use meaning, referents, goals and requested "
                    "outcome; never use timing alone. Scope additions such as 'and "
                    "don't forget the recipe' attach to the relevant active job so "
                    "one final answer includes both requests. An unrelated request "
                    "must be a new job and may run in parallel. Return JSON only: "
                    "{\"action\":\"attach\"|\"new_job\",\"job_id\":string|null,"
                    "\"parent_job_id\":string|null,\"confidence\":0..1,"
                    "\"reason\":string}. Choose attach only "
                    "when the target is clearly one of the supplied active jobs. "
                    "For new_job, parent_job_id may identify one recent completed "
                    "job only when its public conversation context is genuinely "
                    "needed; use null for unrelated work. A different sender is "
                    "evidence of independent work unless they explicitly reply to "
                    "or clearly amend the existing task. A media attachment belongs "
                    "to the new message and is primary evidence. Never parent a new "
                    "attachment to a completed job merely because its short caption "
                    "says save/add/this; require an explicit reply or a clear textual "
                    "reference to that completed job."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "new_message": message,
                        "message_type": message_type,
                        "media_count": media_count,
                        "has_media": media_count > 0,
                        "sender": sender,
                        "active_jobs": candidates,
                        "recent_completed_jobs": recent_jobs,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        temperature=0,
        max_tokens=250,
        timeout=8,
    )
    choice = getattr(response, "choices", [None])[0]
    content = getattr(getattr(choice, "message", None), "content", "")
    payload = _extract_json_object(content)
    action = str(payload.get("action") or "").strip().lower()
    job_id = str(payload.get("job_id") or "").strip() or None
    try:
        confidence = max(0.0, min(1.0, float(payload.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(payload.get("reason") or "semantic router")[:500]
    parent_job_id = str(payload.get("parent_job_id") or "").strip() or None
    if action == "attach" and job_id in candidate_ids and confidence >= 0.55:
        return JobRouteDecision("attach", job_id, confidence, reason)
    if action in {"attach", "new_job"}:
        return JobRouteDecision(
            "new_job",
            None,
            confidence,
            reason or "independent work",
            parent_job_id=(parent_job_id if parent_job_id in recent_ids else None),
        )
    raise ValueError("semantic router returned no valid action")


async def decide_job_route(
    *,
    message: str,
    active_jobs: Iterable[dict[str, Any]],
    recent_jobs: Iterable[dict[str, Any]] = (),
    replied_job_id: Optional[str] = None,
    sender_user_id: Optional[str] = None,
    sender_user_name: Optional[str] = None,
    message_type: str = "text",
    media_count: int = 0,
) -> JobRouteDecision:
    """Return a semantic routing decision for one durable inbox event."""
    candidates = _candidate_payload(active_jobs)
    recent = _candidate_payload(recent_jobs)
    candidate_ids = {str(item["job_id"]) for item in candidates}
    recent_ids = {str(item["job_id"]) for item in recent}
    sender = {
        "user_id": str(sender_user_id or ""),
        "user_name": str(sender_user_name or ""),
    }
    if replied_job_id and replied_job_id in candidate_ids:
        return JobRouteDecision(
            "attach",
            replied_job_id,
            1.0,
            "message replies to output from the active job",
        )
    if replied_job_id and replied_job_id in recent_ids:
        return JobRouteDecision(
            "new_job",
            confidence=1.0,
            reason="message replies to a completed job",
            parent_job_id=replied_job_id,
        )
    # A fresh attachment is self-contained evidence. When no job is currently
    # active, a terse caption such as "save" or "this one" must not let the
    # semantic router borrow an unrelated completed job merely because its
    # text happens to be the closest referent. Explicit Telegram replies were
    # handled above and remain valid continuations.
    if int(media_count or 0) > 0 and not candidates:
        return JobRouteDecision(
            "new_job",
            confidence=1.0,
            reason="new media attachment is the primary context",
        )
    if not candidates and not recent:
        return JobRouteDecision("new_job", confidence=1.0, reason="no active jobs")

    try:
        return await _classify_with_llm(
            str(message or ""),
            candidates,
            recent,
            sender,
            message_type=str(message_type or "text"),
            media_count=max(0, int(media_count or 0)),
        )
    except Exception as exc:
        logger.warning("Semantic job routing failed; applying conservative fallback: %s", exc)

    # Availability fallback, not the primary router. It handles explicit
    # anaphoric scope updates when exactly one target exists; otherwise it
    # preserves isolation by starting independent work.
    candidate_sender_id = str(
        (candidates[0].get("requester") or {}).get("user_id") or ""
    ) if len(candidates) == 1 else ""
    same_or_unknown_sender = (
        not sender["user_id"]
        or not candidate_sender_id
        or sender["user_id"] == candidate_sender_id
    )
    if (
        len(candidates) == 1
        and same_or_unknown_sender
        and _CONTINUATION_PREFIX_RE.search(str(message or ""))
    ):
        return JobRouteDecision(
            "attach",
            str(candidates[0]["job_id"]),
            0.7,
            "single active job and explicit continuation wording",
        )
    if not candidates and len(recent) == 1 and _CONTINUATION_PREFIX_RE.search(
        str(message or "")
    ):
        return JobRouteDecision(
            "new_job",
            confidence=0.7,
            reason="explicit continuation of the only recent completed job",
            parent_job_id=str(recent[0]["job_id"]),
        )
    return JobRouteDecision(
        "new_job",
        confidence=0.5,
        reason="router unavailable and continuation was not unambiguous",
    )
