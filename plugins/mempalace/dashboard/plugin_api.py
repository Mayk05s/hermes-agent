"""MemPalace dashboard plugin API."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from hermes_cli import mempalace


router = APIRouter()

_JOB_EXECUTOR = ThreadPoolExecutor(max_workers=3)
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_MAX_JOBS = 40
_AUTO_RESUME_STARTED: dict[str, float] = {}
_AUTO_TICK_INTERVAL_SECONDS = 60
_AUTO_JOB_THROTTLE_SECONDS = 300
_AUTO_VALIDATION_INTERVAL_SECONDS = 3 * 24 * 60 * 60
_AUTO_VALIDATION_RETRY_SECONDS = 12 * 60 * 60
_AUTO_WORKER_LOCK = threading.Lock()
_AUTO_WORKER_STARTED = False
_AUTO_WORKER_STATE: dict[str, object] = {
    "started": False,
    "started_at": "",
    "last_tick_at": "",
    "next_tick_at": "",
    "last_action": "",
    "last_error": "",
    "interval_seconds": _AUTO_TICK_INTERVAL_SECONDS,
    "throttle_seconds": _AUTO_JOB_THROTTLE_SECONDS,
    "validation_interval_seconds": _AUTO_VALIDATION_INTERVAL_SECONDS,
    "validation_retry_seconds": _AUTO_VALIDATION_RETRY_SECONDS,
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _job_snapshot(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _public_job(_JOBS.get(job_id) or {})
    if not job:
        raise KeyError(job_id)
    return job


def _public_job(job: dict) -> dict:
    return {key: value for key, value in dict(job).items() if not str(key).startswith("_")}


def _set_job(job_id: str, updates: dict) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise KeyError(job_id)
        job.update(updates)
        if len(_JOBS) > _MAX_JOBS:
            removable = [
                key for key, value in _JOBS.items()
                if value.get("status") in {"done", "error"}
            ]
            for key in removable[: max(0, len(_JOBS) - _MAX_JOBS)]:
                _JOBS.pop(key, None)
        return _public_job(job)


def _cleanup_zombie_jobs() -> None:
    finished_at = _now()
    with _JOBS_LOCK:
        for job in _JOBS.values():
            if job.get("status") not in {"queued", "running"}:
                continue
            future = job.get("_future")
            if future is None or not future.done():
                continue
            error = ""
            try:
                exc = future.exception()
                error = str(exc or "")
            except Exception as exc:
                error = str(exc)
            status = "error" if error else "done"
            job.update(
                {
                    "status": status,
                    "finished_at": job.get("finished_at") or finished_at,
                    "message": "Error" if error else "Done",
                    "error": error,
                    "last_event": {
                        "at": finished_at,
                        "status": status,
                        "message": "Cleaned up completed worker state",
                        "error": error,
                    },
                }
            )


def _set_auto_worker_state(updates: dict) -> None:
    with _AUTO_WORKER_LOCK:
        _AUTO_WORKER_STATE.update(updates)


def _auto_worker_snapshot() -> dict:
    with _AUTO_WORKER_LOCK:
        return dict(_AUTO_WORKER_STATE)


def _active_profile_jobs() -> set[str]:
    _cleanup_zombie_jobs()
    with _JOBS_LOCK:
        return {
            str(job.get("profile"))
            for job in _JOBS.values()
            if job.get("status") in {"queued", "running"} and not job.get("all_profiles")
        }


def _has_active_all_profiles_job() -> bool:
    _cleanup_zombie_jobs()
    with _JOBS_LOCK:
        return any(
            job.get("status") in {"queued", "running"} and job.get("all_profiles")
            for job in _JOBS.values()
        )


def _recently_started(key: str, now: float) -> bool:
    last_started = _AUTO_RESUME_STARTED.get(key, 0)
    return bool(last_started and now - last_started < _AUTO_JOB_THROTTLE_SECONDS)


def _recently_started_for(key: str, now: float, seconds: int) -> bool:
    last_started = _AUTO_RESUME_STARTED.get(key, 0)
    return bool(last_started and now - last_started < seconds)


def _iso_age_seconds(value: object, now: float) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, now - dt.timestamp())


def _last_event(status: dict) -> dict:
    events = status.get("events") if isinstance(status, dict) else []
    if isinstance(events, list) and events:
        last = events[-1]
        return dict(last) if isinstance(last, dict) else {}
    return {}


def _status_with_last_event(status: dict) -> dict:
    enriched = dict(status)
    enriched["last_event"] = _last_event(enriched)
    return enriched


def _last_validation_age_seconds(status: dict, now: float) -> float | None:
    events = status.get("events")
    if not isinstance(events, list):
        return None
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        message = str(event.get("message") or "").lower()
        is_validator = (
            event.get("model_task") == mempalace.LLM_VALIDATOR_TASK
            or "validation" in message
            or "validator" in message
        )
        if is_validator:
            return _iso_age_seconds(event.get("at"), now)
    return None


def _validation_due(status: dict, now: float) -> bool:
    if (
        int(status.get("pending_messages") or 0) > 0
        or bool(status.get("running", False))
        or bool(status.get("paused", False))
        or bool(status.get("stale", False))
    ):
        return False
    if not status.get("palaces"):
        return False
    extracted = status.get("last_finished_at") or (status.get("last_batch") or {}).get("finished_at")
    if not extracted:
        events = status.get("events")
        if isinstance(events, list):
            extracted = next(
                (
                    event.get("at")
                    for event in reversed(events)
                    if isinstance(event, dict)
                    and event.get("model_task") == mempalace.LLM_CONSOLIDATOR_TASK
                    and event.get("status") == "success"
                ),
                "",
            )
    if not extracted:
        return False
    validation_age = _last_validation_age_seconds(status, now)
    return validation_age is None or validation_age >= _AUTO_VALIDATION_INTERVAL_SECONDS


def _start_job(kind: str, payload: dict, target) -> dict:
    job_id = uuid.uuid4().hex[:12]
    model_task = mempalace.LLM_VALIDATOR_TASK if kind.startswith("validate") else mempalace.LLM_CONSOLIDATOR_TASK
    model_config = mempalace.llm_task_model_config(model_task)
    created_at = _now()
    job = {
        "id": job_id,
        "kind": kind,
        "status": "queued",
        "created_at": created_at,
        "started_at": "",
        "finished_at": "",
        "updated_at": created_at,
        "profile": payload.get("profile", "default"),
        "all_profiles": bool(payload.get("all_profiles", False)),
        "message": "Queued",
        "progress": {},
        "model": model_config.get("label", "auto / main model"),
        "model_task": model_task,
        "reason": payload.get("reason", ""),
        "result": None,
        "error": "",
        "last_event": {"at": created_at, "status": "queued", "message": "Queued"},
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    def runner():
        started_at = _now()
        _set_job(
            job_id,
            {
                "status": "running",
                "started_at": started_at,
                "updated_at": started_at,
                "message": "Running",
                "last_event": {"at": started_at, "status": "running", "message": "Running"},
            },
        )

        def on_progress(progress: dict) -> None:
            current = progress.get("current") if isinstance(progress, dict) else {}
            model = (
                (current or {}).get("model")
                or progress.get("model")
                or (progress.get("status") or {}).get("model")
                or model_config.get("label", "auto / main model")
            )
            _set_job(
                job_id,
                {
                    "progress": progress,
                    "updated_at": _now(),
                    "model": model,
                    "last_event": progress,
                },
            )

        try:
            result = target(on_progress)
            finished_at = _now()
            _set_job(
                job_id,
                {
                    "status": "done",
                    "finished_at": finished_at,
                    "updated_at": finished_at,
                    "message": "Done",
                    "result": result,
                    "progress": result if isinstance(result, dict) else {},
                    "last_event": {
                        "at": finished_at,
                        "status": "done",
                        "message": "Done",
                        "profile": payload.get("profile", "default"),
                        "model": (result or {}).get("model") if isinstance(result, dict) else model_config.get("label", "auto / main model"),
                        "model_task": model_task,
                    },
                },
            )
        except Exception as exc:
            finished_at = _now()
            _set_job(
                job_id,
                {
                    "status": "error",
                    "finished_at": finished_at,
                    "updated_at": finished_at,
                    "message": "Error",
                    "error": str(exc),
                    "last_event": {
                        "at": finished_at,
                        "status": "error",
                        "message": "Error",
                        "profile": payload.get("profile", "default"),
                        "error": str(exc),
                        "model_task": model_task,
                    },
                },
            )

    future = _JOB_EXECUTOR.submit(runner)
    with _JOBS_LOCK:
        if job_id in _JOBS:
            _JOBS[job_id]["_future"] = future
    return _job_snapshot(job_id)


def _start_resume_job(profile: str, *, reason: str = "manual") -> dict:
    payload = {
        "profile": profile,
        "all_profiles": False,
        "resume": True,
        "reason": reason,
    }

    def target(on_progress):
        return mempalace.backfill_profile_with_llm_full(
            profile=profile,
            backup=True,
            reset_cursor=False,
            clear_history=False,
            auto_clean=True,
            workers=mempalace.LLM_CONSOLIDATOR_WORKERS,
            progress_callback=on_progress,
        )

    kind = "auto_resume_profile" if reason.startswith("auto") else "resume_full_profile"
    return _start_job(kind, payload, target)


def _start_validation_job(profile: str, *, reason: str = "manual") -> dict:
    payload = {
        "profile": profile,
        "all_profiles": False,
        "reason": reason,
    }

    def target(on_progress):
        on_progress({"phase": "validating", "profile": profile, "reason": reason})
        result = mempalace.validate_and_clean_noise_with_llm(
            profile=profile,
            dry_run=False,
            backup=True,
            max_candidates=mempalace.LLM_VALIDATOR_MAX_CANDIDATES,
        )
        on_progress({"phase": "validation_done", **result})
        return result

    kind = "validate_auto_profile" if reason.startswith("auto") else "validate_clean"
    return _start_job(kind, payload, target)


def _raw_auto_enabled_value(profile: str) -> object:
    paths = mempalace.palace_paths(profile)
    state = mempalace._read_refresh_state(paths)
    cstate = state.get(mempalace.LLM_CONSOLIDATOR_STATE_KEY)
    if not isinstance(cstate, dict):
        return None
    return cstate.get("auto_enabled")


def _ensure_auto_enabled_for_all_profiles() -> list[str]:
    enabled: list[str] = []
    for row in mempalace.list_profiles():
        profile = str(row["name"])
        try:
            raw_auto_enabled = _raw_auto_enabled_value(profile)
            if raw_auto_enabled is not None:
                continue
            mempalace.set_consolidator_auto_enabled(profile=profile, enabled=True)
            enabled.append(profile)
        except Exception as exc:
            _set_auto_worker_state({"last_action": f"auto-enable error for {profile}", "last_error": str(exc)})
    return enabled


def _maybe_resume_stale_jobs(*, include_incomplete_chunks: bool = False) -> None:
    if _has_active_all_profiles_job():
        return
    active_profiles = _active_profile_jobs()
    now = time.time()
    for row in mempalace.list_profiles():
        profile = str(row["name"])
        if profile in active_profiles:
            continue
        throttle_key = f"recover:{profile}"
        if _recently_started(throttle_key, now):
            continue
        try:
            status = _status_with_last_event(mempalace.consolidator_status(profile=profile))
        except Exception:
            continue
        pending = int(status.get("pending_messages") or 0)
        incomplete = bool(
            pending > 0
            and not status.get("running")
            and not status.get("paused")
            and status.get("phase") in {"done", "complete", "error", ""}
            and int(status.get("cursor_message_id") or 0) > 0
        )
        should_resume = bool(status.get("stale") or (include_incomplete_chunks and incomplete))
        if should_resume and pending > 0:
            _AUTO_RESUME_STARTED[throttle_key] = now
            _start_resume_job(profile, reason="stale_after_restart" if status.get("stale") else "pending_after_chunk")


def _maybe_run_auto_jobs() -> list[dict]:
    if _has_active_all_profiles_job():
        return []
    active_profiles = _active_profile_jobs()
    now = time.time()
    started: list[dict] = []
    for row in mempalace.list_profiles():
        profile = str(row["name"])
        if profile in active_profiles:
            continue
        throttle_key = f"auto:{profile}"
        if _recently_started(throttle_key, now):
            continue
        try:
            status = _status_with_last_event(mempalace.consolidator_status(profile=profile))
        except Exception as exc:
            _set_auto_worker_state({"last_action": f"status error for {profile}", "last_error": str(exc)})
            continue
        pending = int(status.get("pending_messages") or 0)
        if (
            pending <= 0
            or not bool(status.get("auto_enabled", False))
            or bool(status.get("paused", False))
            or bool(status.get("running", False))
        ):
            continue
        _AUTO_RESUME_STARTED[throttle_key] = now
        job = _start_resume_job(profile, reason="auto_tick")
        started.append(job)
        active_profiles.add(profile)
    return started


def _maybe_run_auto_validations() -> list[dict]:
    if _has_active_all_profiles_job():
        return []
    active_profiles = _active_profile_jobs()
    now = time.time()
    started: list[dict] = []
    for row in mempalace.list_profiles():
        profile = str(row["name"])
        if profile in active_profiles:
            continue
        throttle_key = f"validate:{profile}"
        if _recently_started_for(throttle_key, now, _AUTO_VALIDATION_RETRY_SECONDS):
            continue
        try:
            status = _status_with_last_event(mempalace.consolidator_status(profile=profile))
        except Exception as exc:
            _set_auto_worker_state({"last_action": f"validation status error for {profile}", "last_error": str(exc)})
            continue
        if not _validation_due(status, now):
            continue
        _AUTO_RESUME_STARTED[throttle_key] = now
        job = _start_validation_job(profile, reason="auto_validation_due")
        started.append(job)
        active_profiles.add(profile)
    return started


def _ensure_auto_worker() -> None:
    global _AUTO_WORKER_STARTED
    with _AUTO_WORKER_LOCK:
        if _AUTO_WORKER_STARTED:
            return
        _AUTO_WORKER_STARTED = True
        _AUTO_WORKER_STATE.update(
            {
                "started": True,
                "started_at": _now(),
                "last_action": "starting",
                "last_error": "",
            }
        )

    def runner() -> None:
        time.sleep(2)
        while True:
            tick_at = _now()
            try:
                enabled = _ensure_auto_enabled_for_all_profiles()
                _maybe_resume_stale_jobs(include_incomplete_chunks=True)
                started = _maybe_run_auto_jobs()
                validations = _maybe_run_auto_validations()
                action_parts = []
                if enabled:
                    action_parts.append("enabled auto for " + ", ".join(enabled))
                if started:
                    action_parts.append(
                        "started "
                        + ", ".join(
                            f"{job.get('profile', 'default')}:{job.get('id', '')}" for job in started
                        )
                    )
                if validations:
                    action_parts.append(
                        "validated "
                        + ", ".join(
                            f"{job.get('profile', 'default')}:{job.get('id', '')}" for job in validations
                        )
                    )
                _set_auto_worker_state(
                    {
                        "last_tick_at": tick_at,
                        "next_tick_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() + _AUTO_TICK_INTERVAL_SECONDS),
                        ),
                        "last_action": "; ".join(action_parts) if action_parts else "checked",
                        "last_error": "",
                    }
                )
            except Exception as exc:
                _set_auto_worker_state(
                    {
                        "last_tick_at": tick_at,
                        "next_tick_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() + _AUTO_TICK_INTERVAL_SECONDS),
                        ),
                        "last_action": "error",
                        "last_error": str(exc),
                    }
                )
            time.sleep(_AUTO_TICK_INTERVAL_SECONDS)

    threading.Thread(target=runner, name="mempalace-auto-scheduler", daemon=True).start()


@router.on_event("startup")
async def _startup_resume_stale_jobs() -> None:
    _ensure_auto_enabled_for_all_profiles()
    _maybe_resume_stale_jobs(include_incomplete_chunks=True)
    _ensure_auto_worker()


def _api_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _body_dict(body: BaseModel) -> dict:
    dump = getattr(body, "model_dump", None)
    if callable(dump):
        return dump()
    return body.dict()


def _extractor_config() -> dict:
    extractor = mempalace.llm_task_model_config(mempalace.LLM_CONSOLIDATOR_TASK)
    validator = mempalace.llm_task_model_config(mempalace.LLM_VALIDATOR_TASK)
    return {
        "task": mempalace.LLM_CONSOLIDATOR_TASK,
        "adapter": mempalace.LLM_CONSOLIDATOR_ADAPTER,
        "models": extractor.get("models", []),
        "label": extractor.get("label", ""),
        "validator": {
            "task": mempalace.LLM_VALIDATOR_TASK,
            "adapter": mempalace.LLM_VALIDATOR_ADAPTER,
            "models": validator.get("models", []),
            "label": validator.get("label", ""),
        },
        "workers": mempalace.LLM_CONSOLIDATOR_WORKERS,
        "max_workers": mempalace.LLM_CONSOLIDATOR_MAX_WORKERS,
        "batch_size": mempalace.LLM_CONSOLIDATOR_BATCH_SIZE,
        "max_batch_size": mempalace.LLM_CONSOLIDATOR_MAX_BATCH_SIZE,
    }


class GenerateBody(BaseModel):
    profile: str = "default"
    dry_run: bool = False
    auto_clean: bool = True
    clean_max_delete: int = mempalace.AUTO_CLEAN_MAX_DELETE


class RebuildBody(BaseModel):
    profile: str = "default"
    all_profiles: bool = False
    backup: bool = True
    auto_clean: bool = True
    clean_max_delete: int = mempalace.AUTO_CLEAN_MAX_DELETE
    limit: int = mempalace.LLM_CONSOLIDATOR_BATCH_SIZE
    max_batches: int = 10
    full: bool = True
    workers: int = mempalace.LLM_CONSOLIDATOR_WORKERS
    profile_workers: int = 2


class RefreshBody(BaseModel):
    profile: str = "default"
    force: bool = False


class CleanNoiseBody(BaseModel):
    profile: str = "default"
    palace: str = ""
    dry_run: bool = True
    backup: bool = True
    max_delete: int = 250


class ValidateCleanBody(BaseModel):
    profile: str = "default"
    all_profiles: bool = False
    palace: str = ""
    dry_run: bool = False
    backup: bool = True
    max_candidates: int = mempalace.LLM_VALIDATOR_MAX_CANDIDATES


class ConsolidatorRunBody(BaseModel):
    profile: str = "default"
    all_profiles: bool = False
    dry_run: bool = False
    backfill: bool = False
    reset_cursor: bool = False
    clear_history: bool = False
    resume: bool = False
    backup: bool = True
    limit: int = mempalace.LLM_CONSOLIDATOR_BATCH_SIZE
    max_batches: int = 1
    full: bool = True
    workers: int = mempalace.LLM_CONSOLIDATOR_WORKERS
    profile_workers: int = 2
    auto_clean: bool = True
    clean_max_delete: int = mempalace.AUTO_CLEAN_MAX_DELETE


class ConsolidatorPauseBody(BaseModel):
    profile: str = "default"
    all_profiles: bool = False
    paused: bool = True


class ConsolidatorAutoBody(BaseModel):
    profile: str = "default"
    all_profiles: bool = False
    enabled: bool = True
    unpause: bool = False


class ConsolidatorResetBody(BaseModel):
    profile: str = "default"
    cursor: int = 0


@router.get("/profiles")
async def profiles():
    try:
        rows = []
        for row in mempalace.list_profiles():
            palaces = mempalace.list_palaces(profile=row["name"], include_stats=False)
            rows.append({**row, "palace_count": len(palaces)})
        return {"profiles": rows}
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/palaces")
async def palaces(profile: str = "default"):
    try:
        return {"profile": profile, "palaces": mempalace.list_palaces(profile=profile)}
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/matrix")
async def matrix():
    try:
        return mempalace.profile_matrix()
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/graph")
async def graph(
    profile: str = "default",
    palace: str = "",
    center: str = "",
    depth: int = Query(default=1, ge=1, le=3),
    query: str = "",
    node_limit: int = Query(default=180, ge=10, le=1000),
    edge_limit: int = Query(default=420, ge=10, le=3000),
    min_confidence: float = 0.0,
):
    try:
        if center:
            return mempalace.load_subgraph(
                center,
                profile=profile,
                palace=palace,
                depth=depth,
                node_limit=node_limit,
                edge_limit=edge_limit,
                min_confidence=min_confidence,
            )
        return mempalace.load_graph(
            palace,
            profile=profile,
            query=query,
            node_limit=node_limit,
            edge_limit=edge_limit,
            min_confidence=min_confidence,
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/node")
async def node(
    node_id: str = Query(..., min_length=1),
    profile: str = "default",
    palace: str = "",
    depth: int = Query(default=2, ge=1, le=4),
    limit: int = Query(default=100, ge=10, le=300),
):
    try:
        return mempalace.node_tree(
            node_id,
            profile=profile,
            palace=palace,
            depth=depth,
            limit=limit,
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    profile: str = "default",
    palace: str = "",
    limit: int = Query(default=30, ge=1, le=100),
):
    try:
        return mempalace.search(q, profile=profile, palace=palace, limit=limit)
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/generate")
async def generate(body: GenerateBody):
    try:
        return mempalace.generate_from_markdown(
            profile=body.profile,
            dry_run=body.dry_run,
            auto_clean=body.auto_clean,
            clean_max_delete=body.clean_max_delete,
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/rebuild")
async def rebuild(body: RebuildBody):
    try:
        if body.full:
            def target(on_progress):
                if body.all_profiles:
                    result = mempalace.backfill_all_profiles_with_llm_full(
                        backup=body.backup,
                        limit=body.limit,
                        max_batches=body.max_batches,
                        auto_clean=body.auto_clean,
                        clean_max_delete=body.clean_max_delete,
                        workers=body.workers,
                        profile_workers=body.profile_workers,
                        progress_callback=on_progress,
                    )
                    for row in mempalace.list_profiles():
                        mempalace.generate_from_markdown(
                            profile=str(row["name"]),
                            auto_clean=body.auto_clean,
                            clean_max_delete=body.clean_max_delete,
                        )
                    return {**result, "matrix": mempalace.profile_matrix()}
                result = mempalace.backfill_profile_with_llm_full(
                    profile=body.profile,
                    backup=body.backup,
                    limit=body.limit,
                    max_batches=body.max_batches,
                    auto_clean=body.auto_clean,
                    clean_max_delete=body.clean_max_delete,
                    workers=body.workers,
                    progress_callback=on_progress,
                )
                markdown = mempalace.generate_from_markdown(
                    profile=body.profile,
                    auto_clean=body.auto_clean,
                    clean_max_delete=body.clean_max_delete,
                )
                return {**result, "generated": markdown}

            return _start_job(
                "full_rebuild_all" if body.all_profiles else "full_rebuild_profile",
                _body_dict(body),
                target,
            )
        if body.all_profiles:
            return mempalace.backfill_all_profiles_with_llm(
                backup=body.backup,
                limit=body.limit,
                max_batches=body.max_batches,
                auto_clean=body.auto_clean,
                clean_max_delete=body.clean_max_delete,
                workers=body.workers,
            )
        result = mempalace.backfill_profile_with_llm(
            profile=body.profile,
            backup=body.backup,
            limit=body.limit,
            max_batches=body.max_batches,
            auto_clean=body.auto_clean,
            clean_max_delete=body.clean_max_delete,
            workers=body.workers,
        )
        markdown = mempalace.generate_from_markdown(
            profile=body.profile,
            auto_clean=body.auto_clean,
            clean_max_delete=body.clean_max_delete,
        )
        return {**result, "generated": markdown}
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/refresh")
async def refresh(body: RefreshBody):
    try:
        return mempalace.refresh_if_due(profile=body.profile, force=body.force)
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/consolidator/status")
async def consolidator_status(profile: str = "default"):
    try:
        status = mempalace.consolidator_status(profile=profile)
        status = _status_with_last_event(status)
        status["scheduler"] = _auto_worker_snapshot()
        return status
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/consolidator/statuses")
async def consolidator_statuses():
    try:
        scheduler = _auto_worker_snapshot()
        statuses = []
        for row in mempalace.list_profiles():
            status = _status_with_last_event(mempalace.consolidator_status(profile=str(row["name"])))
            status["scheduler"] = scheduler
            statuses.append(status)
        return {"profiles": statuses, "scheduler": scheduler}
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/consolidator/config")
async def consolidator_config():
    try:
        return _extractor_config()
    except Exception as exc:
        raise _api_error(exc) from exc


@router.get("/consolidator/jobs")
async def consolidator_jobs():
    _cleanup_zombie_jobs()
    with _JOBS_LOCK:
        jobs = [_public_job(job) for job in _JOBS.values()]
    jobs.sort(key=lambda row: row.get("created_at", ""), reverse=True)
    return {"jobs": jobs[:_MAX_JOBS], "scheduler": _auto_worker_snapshot()}


@router.get("/consolidator/scheduler")
async def consolidator_scheduler():
    return _auto_worker_snapshot()


@router.get("/consolidator/jobs/{job_id}")
async def consolidator_job(job_id: str):
    try:
        return _job_snapshot(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc


@router.post("/consolidator/run")
async def consolidator_run(body: ConsolidatorRunBody):
    try:
        if body.resume and not body.dry_run:
            if body.all_profiles:
                def target(on_progress):
                    return mempalace.backfill_all_profiles_with_llm_full(
                        backup=body.backup,
                        reset_cursor=False,
                        clear_history=False,
                        limit=body.limit,
                        max_batches=body.max_batches,
                        auto_clean=body.auto_clean,
                        clean_max_delete=body.clean_max_delete,
                        workers=body.workers,
                        profile_workers=body.profile_workers,
                        progress_callback=on_progress,
                    )

                return _start_job("resume_full_all", _body_dict(body), target)
            return _start_resume_job(body.profile, reason="manual")
        if body.backfill and not body.dry_run and body.full:
            def target(on_progress):
                if body.all_profiles:
                    return mempalace.backfill_all_profiles_with_llm_full(
                        backup=body.backup,
                        limit=body.limit,
                        max_batches=body.max_batches,
                        auto_clean=body.auto_clean,
                        clean_max_delete=body.clean_max_delete,
                        workers=body.workers,
                        profile_workers=body.profile_workers,
                        progress_callback=on_progress,
                    )
                return mempalace.backfill_profile_with_llm_full(
                    profile=body.profile,
                    backup=body.backup,
                    limit=body.limit,
                    max_batches=body.max_batches,
                    auto_clean=body.auto_clean,
                    clean_max_delete=body.clean_max_delete,
                    workers=body.workers,
                    progress_callback=on_progress,
                )

            return _start_job(
                "full_backfill_all" if body.all_profiles else "full_backfill_profile",
                _body_dict(body),
                target,
            )
        if body.all_profiles:
            results = [
                (
                    mempalace.backfill_profile_with_llm(
                        profile=str(row["name"]),
                        dry_run=body.dry_run,
                        backup=body.backup,
                        limit=body.limit,
                        max_batches=body.max_batches,
                        auto_clean=body.auto_clean,
                        clean_max_delete=body.clean_max_delete,
                        workers=body.workers,
                    )
                    if body.backfill
                    else mempalace.consolidate_profile(
                        profile=str(row["name"]),
                        dry_run=body.dry_run,
                        force=True,
                        reset_cursor=body.reset_cursor,
                        clear_history=body.clear_history,
                        backup=body.backup,
                        limit=body.limit,
                        max_batches=body.max_batches,
                        auto_clean=body.auto_clean,
                        clean_max_delete=body.clean_max_delete,
                        workers=body.workers,
                    )
                )
                for row in mempalace.list_profiles()
            ]
            return {"profiles": results, "matrix": mempalace.profile_matrix()}
        if body.backfill:
            return mempalace.backfill_profile_with_llm(
                profile=body.profile,
                dry_run=body.dry_run,
                backup=body.backup,
                limit=body.limit,
                max_batches=body.max_batches,
                auto_clean=body.auto_clean,
                clean_max_delete=body.clean_max_delete,
                workers=body.workers,
            )
        return mempalace.consolidate_profile(
            profile=body.profile,
            dry_run=body.dry_run,
            force=True,
            reset_cursor=body.reset_cursor,
            clear_history=body.clear_history,
            backup=body.backup,
            limit=body.limit,
            max_batches=body.max_batches,
            auto_clean=body.auto_clean,
            clean_max_delete=body.clean_max_delete,
            workers=body.workers,
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/consolidator/pause")
async def consolidator_pause(body: ConsolidatorPauseBody):
    try:
        if body.all_profiles:
            statuses = [
                mempalace.set_consolidator_paused(profile=str(row["name"]), paused=body.paused)
                for row in mempalace.list_profiles()
            ]
            scheduler = _auto_worker_snapshot()
            return {"profiles": statuses, "scheduler": scheduler}
        return mempalace.set_consolidator_paused(profile=body.profile, paused=body.paused)
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/consolidator/auto")
async def consolidator_auto(body: ConsolidatorAutoBody):
    try:
        def update_one(profile: str) -> dict:
            status = mempalace.set_consolidator_auto_enabled(profile=profile, enabled=body.enabled)
            if body.unpause:
                status = mempalace.set_consolidator_paused(profile=profile, paused=False)
            return status

        if body.all_profiles:
            statuses = [update_one(str(row["name"])) for row in mempalace.list_profiles()]
            scheduler = _auto_worker_snapshot()
            return {"profiles": statuses, "scheduler": scheduler}
        return update_one(body.profile)
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/consolidator/reset")
async def consolidator_reset(body: ConsolidatorResetBody):
    try:
        return mempalace.reset_consolidator_cursor(profile=body.profile, cursor=body.cursor)
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/clean-noise")
async def clean_noise(body: CleanNoiseBody):
    try:
        return mempalace.clean_noise(
            profile=body.profile,
            palace=body.palace,
            dry_run=body.dry_run,
            backup=body.backup,
            max_delete=body.max_delete,
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@router.post("/validate-clean")
async def validate_clean(body: ValidateCleanBody):
    try:
        def target(on_progress):
            if body.all_profiles:
                results = []
                for row in mempalace.list_profiles():
                    profile_name = str(row["name"])
                    on_progress({"phase": "validating", "profile": profile_name})
                    result = mempalace.validate_and_clean_noise_with_llm(
                        profile=profile_name,
                        palace=body.palace,
                        dry_run=body.dry_run,
                        backup=body.backup,
                        max_candidates=body.max_candidates,
                    )
                    results.append(result)
                    on_progress({"phase": "profile_validation_done", "profile": profile_name, "profile_result": result})
                aggregate = {"profiles": results, "phase": "validation_done"}
                on_progress(aggregate)
                return aggregate

            result = mempalace.validate_and_clean_noise_with_llm(
                profile=body.profile,
                palace=body.palace,
                dry_run=body.dry_run,
                backup=body.backup,
                max_candidates=body.max_candidates,
            )
            on_progress({"phase": "validation_done", **result})
            return result

        return _start_job("validate_clean_all" if body.all_profiles else "validate_clean", _body_dict(body), target)
    except Exception as exc:
        raise _api_error(exc) from exc
