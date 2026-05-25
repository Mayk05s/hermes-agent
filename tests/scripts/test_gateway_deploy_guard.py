from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gateway_deploy_guard.py"
spec = importlib.util.spec_from_file_location("gateway_deploy_guard", SCRIPT)
assert spec is not None
assert spec.loader is not None
gateway_deploy_guard: ModuleType = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gateway_deploy_guard
spec.loader.exec_module(gateway_deploy_guard)


class FakeRunner(gateway_deploy_guard.Runner):
    def __init__(self, *, apply: bool = False, active: bool = True):
        super().__init__(apply=apply)
        self.active = active
        self.calls: list[tuple[str, list[str]]] = []

    def run(self, args, *, label, check=True, capture=True, timeout=60):  # noqa: ANN001, ANN201
        command = [str(arg) for arg in args]
        self.calls.append((label, command))
        if label in gateway_deploy_guard.MUTATING_LABELS and not self.apply:
            self.dry_run_log.append(command)
            return gateway_deploy_guard.CommandResult(command, 0, "DRY-RUN: not executed\n", "")
        if label == "git-current-branch":
            return gateway_deploy_guard.CommandResult(command, 0, "main\n", "")
        if label == "git-current-commit":
            return gateway_deploy_guard.CommandResult(command, 0, "prevsha\n", "")
        if label == "git-symbolic-ref":
            return gateway_deploy_guard.CommandResult(command, 0, "refs/heads/main\n", "")
        if label == "git-resolve-target":
            return gateway_deploy_guard.CommandResult(command, 0, "targetsha\n", "")
        if label == "service-restart-count":
            return gateway_deploy_guard.CommandResult(command, 0, "3\n", "")
        if label == "service-active":
            return gateway_deploy_guard.CommandResult(command, 0 if self.active else 3, "active\n" if self.active else "failed\n", "")
        if label == "service-details":
            state = "active" if self.active else "failed"
            return gateway_deploy_guard.CommandResult(command, 0, f"ActiveState={state}\nSubState=running\nExecMainStatus=0\nNRestarts=3\n", "")
        if label == "git-show-ref":
            return gateway_deploy_guard.CommandResult(command, 0, "targetsha HEAD\nprevsha refs/heads/main\n", "")
        if label == "git-status":
            return gateway_deploy_guard.CommandResult(command, 0, "## main\n", "")
        if label == "journal-tail":
            return gateway_deploy_guard.CommandResult(command, 0, "TOKEN=super-secret\nBearer abcdefghijklmnop\n", "")
        return gateway_deploy_guard.CommandResult(command, 0, "", "")


class RestartTimeoutRunner(FakeRunner):
    def run(self, args, *, label, check=True, capture=True, timeout=60):  # noqa: ANN001, ANN201
        command = [str(arg) for arg in args]
        if label == "restart-service":
            self.calls.append((label, command))
            raise gateway_deploy_guard.subprocess.TimeoutExpired(command, timeout or 60)
        return super().run(args, label=label, check=check, capture=capture, timeout=timeout)


def cfg(tmp_path: Path, *, apply: bool = False) -> Any:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "gateway.log").write_text("gateway ok\nOPENAI_API_KEY=sk-should-redact\n", encoding="utf-8")
    (log_dir / "errors.log").write_text("no errors\n", encoding="utf-8")
    return gateway_deploy_guard.DeployConfig(
        repo=tmp_path / "repo",
        target="target-ref",
        service="hermes-gateway.service",
        log_dir=log_dir,
        incident_dir=tmp_path / "incidents",
        record_dir=tmp_path / "records",
        apply=apply,
        health_timeout=0,
        health_interval=0,
        settle_seconds=0,
    )


def test_dry_run_skips_checkout_and_restart(tmp_path):
    runner = FakeRunner(apply=False, active=True)

    code = gateway_deploy_guard.deploy(cfg(tmp_path, apply=False), runner=runner, sleeper=lambda _: None)

    assert code == 0
    assert [call[0] for call in runner.calls].count("checkout-target") == 1
    assert [call[0] for call in runner.calls].count("restart-service") == 1
    assert runner.dry_run_log == [
        ["git", "-C", str(tmp_path / "repo"), "checkout", "--detach", "targetsha"],
        ["systemctl", "--user", "restart", "hermes-gateway.service"],
    ]


def test_apply_health_failure_collects_bundle_and_rolls_back(tmp_path):
    runner = FakeRunner(apply=True, active=False)

    code = gateway_deploy_guard.deploy(cfg(tmp_path, apply=True), runner=runner, sleeper=lambda _: None)

    assert code == 1
    assert ("rollback-checkout", ["git", "-C", str(tmp_path / "repo"), "checkout", "main"]) in runner.calls
    restart_calls = [call for call in runner.calls if call[0] == "restart-service"]
    assert restart_calls == [
        ("restart-service", ["systemctl", "--user", "restart", "hermes-gateway.service"]),
        ("restart-service", ["systemctl", "--user", "restart", "hermes-gateway.service"]),
    ]
    bundles = list((tmp_path / "incidents").glob("gateway-deploy-*"))
    assert len(bundles) == 1
    assert (bundles[0] / "metadata.json").exists()
    journal = (bundles[0] / "journalctl.txt").read_text(encoding="utf-8")
    gateway_log = (bundles[0] / "gateway.log.tail.txt").read_text(encoding="utf-8")
    assert "super-secret" not in journal
    assert "abcdefghijklmnop" not in journal
    assert "sk-should-redact" not in gateway_log
    assert "[REDACTED]" in journal


def test_redact_authorization_bearer_and_quoted_secrets():
    raw = "\n".join(
        [
            "Authorization: Bearer abc",
            'TOKEN="secret-token"',
            'API_KEY: "secret-key"',
            "PASSWORD='secret-password'",
            "plain=ok",
        ]
    )

    redacted = gateway_deploy_guard.redact(raw)

    assert "abc" not in redacted
    assert "secret-token" not in redacted
    assert "secret-key" not in redacted
    assert "secret-password" not in redacted
    assert "Authorization: Bearer [REDACTED]" in redacted
    assert 'TOKEN="[REDACTED]"' in redacted
    assert 'API_KEY: "[REDACTED]"' in redacted
    assert "PASSWORD='[REDACTED]'" in redacted
    assert "plain=ok" in redacted


def test_success_writes_deploy_audit_record_with_refs_and_mode(tmp_path):
    runner = FakeRunner(apply=False, active=True)

    code = gateway_deploy_guard.deploy(cfg(tmp_path, apply=False), runner=runner, sleeper=lambda _: None)

    assert code == 0
    records = list((tmp_path / "records").glob("gateway-deploy-*.json"))
    assert len(records) == 1
    record = gateway_deploy_guard.json.loads(records[0].read_text(encoding="utf-8"))
    assert record["mode"] == "dry-run"
    assert record["status"] == "success"
    assert record["previous"]["commit"] == "prevsha"
    assert record["current"]["commit"] == "prevsha"
    assert record["target"]["ref"] == "target-ref"
    assert record["target"]["commit"] == "targetsha"
    assert record["restart_mode"] == "restart"


def test_rollback_health_result_and_command_results_are_recorded(tmp_path):
    runner = FakeRunner(apply=True, active=False)

    code = gateway_deploy_guard.deploy(cfg(tmp_path, apply=True), runner=runner, sleeper=lambda _: None)

    assert code == 1
    bundle = next((tmp_path / "incidents").glob("gateway-deploy-*"))
    metadata = gateway_deploy_guard.json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
    rollback = metadata["rollback"]
    assert rollback["checkout"]["returncode"] == 0
    assert rollback["restart"]["returncode"] == 0
    assert rollback["post_rollback_health"]["healthy"] is False
    assert "failed" in rollback["post_rollback_health"]["detail"]


def test_restart_timeout_during_deploy_and_rollback_writes_incident_and_audit(tmp_path):
    runner = RestartTimeoutRunner(apply=True, active=True)

    code = gateway_deploy_guard.deploy(cfg(tmp_path, apply=True), runner=runner, sleeper=lambda _: None)

    assert code == 1
    restart_calls = [call for call in runner.calls if call[0] == "restart-service"]
    assert restart_calls == [
        ("restart-service", ["systemctl", "--user", "restart", "hermes-gateway.service"]),
        ("restart-service", ["systemctl", "--user", "restart", "hermes-gateway.service"]),
    ]
    bundle = next((tmp_path / "incidents").glob("gateway-deploy-*"))
    metadata = gateway_deploy_guard.json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["reason"] == "TimeoutExpired"
    assert "rollback" in metadata
    assert metadata["rollback"]["checkout"]["returncode"] == 0
    assert metadata["rollback"]["restart"]["returncode"] == 124
    assert "timed out" in metadata["rollback"]["restart"]["stderr"]

    records = list((tmp_path / "records").glob("gateway-deploy-*.json"))
    assert len(records) == 1
    record = gateway_deploy_guard.json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "failure"
    assert record["incident_bundle"] == str(bundle)
    assert record["rollback"]["restart"]["returncode"] == 124


def test_runner_converts_subprocess_timeout_to_command_result(monkeypatch):
    def fake_run(*args, **kwargs):  # noqa: ANN001, ANN202
        raise gateway_deploy_guard.subprocess.TimeoutExpired(args[0], kwargs["timeout"], output="out", stderr="err")

    monkeypatch.setattr(gateway_deploy_guard.subprocess, "run", fake_run)
    runner = gateway_deploy_guard.Runner(apply=True)

    result = runner.run(["systemctl", "--user", "restart", "hermes-gateway.service"], label="restart-service", check=False)

    assert result.returncode == 124
    assert result.stdout == "out"
    assert "err" in result.stderr
    assert "timed out after 60 seconds" in result.stderr


def test_argument_parser_defaults_to_dry_run(tmp_path):
    parsed = gateway_deploy_guard.parse_args(["--repo", str(tmp_path), "--target", "abc123"])

    assert parsed.apply is False
    assert parsed.repo == tmp_path
    assert parsed.service == "hermes-gateway.service"
    assert parsed.target == "abc123"
