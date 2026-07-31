"""External Google Workspace agent provider tool.

This tool lets Hermes delegate a bounded Workspace task to a separately
authenticated local CLI, such as Antigravity CLI configured with Google
Workspace MCP servers. Hermes does not read OAuth tokens directly; it only
launches the configured CLI with an isolated HOME/env.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.redact import redact_sensitive_text
from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error, tool_result


_DEV_ENVS = {"dev", "development", "staging", "stage", "test", "testing"}
_CONFIG_PREFIXES = (
    "HERMES_EXTERNAL_WORKSPACE_",
    "HERMES_GEMINI_SUBSCRIPTION_",
    "HERMES_ANTIGRAVITY_WORKSPACE_",
)
_DEFAULT_PROMPT_ARG = "-p"
_DEFAULT_TIMEOUT_SECONDS = 90
_DEFAULT_MAX_OUTPUT_CHARS = 12000
_SAFE_BASE_ENV_KEYS = {
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "TERM",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}
_WRITE_TASK_RE = re.compile(
    r"(?i)\b(create|add|delete|remove|cancel|reschedule|move|update|send|reply|rsvp|accept|decline)\b"
    r"|(?:\b|^)(создай|добавь|удали|отмени|перенеси|измени|ответь|отправь)(?:\b|$)"
)


WORKSPACE_EXTERNAL_PROVIDER_SCHEMA = {
    "name": "workspace_external_provider",
    "description": (
        "Delegate Google Workspace questions to a configured dev/staging external "
        "agent provider, such as Antigravity CLI with Google Workspace MCP. Use this "
        "for live Calendar/Gmail/Drive data when the user asks to read their plans, "
        "meetings, email, or files. The provider is read-only by default and is only "
        "available when explicitly enabled in local .secrets config."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["ask", "status"],
                "description": "Use 'ask' to delegate a task, or 'status' to inspect provider availability.",
                "default": "ask",
            },
            "task": {
                "type": "string",
                "description": (
                    "Natural-language task to delegate. Example: "
                    "'Read my Google Calendar for today and summarize all plans.'"
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["calendar", "gmail", "drive", "contacts", "workspace", "other"],
                "description": "Workspace area the external agent should focus on.",
                "default": "workspace",
            },
            "read_only": {
                "type": "boolean",
                "description": "Keep true unless the user explicitly asked for a write and writes are enabled.",
                "default": True,
            },
            "output_format": {
                "type": "string",
                "enum": ["json", "text"],
                "description": "Preferred output shape from the external provider.",
                "default": "json",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 5,
                "maximum": 300,
                "description": "Optional per-call timeout.",
            },
        },
        "required": [],
    },
}


@dataclass
class WorkspaceExternalConfig:
    env_file: Path | None
    file_env: dict[str, str]
    merged_env: dict[str, str]
    enabled: bool
    env_name: str
    provider: str
    command: str
    args_template: str
    command_template: str
    prompt_stdin: bool
    inherit_env: bool
    home_dir: Path
    cwd: Path
    timeout_seconds: int
    max_output_chars: int
    allow_writes: bool


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _first_nonempty(env: dict[str, str], *keys: str, default: str = "") -> str:
    for key in keys:
        value = str(env.get(key) or "").strip()
        if value:
            return value
    return default


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _candidate_env_files() -> list[Path]:
    explicit = os.getenv("HERMES_EXTERNAL_WORKSPACE_ENV_FILE", "").strip()
    if explicit:
        return [Path(explicit).expanduser()]

    names = [
        ".env.antigravity-workspace.local",
        ".env.gemini-subscription.local",
        ".env.workspace-external.local",
    ]
    roots = [
        _project_root() / ".secrets",
        get_hermes_home() / ".secrets",
    ]
    return [root / name for root in roots for name in names]


def _load_file_env() -> tuple[Path | None, dict[str, str]]:
    for path in _candidate_env_files():
        expanded = path.expanduser()
        if expanded.exists():
            return expanded, _parse_env_file(expanded)
    return None, {}


def _merged_env(file_env: dict[str, str]) -> dict[str, str]:
    merged = dict(file_env)
    for key, value in os.environ.items():
        if key.startswith(_CONFIG_PREFIXES) or key in {
            "HERMES_ENV",
            "HERMES_ENVIRONMENT",
            "APP_ENV",
            "ENVIRONMENT",
        }:
            merged[key] = value
    return merged


def _expand_path(value: str, *, base: Path) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(value)))
    if not p.is_absolute():
        p = base / p
    return p


def _int_env(env: dict[str, str], key: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(str(env.get(key) or "").strip())
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def _bool_arg(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _load_config() -> WorkspaceExternalConfig:
    env_file, file_env = _load_file_env()
    env = _merged_env(file_env)
    root = _project_root()

    env_name = _first_nonempty(
        env,
        "HERMES_EXTERNAL_WORKSPACE_ENV",
        "HERMES_ENV",
        "HERMES_ENVIRONMENT",
        "APP_ENV",
        "ENVIRONMENT",
    ).lower()
    enabled = any(
        _truthy(env.get(key))
        for key in (
            "HERMES_EXTERNAL_WORKSPACE_ENABLED",
            "HERMES_GEMINI_SUBSCRIPTION_ENABLED",
            "HERMES_ANTIGRAVITY_WORKSPACE_ENABLED",
        )
    )
    provider = _first_nonempty(
        env,
        "HERMES_EXTERNAL_WORKSPACE_PROVIDER",
        "HERMES_GEMINI_SUBSCRIPTION_PROVIDER",
        default="auto",
    ).lower()

    command = _first_nonempty(
        env,
        "HERMES_EXTERNAL_WORKSPACE_CMD",
        "HERMES_EXTERNAL_WORKSPACE_COMMAND_BIN",
        "HERMES_ANTIGRAVITY_CMD",
        "ANTIGRAVITY_CMD",
        "HERMES_GEMINI_SUBSCRIPTION_CMD",
        "GEMINI_SUBSCRIPTION_CMD",
    )
    if not command:
        for candidate in ("agy", "antigravity", "gemini"):
            if shutil.which(candidate):
                command = candidate
                break
    if not command:
        command = "agy"

    args_template = _first_nonempty(
        env,
        "HERMES_EXTERNAL_WORKSPACE_ARGS",
        "HERMES_ANTIGRAVITY_ARGS",
        "HERMES_GEMINI_SUBSCRIPTION_ARGS",
        default=_DEFAULT_PROMPT_ARG,
    )
    command_template = _first_nonempty(
        env,
        "HERMES_EXTERNAL_WORKSPACE_COMMAND",
        "HERMES_ANTIGRAVITY_COMMAND",
        "HERMES_GEMINI_SUBSCRIPTION_COMMAND",
    )
    default_home = root / ".secrets" / "antigravity-workspace" / "home"
    home_value = _first_nonempty(
        env,
        "HERMES_EXTERNAL_WORKSPACE_HOME",
        "HERMES_ANTIGRAVITY_HOME",
        "HERMES_GEMINI_SUBSCRIPTION_HOME",
        default=str(default_home),
    )
    cwd_value = _first_nonempty(
        env,
        "HERMES_EXTERNAL_WORKSPACE_CWD",
        "HERMES_ANTIGRAVITY_CWD",
        "HERMES_GEMINI_SUBSCRIPTION_CWD",
        default=str(root),
    )

    return WorkspaceExternalConfig(
        env_file=env_file,
        file_env=file_env,
        merged_env=env,
        enabled=enabled,
        env_name=env_name,
        provider=provider,
        command=command,
        args_template=args_template,
        command_template=command_template,
        prompt_stdin=_truthy(env.get("HERMES_EXTERNAL_WORKSPACE_STDIN")),
        inherit_env=_truthy(env.get("HERMES_EXTERNAL_WORKSPACE_INHERIT_ENV")),
        home_dir=_expand_path(home_value, base=root),
        cwd=_expand_path(cwd_value, base=root),
        timeout_seconds=_int_env(
            env,
            "HERMES_EXTERNAL_WORKSPACE_TIMEOUT",
            _DEFAULT_TIMEOUT_SECONDS,
            min_value=5,
            max_value=300,
        ),
        max_output_chars=_int_env(
            env,
            "HERMES_EXTERNAL_WORKSPACE_MAX_OUTPUT_CHARS",
            _DEFAULT_MAX_OUTPUT_CHARS,
            min_value=1000,
            max_value=100000,
        ),
        allow_writes=_truthy(env.get("HERMES_EXTERNAL_WORKSPACE_ALLOW_WRITES")),
    )


def _executable_path(command: str, cfg: WorkspaceExternalConfig) -> str | None:
    command = command.strip()
    if not command:
        return None
    expanded = os.path.expandvars(os.path.expanduser(command))
    if os.sep in expanded or (os.altsep and os.altsep in expanded):
        p = Path(expanded)
        return str(p) if p.exists() and os.access(p, os.X_OK) else None
    path = cfg.file_env.get("PATH") or os.environ.get("PATH")
    return shutil.which(expanded, path=path)


def _availability(cfg: WorkspaceExternalConfig | None = None) -> dict[str, Any]:
    cfg = cfg or _load_config()
    reasons: list[str] = []
    if not cfg.enabled:
        reasons.append("Set HERMES_EXTERNAL_WORKSPACE_ENABLED=1 in .secrets env.")
    if cfg.env_name not in _DEV_ENVS:
        reasons.append("Set HERMES_EXTERNAL_WORKSPACE_ENV=dev or staging.")
    exe = None
    command_label = cfg.command
    if cfg.command_template:
        try:
            tokens = shlex.split(cfg.command_template)
        except ValueError as exc:
            tokens = []
            reasons.append(f"Invalid HERMES_EXTERNAL_WORKSPACE_COMMAND: {exc}")
        first_token = tokens[0] if tokens else ""
        command_label = cfg.command_template
        exe = _executable_path(first_token, cfg) if first_token else None
        if first_token and not exe:
            reasons.append(f"External workspace command template executable not found: {first_token}")
        elif not first_token and not reasons:
            reasons.append("HERMES_EXTERNAL_WORKSPACE_COMMAND is empty.")
    else:
        exe = _executable_path(cfg.command, cfg)
        if not exe:
            reasons.append(f"External workspace CLI not found or not executable: {cfg.command}")
    return {
        "available": not reasons,
        "reasons": reasons,
        "enabled": cfg.enabled,
        "env": cfg.env_name,
        "provider": cfg.provider,
        "env_file": str(cfg.env_file) if cfg.env_file else None,
        "command": command_label,
        "command_found": bool(exe),
        "home_dir": str(cfg.home_dir),
        "cwd": str(cfg.cwd),
        "allow_writes": cfg.allow_writes,
    }


def check_workspace_external_provider_requirements() -> bool:
    return bool(_availability().get("available"))


def _tokens_with_prompt(tokens: list[str], prompt: str, *, append_if_missing: bool) -> list[str]:
    replaced = False
    result: list[str] = []
    for token in tokens:
        if "{prompt}" in token:
            result.append(token.replace("{prompt}", prompt))
            replaced = True
        else:
            result.append(token)
    if append_if_missing and not replaced:
        result.append(prompt)
    return result


def _build_command(cfg: WorkspaceExternalConfig, prompt: str) -> list[str]:
    if cfg.command_template:
        tokens = shlex.split(cfg.command_template)
        tokens = _tokens_with_prompt(tokens, prompt, append_if_missing=not cfg.prompt_stdin)
        if not tokens:
            raise ValueError("HERMES_EXTERNAL_WORKSPACE_COMMAND produced an empty command")
        exe = _executable_path(tokens[0], cfg)
        if not exe:
            raise FileNotFoundError(f"External workspace command not found: {tokens[0]}")
        return [exe, *tokens[1:]]

    exe = _executable_path(cfg.command, cfg)
    if not exe:
        raise FileNotFoundError(f"External workspace CLI not found: {cfg.command}")
    args = shlex.split(cfg.args_template)
    args = _tokens_with_prompt(args, prompt, append_if_missing=not cfg.prompt_stdin)
    return [exe, *args]


def _build_child_env(cfg: WorkspaceExternalConfig) -> dict[str, str]:
    if cfg.inherit_env:
        child_env = dict(os.environ)
    else:
        child_env = {
            key: value
            for key, value in os.environ.items()
            if key in _SAFE_BASE_ENV_KEYS and value
        }

    for key, value in cfg.file_env.items():
        if any(key.startswith(prefix) for prefix in _CONFIG_PREFIXES):
            continue
        child_env[key] = value

    child_env["HOME"] = str(cfg.home_dir)
    child_env.setdefault("PATH", os.environ.get("PATH", ""))
    child_env.setdefault("LANG", "C.UTF-8")
    return child_env


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n...[truncated {omitted} chars]"


def _looks_like_write_task(task: str) -> bool:
    return bool(_WRITE_TASK_RE.search(task or ""))


def _build_provider_prompt(
    *,
    task: str,
    scope: str,
    read_only: bool,
    output_format: str,
) -> str:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    mode_line = (
        "READ-ONLY MODE: do not create, update, delete, send, reply, RSVP, or change anything."
        if read_only
        else "WRITE MODE: only perform the exact user-requested change; do not make extra changes."
    )
    format_line = (
        "Return compact JSON only, with keys: answer, items, source, limitations."
        if output_format == "json"
        else "Return concise plain text."
    )
    return "\n".join(
        [
            "You are an external Google Workspace provider invoked by Hermes.",
            "Use the Google Workspace tools already configured in this CLI/MCP environment.",
            mode_line,
            f"Scope: {scope}.",
            f"Current datetime: {now}.",
            format_line,
            "If you cannot access the requested data, explain the missing authorization or tool.",
            "",
            "User task:",
            task.strip(),
        ]
    )


def workspace_external_provider_tool(args: dict, **_: Any) -> str:
    action = str(args.get("action") or "ask").strip().lower()
    cfg = _load_config()
    status = _availability(cfg)
    if action == "status":
        return tool_result(status)
    if action != "ask":
        return tool_error(f"Unknown action: {action}")
    if not status["available"]:
        return tool_error(
            "External workspace provider is not available.",
            status=status,
        )

    task = str(args.get("task") or "").strip()
    if not task:
        return tool_error("task is required when action='ask'")

    scope = str(args.get("scope") or "workspace").strip().lower()
    if scope not in {"calendar", "gmail", "drive", "contacts", "workspace", "other"}:
        scope = "workspace"
    output_format = str(args.get("output_format") or "json").strip().lower()
    if output_format not in {"json", "text"}:
        output_format = "json"
    read_only = _bool_arg(args.get("read_only"), True)
    if read_only and _looks_like_write_task(task):
        return tool_error(
            "Refused a write-shaped task in read-only mode. Use a dedicated write flow "
            "or explicitly set read_only=false with HERMES_EXTERNAL_WORKSPACE_ALLOW_WRITES=1."
        )
    if not read_only and not cfg.allow_writes:
        return tool_error("Writes are disabled. Set HERMES_EXTERNAL_WORKSPACE_ALLOW_WRITES=1 to opt in.")

    timeout = args.get("timeout_seconds")
    try:
        timeout_seconds = int(timeout) if timeout is not None else cfg.timeout_seconds
    except (TypeError, ValueError):
        timeout_seconds = cfg.timeout_seconds
    timeout_seconds = max(5, min(300, timeout_seconds))

    prompt = _build_provider_prompt(
        task=task,
        scope=scope,
        read_only=read_only,
        output_format=output_format,
    )
    try:
        cfg.home_dir.mkdir(parents=True, exist_ok=True)
        try:
            cfg.home_dir.chmod(0o700)
        except OSError:
            pass
        cfg.cwd.mkdir(parents=True, exist_ok=True)
        command = _build_command(cfg, prompt)
        child_env = _build_child_env(cfg)
        started = time.monotonic()
        proc = subprocess.run(
            command,
            input=prompt if cfg.prompt_stdin else None,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            cwd=str(cfg.cwd),
            env=child_env,
            check=False,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
    except subprocess.TimeoutExpired as exc:
        return tool_error(
            f"External workspace provider timed out after {timeout_seconds}s.",
            stdout=redact_sensitive_text(exc.stdout or ""),
            stderr=redact_sensitive_text(exc.stderr or ""),
        )
    except Exception as exc:
        return tool_error(f"External workspace provider failed to start: {exc}")

    stdout = _truncate(redact_sensitive_text(proc.stdout or ""), cfg.max_output_chars)
    stderr = _truncate(redact_sensitive_text(proc.stderr or ""), min(cfg.max_output_chars, 4000))
    result = {
        "success": proc.returncode == 0,
        "provider": cfg.provider,
        "scope": scope,
        "read_only": read_only,
        "returncode": proc.returncode,
        "elapsed_ms": elapsed_ms,
        "output": stdout,
    }
    if stderr:
        result["stderr"] = stderr
    if proc.returncode != 0:
        result["error"] = "External workspace provider exited non-zero."
    return tool_result(result)


registry.register(
    name="workspace_external_provider",
    toolset="workspace_external",
    schema=WORKSPACE_EXTERNAL_PROVIDER_SCHEMA,
    handler=workspace_external_provider_tool,
    check_fn=check_workspace_external_provider_requirements,
    requires_env=["HERMES_EXTERNAL_WORKSPACE_ENABLED"],
    emoji="",
    max_result_size_chars=20000,
)
