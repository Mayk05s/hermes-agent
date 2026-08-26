"""Restart-safe delivery idempotency for scheduled cron slots."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from cron.delivery_ledger import DeliveryLedger, stable_slot_key
from cron.scheduler import _deliver_result, _parse_delivery_envelope, tick


def _target(platform: str = "telegram", chat_id: str = "123") -> dict:
    return {"platform": platform, "chat_id": chat_id, "thread_id": None}


def _prepare(ledger: DeliveryLedger, job_id: str, scheduled_for: str, targets):
    slot_key = stable_slot_key(job_id, scheduled_for)
    ledger.prepare_slot(
        slot_key=slot_key,
        job_id=job_id,
        scheduled_for=scheduled_for,
        success=True,
        error=None,
        delivery_content="cached payload",
        should_deliver=True,
        targets=targets,
    )
    return slot_key


def test_slot_key_is_stable_for_equivalent_timestamp_offsets():
    first = stable_slot_key("job-1", "2026-08-24T09:00:00+00:00")
    second = stable_slot_key("job-1", "2026-08-24T12:00:00+03:00")

    assert first == second
    assert first != stable_slot_key("job-2", "2026-08-24T09:00:00Z")


def test_delivery_envelope_separates_chat_message_from_receipts():
    job = {"delivery_receipt_script": "coaching_tick.py"}
    content, receipts = _parse_delivery_envelope(
        job,
        '{"schemaVersion":1,"wakeAgent":true,"message":"Татьяна, мягкое напоминание",'
        '"receipts":[{"kind":"nutrition-coaching","participant":"tatyana"}]}',
    )
    assert content == "Татьяна, мягкое напоминание"
    assert receipts == [{"kind": "nutrition-coaching", "participant": "tatyana"}]
    assert "participant" not in content


def test_delivery_envelope_accepts_exact_silent_marker_without_receipts():
    job = {"delivery_receipt_script": "coaching_tick.py"}

    content, receipts = _parse_delivery_envelope(job, "[SILENT]\n")

    assert content == "[SILENT]"
    assert receipts == []


def test_only_confirmed_target_failure_is_retryable(tmp_path):
    ledger = DeliveryLedger(tmp_path / "cron")
    target = _target()
    slot_key = _prepare(
        ledger, "job-1", "2026-08-24T09:00:00Z", [target]
    )

    assert ledger.begin_target(slot_key, target) == "sending"
    ledger.finish_target(slot_key, target, success=False, error="network down")
    assert ledger.failed_target_errors(slot_key) == ["network down"]

    # A positively observed error can be retried.
    assert ledger.begin_target(slot_key, target) == "sending"
    ledger.finish_target(slot_key, target, success=True)
    assert ledger.failed_target_errors(slot_key) == []

    # A committed success is never claimable again.
    assert ledger.begin_target(slot_key, target) == "delivered"


def test_interrupted_send_becomes_uncertain_and_suppresses_duplicate(tmp_path):
    ledger = DeliveryLedger(tmp_path / "cron")
    target = _target()
    slot_key = _prepare(
        ledger, "job-1", "2026-08-24T09:00:00Z", [target]
    )

    assert ledger.begin_target(slot_key, target) == "sending"
    # A recovery process sees that send as ambiguous and fails closed.
    assert ledger.begin_target(slot_key, target) == "uncertain"
    assert ledger.begin_target(slot_key, target) == "uncertain"
    assert "duplicate suppressed" in ledger.uncertain_target_notes(slot_key)[0]


def test_multi_target_retry_does_not_duplicate_successful_target(tmp_path):
    from gateway.config import Platform

    ledger = DeliveryLedger(tmp_path / "cron")
    targets = [_target("telegram", "111"), _target("discord", "222")]
    slot_key = _prepare(
        ledger, "job-1", "2026-08-24T09:00:00Z", targets
    )
    job = {"id": "job-1", "deliver": "local"}

    telegram_config = MagicMock(enabled=True)
    discord_config = MagicMock(enabled=True)
    gateway_config = MagicMock(
        platforms={
            Platform.TELEGRAM: telegram_config,
            Platform.DISCORD: discord_config,
        }
    )

    sender = AsyncMock(
        side_effect=[{"success": True}, {"error": "discord unavailable"}]
    )
    with patch("gateway.config.load_gateway_config", return_value=gateway_config), patch(
        "tools.send_message_tool._send_to_platform", new=sender
    ):
        first_error = _deliver_result(
            job,
            "cached payload",
            slot_key=slot_key,
            delivery_targets=targets,
            ledger=ledger,
        )
        assert "discord unavailable" in first_error

        sender.side_effect = None
        sender.return_value = {"success": True}
        second_error = _deliver_result(
            job,
            "cached payload",
            slot_key=slot_key,
            delivery_targets=targets,
            ledger=ledger,
        )

    assert second_error is None
    assert sender.await_count == 3
    # Telegram succeeded on the first attempt and was skipped on retry.
    assert sender.await_args_list[0].args[0] == Platform.TELEGRAM
    assert sender.await_args_list[1].args[0] == Platform.DISCORD
    assert sender.await_args_list[2].args[0] == Platform.DISCORD


def test_transport_exception_is_uncertain_and_not_retried(tmp_path):
    from gateway.config import Platform

    ledger = DeliveryLedger(tmp_path / "cron")
    target = _target()
    slot_key = _prepare(
        ledger, "job-1", "2026-08-24T09:00:00Z", [target]
    )
    job = {"id": "job-1", "deliver": "local"}
    gateway_config = MagicMock(
        platforms={Platform.TELEGRAM: MagicMock(enabled=True)}
    )
    sender = AsyncMock(side_effect=OSError("connection dropped after send"))

    with patch("gateway.config.load_gateway_config", return_value=gateway_config), patch(
        "tools.send_message_tool._send_to_platform", new=sender
    ):
        assert _deliver_result(
            job,
            "cached payload",
            slot_key=slot_key,
            delivery_targets=[target],
            ledger=ledger,
        ) is None
        assert _deliver_result(
            job,
            "cached payload",
            slot_key=slot_key,
            delivery_targets=[target],
            ledger=ledger,
        ) is None

    assert sender.await_count == 1
    assert "duplicate suppressed" in ledger.uncertain_target_notes(slot_key)[0]


def test_tick_retries_cached_payload_without_rerunning_agent(tmp_path):
    lock_dir = tmp_path / "cron"
    lock_dir.mkdir()
    lock_file = lock_dir / ".tick.lock"
    scheduled_for = datetime(2026, 8, 24, 9, tzinfo=timezone.utc).isoformat()
    job = {
        "id": "daily-summary",
        "name": "summary",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
        "next_run_at": scheduled_for,
    }

    delivery_results = ["temporary network error", None]
    with patch("cron.scheduler._hermes_home", tmp_path), patch(
        "cron.scheduler._get_lock_paths", return_value=(lock_dir, lock_file)
    ), patch("cron.scheduler.get_due_jobs", return_value=[job]), patch(
        "cron.scheduler.run_job",
        return_value=(True, "# audit output", "same summary", None),
    ) as run_mock, patch(
        "cron.scheduler.save_job_output", return_value=tmp_path / "out.md"
    ) as save_mock, patch(
        "cron.scheduler._deliver_result", side_effect=delivery_results
    ) as deliver_mock, patch(
        "cron.scheduler.mark_job_delivery_retry"
    ) as retry_mock, patch(
        "cron.scheduler.mark_job_run"
    ) as mark_mock:
        assert tick(verbose=False) == 1
        assert tick(verbose=False) == 1

    assert run_mock.call_count == 1
    assert save_mock.call_count == 1
    assert deliver_mock.call_count == 2
    assert deliver_mock.call_args_list[0].args[1] == "same summary"
    assert deliver_mock.call_args_list[1].args[1] == "same summary"
    retry_mock.assert_called_once_with("daily-summary", "temporary network error")
    mark_mock.assert_called_once_with(
        "daily-summary", True, None, delivery_error=None
    )


def test_completed_slot_advances_job_after_restart_without_delivery(tmp_path):
    lock_dir = tmp_path / "cron"
    lock_dir.mkdir()
    lock_file = lock_dir / ".tick.lock"
    scheduled_for = "2026-08-24T09:00:00Z"
    job = {
        "id": "daily-summary",
        "name": "summary",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
        "next_run_at": scheduled_for,
    }
    ledger = DeliveryLedger(lock_dir)
    slot_key = _prepare(ledger, job["id"], scheduled_for, [_target()])
    ledger.mark_slot_completed(slot_key)

    with patch("cron.scheduler._hermes_home", tmp_path), patch(
        "cron.scheduler._get_lock_paths", return_value=(lock_dir, lock_file)
    ), patch("cron.scheduler.get_due_jobs", return_value=[job]), patch(
        "cron.scheduler.run_job"
    ) as run_mock, patch("cron.scheduler._deliver_result") as deliver_mock, patch(
        "cron.scheduler.mark_job_run"
    ) as mark_mock:
        assert tick(verbose=False) == 1

    run_mock.assert_not_called()
    deliver_mock.assert_not_called()
    mark_mock.assert_called_once_with(
        "daily-summary", True, None, delivery_error=None
    )


def test_receipt_callback_retries_after_send_without_duplicate(tmp_path):
    lock_dir = tmp_path / "cron"
    lock_dir.mkdir()
    lock_file = lock_dir / ".tick.lock"
    scheduled_for = "2026-08-24T09:00:00Z"
    job = {
        "id": "coaching",
        "name": "coaching",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
        "profile": "hudeem-tripio",
        "delivery_receipt_script": "coaching_tick.py",
        "next_run_at": scheduled_for,
    }
    envelope = (
        '{"schemaVersion":1,"wakeAgent":true,"message":"same reminder",'
        '"receipts":[{"kind":"nutrition-coaching","participant":"tatyana"}]}'
    )
    send_count = 0

    def durable_delivery(_job, _content, *, slot_key, delivery_targets, ledger, **_kwargs):
        nonlocal send_count
        for target in delivery_targets:
            state = ledger.begin_target(slot_key, target)
            if state == "sending":
                send_count += 1
                ledger.finish_target(slot_key, target, success=True)
        return None

    receipt_results = [
        (True, None, False),
        (False, "receipt backend down", False),
        (True, None, False),
        (True, None, False),
    ]
    with patch("cron.scheduler._hermes_home", tmp_path), patch(
        "cron.scheduler._get_lock_paths", return_value=(lock_dir, lock_file)
    ), patch("cron.scheduler.get_due_jobs", return_value=[job]), patch(
        "cron.scheduler.run_job",
        return_value=(True, "# audit output", envelope, None),
    ) as run_mock, patch(
        "cron.scheduler.save_job_output", return_value=tmp_path / "out.md"
    ), patch(
        "cron.scheduler._deliver_result", side_effect=durable_delivery
    ), patch(
        "cron.scheduler._run_delivery_receipt_hook", side_effect=receipt_results
    ) as receipt_mock, patch(
        "cron.scheduler.mark_job_delivery_retry"
    ) as retry_mock, patch(
        "cron.scheduler.mark_job_run"
    ) as mark_mock:
        assert tick(verbose=False) == 1
        assert tick(verbose=False) == 1

    assert run_mock.call_count == 1
    assert send_count == 1
    assert [call.args[2] for call in receipt_mock.call_args_list] == [
        "queued",
        "delivered",
        "queued",
        "delivered",
    ]
    retry_mock.assert_called_once_with("coaching", "receipt backend down")
    mark_mock.assert_called_once_with(
        "coaching", True, None, delivery_error=None
    )


def test_authoritative_delivered_receipt_suppresses_chat_send(tmp_path):
    lock_dir = tmp_path / "cron"
    lock_dir.mkdir()
    lock_file = lock_dir / ".tick.lock"
    job = {
        "id": "coaching",
        "name": "coaching",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
        "profile": "hudeem-tripio",
        "delivery_receipt_script": "coaching_tick.py",
        "next_run_at": "2026-08-24T09:00:00Z",
    }
    envelope = (
        '{"schemaVersion":1,"wakeAgent":true,"message":"same reminder",'
        '"receipts":[{"kind":"nutrition-coaching","participant":"tatyana"}]}'
    )
    with patch("cron.scheduler._hermes_home", tmp_path), patch(
        "cron.scheduler._get_lock_paths", return_value=(lock_dir, lock_file)
    ), patch("cron.scheduler.get_due_jobs", return_value=[job]), patch(
        "cron.scheduler.run_job",
        return_value=(True, "# audit output", envelope, None),
    ), patch(
        "cron.scheduler.save_job_output", return_value=tmp_path / "out.md"
    ), patch(
        "cron.scheduler._run_delivery_receipt_hook",
        return_value=(True, None, True),
    ), patch("cron.scheduler._deliver_result") as deliver_mock, patch(
        "cron.scheduler.mark_job_run"
    ) as mark_mock:
        assert tick(verbose=False) == 1

    deliver_mock.assert_not_called()
    mark_mock.assert_called_once_with(
        "coaching",
        True,
        None,
        delivery_error="authoritative receipt already delivered; duplicate suppressed",
    )


def test_stale_coaching_slot_fails_receipt_and_never_sends(tmp_path):
    lock_dir = tmp_path / "cron"
    lock_dir.mkdir()
    lock_file = lock_dir / ".tick.lock"
    scheduled_for = "2026-08-24T09:00:00Z"
    job = {
        "id": "coaching",
        "name": "coaching",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
        "profile": "hudeem-tripio",
        "delivery_receipt_script": "coaching_tick.py",
        "next_run_at": scheduled_for,
        "_stale_slot_expired": True,
    }
    ledger = DeliveryLedger(lock_dir)
    slot_key = stable_slot_key(job["id"], scheduled_for)
    ledger.prepare_slot(
        slot_key=slot_key,
        job_id=job["id"],
        scheduled_for=scheduled_for,
        success=True,
        error=None,
        delivery_content="stale reminder",
        should_deliver=True,
        targets=[_target()],
        receipts=[{"kind": "nutrition-coaching", "participant": "tatyana"}],
    )
    with patch("cron.scheduler._hermes_home", tmp_path), patch(
        "cron.scheduler._get_lock_paths", return_value=(lock_dir, lock_file)
    ), patch("cron.scheduler.get_due_jobs", return_value=[job]), patch(
        "cron.scheduler.run_job"
    ) as run_mock, patch(
        "cron.scheduler._run_delivery_receipt_hook",
        return_value=(True, None, False),
    ) as receipt_mock, patch(
        "cron.scheduler._deliver_result"
    ) as deliver_mock, patch(
        "cron.scheduler.mark_job_run"
    ) as mark_mock:
        assert tick(verbose=False) == 1

    run_mock.assert_not_called()
    deliver_mock.assert_not_called()
    assert receipt_mock.call_args.args[2] == "failed"
    assert receipt_mock.call_args.kwargs["note"] == "stale_slot"
    mark_mock.assert_called_once_with(
        "coaching",
        False,
        "stale scheduled slot expired without late delivery",
        delivery_error="stale scheduled slot expired without late delivery",
    )
    assert ledger.get_slot(slot_key) is None
