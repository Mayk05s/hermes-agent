from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plugins.mempalace.dashboard import plugin_api


def _iso_ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


@pytest.fixture(autouse=True)
def _reset_scheduler_state():
    with plugin_api._JOBS_LOCK:
        plugin_api._JOBS.clear()
    plugin_api._AUTO_RESUME_STARTED.clear()
    yield
    with plugin_api._JOBS_LOCK:
        plugin_api._JOBS.clear()
    plugin_api._AUTO_RESUME_STARTED.clear()


def test_auto_validator_runs_when_due(monkeypatch):
    old = plugin_api._AUTO_VALIDATION_INTERVAL_SECONDS + 60
    status = {
        "profile": "family",
        "pending_messages": 0,
        "running": False,
        "paused": False,
        "stale": False,
        "palaces": [{"palace": "history_telegram"}],
        "last_finished_at": _iso_ago(old),
        "events": [
            {
                "at": _iso_ago(old),
                "status": "success",
                "message": "MemPalace validation finished",
                "model_task": "mempalace_validator",
            }
        ],
    }
    started = []

    monkeypatch.setattr(plugin_api.mempalace, "list_profiles", lambda: [{"name": "family"}])
    monkeypatch.setattr(plugin_api.mempalace, "consolidator_status", lambda profile: status)
    monkeypatch.setattr(
        plugin_api,
        "_start_validation_job",
        lambda profile, reason: started.append((profile, reason)) or {"profile": profile, "id": "job1"},
    )

    jobs = plugin_api._maybe_run_auto_validations()

    assert jobs == [{"profile": "family", "id": "job1"}]
    assert started == [("family", "auto_validation_due")]


def test_auto_validator_not_due_with_pending_or_recent_validation(monkeypatch):
    recent = max(1, plugin_api._AUTO_VALIDATION_INTERVAL_SECONDS // 2)
    statuses = {
        "pending": {
            "profile": "pending",
            "pending_messages": 1,
            "running": False,
            "paused": False,
            "palaces": [{"palace": "history_telegram"}],
            "last_finished_at": _iso_ago(plugin_api._AUTO_VALIDATION_INTERVAL_SECONDS + 60),
            "events": [],
        },
        "recent": {
            "profile": "recent",
            "pending_messages": 0,
            "running": False,
            "paused": False,
            "palaces": [{"palace": "history_telegram"}],
            "last_finished_at": _iso_ago(plugin_api._AUTO_VALIDATION_INTERVAL_SECONDS + 60),
            "events": [
                {
                    "at": _iso_ago(recent),
                    "status": "success",
                    "message": "MemPalace validation finished",
                    "model_task": "mempalace_validator",
                }
            ],
        },
    }

    monkeypatch.setattr(plugin_api.mempalace, "list_profiles", lambda: [{"name": "pending"}, {"name": "recent"}])
    monkeypatch.setattr(plugin_api.mempalace, "consolidator_status", lambda profile: statuses[profile])
    monkeypatch.setattr(plugin_api, "_start_validation_job", lambda *_args, **_kwargs: pytest.fail("not due"))

    assert plugin_api._maybe_run_auto_validations() == []


def test_auto_enabled_seeds_legacy_missing_state_only(monkeypatch):
    statuses = {
        "missing": {},
        "unset": {},
        "disabled": {"auto_enabled": False},
        "enabled": {"auto_enabled": True},
    }
    raw_values = {
        "missing": None,
        "unset": None,
        "disabled": False,
        "enabled": True,
    }

    monkeypatch.setattr(
        plugin_api.mempalace,
        "list_profiles",
        lambda: [{"name": name} for name in ("missing", "unset", "disabled", "enabled")],
    )
    monkeypatch.setattr(plugin_api, "_raw_auto_enabled_value", lambda profile: raw_values[profile])
    monkeypatch.setattr(
        plugin_api.mempalace,
        "set_consolidator_auto_enabled",
        lambda profile, enabled: statuses[profile].update({"auto_enabled": enabled}),
    )

    migrated = plugin_api._ensure_auto_enabled_for_all_profiles()

    assert migrated == ["missing", "unset"]
    assert statuses["missing"]["auto_enabled"] is True
    assert statuses["unset"]["auto_enabled"] is True
    assert statuses["disabled"]["auto_enabled"] is False
    assert statuses["enabled"]["auto_enabled"] is True


def test_zombie_worker_future_is_cleaned_from_public_jobs():
    class DoneFuture:
        def done(self):
            return True

        def exception(self):
            return None

    with plugin_api._JOBS_LOCK:
        plugin_api._JOBS["job1"] = {
            "id": "job1",
            "kind": "auto_resume_profile",
            "status": "running",
            "profile": "default",
            "created_at": _iso_ago(60),
            "started_at": _iso_ago(60),
            "finished_at": "",
            "_future": DoneFuture(),
        }

    plugin_api._cleanup_zombie_jobs()
    snapshot = plugin_api._job_snapshot("job1")

    assert snapshot["status"] == "done"
    assert snapshot["finished_at"]
    assert snapshot["last_event"]["status"] == "done"
    assert "_future" not in snapshot


def test_auto_scheduler_waits_for_all_profiles_job(monkeypatch):
    with plugin_api._JOBS_LOCK:
        plugin_api._JOBS["all1"] = {
            "id": "all1",
            "kind": "full_backfill_all",
            "status": "running",
            "profile": "default",
            "all_profiles": True,
        }

    monkeypatch.setattr(plugin_api.mempalace, "list_profiles", lambda: pytest.fail("blocked by all-profiles job"))

    plugin_api._maybe_resume_stale_jobs(include_incomplete_chunks=True)
    assert plugin_api._maybe_run_auto_jobs() == []
    assert plugin_api._maybe_run_auto_validations() == []
