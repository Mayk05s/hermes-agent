#!/usr/bin/env python3
"""Safe deploy wrapper for the Hermes gateway systemd user service.

The wrapper is intentionally dry-run by default. It records the current git ref,
checks out a target ref, restarts the gateway, waits for systemd health, and
rolls back to the previous ref if the service fails to become healthy. Mutating
steps only run when --apply is provided.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

DEFAULT_REPO = Path("/home/hermes/.hermes/hermes-agent")
DEFAULT_SERVICE = "hermes-gateway.service"
DEFAULT_LOG_DIR = Path.home() / ".hermes" / "logs"
DEFAULT_INCIDENT_DIR = Path.home() / ".hermes" / "deploy-incidents"
DEFAULT_RECORD_DIR = DEFAULT_LOG_DIR / "gateway-deploys"
MUTATING_LABELS = {"checkout-target", "restart-service", "rollback-checkout", "reload-service"}

SECRET_KEY_RE = r"[A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|AUTH)[A-Z0-9_]*"
AUTHORIZATION_BEARER_PATTERN = re.compile(r"(?i)\b(Authorization\s*[:=]\s*)([\"']?)(Bearer\s+)([^\s,}\]\"']+)(\2)")
QUOTED_SECRET_PATTERN = re.compile(rf"(?i)\b(?!Authorization\b)({SECRET_KEY_RE})(\s*[:=]\s*)([\"'])([^\"']*)(\3)")
UNQUOTED_SECRET_PATTERN = re.compile(rf"(?i)\b(?!Authorization\b)({SECRET_KEY_RE})(\s*[:=]\s*)([^\s,}}\]\"']+)")
BEARER_PATTERN = re.compile(r"(?i)\b(Bearer\s+)([^\s,}\]\"']+)")
OPENAI_KEY_PATTERN = re.compile(r"\b(sk-[A-Za-z0-9_-]{12,})")


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandError(RuntimeError):
    def __init__(self, label: str, result: CommandResult):
        self.label = label
        self.result = result
        super().__init__(f"{label} failed with exit code {result.returncode}: {result.stderr.strip()}")


def timeout_stream_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def command_timeout_result(exc: subprocess.TimeoutExpired, command: list[str] | None = None) -> CommandResult:
    args = [str(arg) for arg in (command or exc.cmd)]
    stdout = timeout_stream_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
    stderr = timeout_stream_text(getattr(exc, "stderr", None))
    message = f"Command {args!r} timed out after {exc.timeout} seconds"
    if stderr:
        stderr = f"{stderr.rstrip()}\n{message}\n"
    else:
        stderr = f"{message}\n"
    return CommandResult(args, 124, stdout, stderr)


@dataclass
class Runner:
    apply: bool = False
    dry_run_log: list[list[str]] = field(default_factory=list)

    def run(
        self,
        args: Sequence[str],
        *,
        label: str,
        check: bool = True,
        capture: bool = True,
        timeout: int | None = 60,
    ) -> CommandResult:
        command = [str(arg) for arg in args]
        if label in MUTATING_LABELS and not self.apply:
            self.dry_run_log.append(command)
            return CommandResult(command, 0, "DRY-RUN: not executed\n", "")

        try:
            completed = subprocess.run(
                command,
                check=False,
                text=True,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            result = command_timeout_result(exc, command)
            if check:
                raise CommandError(label, result) from exc
            return result
        result = CommandResult(command, completed.returncode, completed.stdout or "", completed.stderr or "")
        if check and result.returncode != 0:
            raise CommandError(label, result)
        return result


@dataclass
class DeployConfig:
    repo: Path
    target: str
    service: str = DEFAULT_SERVICE
    log_dir: Path = DEFAULT_LOG_DIR
    incident_dir: Path = DEFAULT_INCIDENT_DIR
    record_dir: Path = DEFAULT_RECORD_DIR
    apply: bool = False
    health_timeout: float = 45.0
    health_interval: float = 2.0
    settle_seconds: float = 5.0
    restart_mode: str = "restart"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact(text: str) -> str:
    """Redact credentials from deploy logs while preserving useful context."""
    redacted = text
    redacted = AUTHORIZATION_BEARER_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]{m.group(5)}", redacted)
    redacted = QUOTED_SECRET_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]{m.group(5)}", redacted)
    redacted = UNQUOTED_SECRET_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", redacted)
    redacted = BEARER_PATTERN.sub(lambda m: f"{m.group(1)}[REDACTED]", redacted)
    redacted = OPENAI_KEY_PATTERN.sub("[REDACTED_SECRET]", redacted)
    return redacted


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(text), encoding="utf-8")


def write_json(path: Path, data: dict[str, object]) -> None:
    write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def command_result_metadata(result: CommandResult) -> dict[str, object]:
    return {
        "args": result.args,
        "returncode": result.returncode,
        "stdout": redact(result.stdout),
        "stderr": redact(result.stderr),
    }


def run_no_raise(
    runner: Runner,
    args: Sequence[str],
    *,
    label: str,
    capture: bool = True,
    timeout: int | None = 60,
) -> CommandResult:
    command = [str(arg) for arg in args]
    try:
        return runner.run(command, label=label, check=False, capture=capture, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return command_timeout_result(exc, command)
    except CommandError as exc:
        return exc.result


def deploy_record_path(record_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return record_dir / f"gateway-deploy-{stamp}-{os.getpid()}.json"


def write_deploy_audit_record(
    cfg: DeployConfig,
    *,
    previous: dict[str, str],
    current: dict[str, str],
    target_commit: str,
    status: str,
    health: dict[str, object] | None = None,
    error: str | None = None,
    incident_bundle: Path | None = None,
    rollback: dict[str, object] | None = None,
) -> Path:
    record: dict[str, object] = {
        "created_at": utc_now(),
        "repo": str(cfg.repo),
        "service": cfg.service,
        "mode": "apply" if cfg.apply else "dry-run",
        "apply": cfg.apply,
        "restart_mode": cfg.restart_mode,
        "status": status,
        "previous": previous,
        "current": current,
        "target": {"ref": cfg.target, "commit": target_commit},
    }
    if health is not None:
        record["health"] = health
    if error is not None:
        record["error"] = redact(error)
    if incident_bundle is not None:
        record["incident_bundle"] = str(incident_bundle)
    if rollback is not None:
        record["rollback"] = rollback
    path = deploy_record_path(cfg.record_dir)
    write_json(path, record)
    return path


def git(runner: Runner, repo: Path, args: Sequence[str], *, label: str, check: bool = True) -> CommandResult:
    return runner.run(["git", "-C", str(repo), *args], label=label, check=check)


def current_ref(runner: Runner, repo: Path) -> dict[str, str]:
    branch = git(runner, repo, ["branch", "--show-current"], label="git-current-branch", check=False).stdout.strip()
    commit = git(runner, repo, ["rev-parse", "HEAD"], label="git-current-commit").stdout.strip()
    symbolic = git(runner, repo, ["symbolic-ref", "-q", "HEAD"], label="git-symbolic-ref", check=False).stdout.strip()
    return {"branch": branch, "commit": commit, "symbolic_ref": symbolic, "rollback_ref": branch or commit}


def resolve_target(runner: Runner, repo: Path, target: str) -> str:
    return git(runner, repo, ["rev-parse", "--verify", f"{target}^{{commit}}"], label="git-resolve-target").stdout.strip()


def service_restart_count(runner: Runner, service: str) -> int | None:
    result = runner.run(
        ["systemctl", "--user", "show", service, "-p", "NRestarts", "--value"],
        label="service-restart-count",
        check=False,
    )
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def service_is_healthy(runner: Runner, service: str, baseline_restarts: int | None) -> tuple[bool, str]:
    active = runner.run(["systemctl", "--user", "is-active", service], label="service-active", check=False)
    details = runner.run(
        ["systemctl", "--user", "show", service, "-p", "ActiveState", "-p", "SubState", "-p", "ExecMainStatus", "-p", "NRestarts"],
        label="service-details",
        check=False,
    )
    body = f"is-active: {active.stdout.strip()}\n{details.stdout}"
    if active.stdout.strip() != "active":
        return False, body
    current_restarts = service_restart_count(runner, service)
    if baseline_restarts is not None and current_restarts is not None and current_restarts > baseline_restarts:
        return False, body + f"\nrestart count increased from {baseline_restarts} to {current_restarts}"
    return True, body


def wait_for_health(runner: Runner, service: str, timeout: float, interval: float, baseline_restarts: int | None) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last_detail = ""
    while True:
        healthy, detail = service_is_healthy(runner, service, baseline_restarts)
        last_detail = detail
        if healthy:
            return True, detail
        if time.monotonic() >= deadline:
            return False, last_detail
        time.sleep(interval)


def tail_file(path: Path, max_bytes: int = 128_000) -> str:
    if not path.exists():
        return f"{path} does not exist\n"
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes), os.SEEK_SET)
        return handle.read().decode("utf-8", errors="replace")


def collect_incident_bundle(
    cfg: DeployConfig,
    runner: Runner,
    previous: dict[str, str],
    target_commit: str,
    reason: str,
    health_detail: str = "",
    rollback: dict[str, object] | None = None,
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle = cfg.incident_dir / f"gateway-deploy-{stamp}"
    bundle.mkdir(parents=True, exist_ok=True)
    metadata = {
        "created_at": utc_now(),
        "reason": reason,
        "repo": str(cfg.repo),
        "service": cfg.service,
        "apply": cfg.apply,
        "previous": previous,
        "target": cfg.target,
        "target_commit": target_commit,
    }
    if rollback is not None:
        metadata["rollback"] = rollback
    write_json(bundle / "metadata.json", metadata)
    write_text(bundle / "health.txt", health_detail)

    refs = git(runner, cfg.repo, ["show-ref", "--head"], label="git-show-ref", check=False)
    status = git(runner, cfg.repo, ["status", "--short", "--branch"], label="git-status", check=False)
    write_text(bundle / "git_refs.txt", f"# git status\n{status.stdout}{status.stderr}\n# git show-ref\n{refs.stdout}{refs.stderr}")

    journal = runner.run(
        ["journalctl", "--user-unit", cfg.service, "-n", "300", "--no-pager", "--output", "short-iso"],
        label="journal-tail",
        check=False,
        timeout=30,
    )
    write_text(bundle / "journalctl.txt", journal.stdout + journal.stderr)
    write_text(bundle / "gateway.log.tail.txt", tail_file(cfg.log_dir / "gateway.log"))
    write_text(bundle / "errors.log.tail.txt", tail_file(cfg.log_dir / "errors.log"))
    return bundle


def deploy(cfg: DeployConfig, runner: Runner | None = None, sleeper: Callable[[float], None] = time.sleep) -> int:
    runner = runner or Runner(apply=cfg.apply)
    previous = current_ref(runner, cfg.repo)
    target_commit = resolve_target(runner, cfg.repo, cfg.target)
    baseline_restarts = service_restart_count(runner, cfg.service)

    print(f"mode: {'APPLY' if cfg.apply else 'DRY-RUN'}")
    print(f"repo: {cfg.repo}")
    print(f"previous: {previous['rollback_ref']} ({previous['commit']})")
    print(f"target: {cfg.target} ({target_commit})")

    try:
        git(runner, cfg.repo, ["checkout", "--detach", target_commit], label="checkout-target")
        runner.run(["systemctl", "--user", cfg.restart_mode, cfg.service], label="restart-service")
        if cfg.settle_seconds > 0:
            sleeper(cfg.settle_seconds)
        healthy, detail = wait_for_health(runner, cfg.service, cfg.health_timeout, cfg.health_interval, baseline_restarts)
        health = {"healthy": healthy, "detail": redact(detail)}
        if not healthy:
            raise RuntimeError(f"health check failed\n{detail}")
        current = current_ref(runner, cfg.repo)
        record = write_deploy_audit_record(
            cfg,
            previous=previous,
            current=current,
            target_commit=target_commit,
            status="success",
            health=health,
        )
        print("health: ok")
        print(f"deploy audit record: {record}")
        if not cfg.apply:
            print("dry-run: mutating checkout/restart/rollback commands were not executed")
        return 0
    except Exception as exc:
        detail = str(exc)
        rollback_metadata: dict[str, object] | None = None
        if cfg.apply:
            rollback = previous["rollback_ref"]
            print(f"rolling back checkout to {rollback}", file=sys.stderr)
            checkout_result = run_no_raise(runner, ["git", "-C", str(cfg.repo), "checkout", rollback], label="rollback-checkout")
            restart_result = run_no_raise(runner, ["systemctl", "--user", cfg.restart_mode, cfg.service], label="restart-service")
            post_rollback_healthy, post_rollback_detail = service_is_healthy(runner, cfg.service, baseline_restarts)
            rollback_metadata = {
                "ref": rollback,
                "checkout": command_result_metadata(checkout_result),
                "restart": command_result_metadata(restart_result),
                "post_rollback_health": {
                    "healthy": post_rollback_healthy,
                    "detail": redact(post_rollback_detail),
                },
            }
        else:
            print("dry-run: rollback checkout/restart not executed", file=sys.stderr)
        bundle = collect_incident_bundle(
            cfg,
            runner,
            previous,
            target_commit,
            reason=type(exc).__name__,
            health_detail=detail,
            rollback=rollback_metadata,
        )
        current = current_ref(runner, cfg.repo)
        record = write_deploy_audit_record(
            cfg,
            previous=previous,
            current=current,
            target_commit=target_commit,
            status="failure",
            health={"healthy": False, "detail": redact(detail)},
            error=detail,
            incident_bundle=bundle,
            rollback=rollback_metadata,
        )
        print(f"deploy failed: {detail}", file=sys.stderr)
        print(f"incident bundle: {bundle}", file=sys.stderr)
        print(f"deploy audit record: {record}", file=sys.stderr)
        return 1


def parse_args(argv: Sequence[str]) -> DeployConfig:
    parser = argparse.ArgumentParser(
        description="Dry-run-by-default Hermes gateway deploy wrapper with health check and rollback.",
        epilog=(
            "Example dry-run: scripts/gateway_deploy_guard.py --target HEAD\n"
            "Example real deploy (operator only): scripts/gateway_deploy_guard.py --apply --target <commit-or-ref>"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO, help=f"gateway checkout path (default: {DEFAULT_REPO})")
    parser.add_argument("--target", required=True, help="commit/ref to deploy")
    parser.add_argument("--service", default=DEFAULT_SERVICE, help=f"systemd user unit (default: {DEFAULT_SERVICE})")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="directory containing gateway.log/errors.log")
    parser.add_argument("--incident-dir", type=Path, default=DEFAULT_INCIDENT_DIR, help="where sanitized incident bundles are written")
    parser.add_argument("--record-dir", type=Path, default=DEFAULT_RECORD_DIR, help="where sanitized deploy audit JSON records are written")
    parser.add_argument("--apply", action="store_true", help="execute checkout/restart/rollback; without this all destructive actions are skipped")
    parser.add_argument("--health-timeout", type=float, default=45.0, help="seconds to wait for healthy service")
    parser.add_argument("--health-interval", type=float, default=2.0, help="seconds between health probes")
    parser.add_argument("--settle-seconds", type=float, default=5.0, help="seconds to wait after restart before health polling")
    parser.add_argument("--restart-mode", choices=("restart", "reload-or-restart"), default="restart", help="systemctl action to use")
    ns = parser.parse_args(argv)
    return DeployConfig(
        repo=ns.repo,
        target=ns.target,
        service=ns.service,
        log_dir=ns.log_dir,
        incident_dir=ns.incident_dir,
        record_dir=ns.record_dir,
        apply=ns.apply,
        health_timeout=ns.health_timeout,
        health_interval=ns.health_interval,
        settle_seconds=ns.settle_seconds,
        restart_mode=ns.restart_mode,
    )


def main(argv: Sequence[str] | None = None) -> int:
    cfg = parse_args(argv or sys.argv[1:])
    return deploy(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
