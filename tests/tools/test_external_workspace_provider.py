import json
import subprocess
from pathlib import Path

from tools import external_workspace_provider as workspace


def _write_env(path: Path, body: str) -> Path:
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def test_external_workspace_provider_requires_dev_or_staging(monkeypatch, tmp_path):
    env_file = _write_env(
        tmp_path / ".env.gemini-subscription.local",
        """
        HERMES_EXTERNAL_WORKSPACE_ENABLED=1
        HERMES_EXTERNAL_WORKSPACE_ENV=production
        HERMES_EXTERNAL_WORKSPACE_CMD=agy
        """,
    )
    monkeypatch.setenv("HERMES_EXTERNAL_WORKSPACE_ENV_FILE", str(env_file))
    monkeypatch.setattr(workspace.shutil, "which", lambda cmd, path=None: "/usr/bin/agy")

    status = workspace._availability()

    assert status["available"] is False
    assert any("dev or staging" in reason for reason in status["reasons"])


def test_command_template_does_not_require_default_agy(monkeypatch, tmp_path):
    env_file = _write_env(
        tmp_path / ".env.gemini-subscription.local",
        """
        HERMES_EXTERNAL_WORKSPACE_ENABLED=1
        HERMES_EXTERNAL_WORKSPACE_ENV=dev
        HERMES_EXTERNAL_WORKSPACE_COMMAND=workspace-agent --prompt {prompt}
        """,
    )
    monkeypatch.setenv("HERMES_EXTERNAL_WORKSPACE_ENV_FILE", str(env_file))

    def fake_which(cmd, path=None):
        return "/usr/local/bin/workspace-agent" if cmd == "workspace-agent" else None

    monkeypatch.setattr(workspace.shutil, "which", fake_which)

    status = workspace._availability()

    assert status["available"] is True
    assert status["command_found"] is True
    assert status["command"] == "workspace-agent --prompt {prompt}"


def test_external_workspace_provider_runs_cli_without_shell(monkeypatch, tmp_path):
    home_dir = tmp_path / "home"
    cwd = tmp_path / "cwd"
    env_file = _write_env(
        tmp_path / ".env.gemini-subscription.local",
        f"""
        HERMES_EXTERNAL_WORKSPACE_ENABLED=1
        HERMES_EXTERNAL_WORKSPACE_ENV=staging
        HERMES_EXTERNAL_WORKSPACE_CMD=agy
        HERMES_EXTERNAL_WORKSPACE_ARGS=run --prompt {{prompt}}
        HERMES_EXTERNAL_WORKSPACE_HOME={home_dir}
        HERMES_EXTERNAL_WORKSPACE_CWD={cwd}
        GOOGLE_WORKSPACE_MARKER=calendar-ok
        HERMES_EXTERNAL_WORKSPACE_INTERNAL_SECRET=do-not-pass
        """,
    )
    monkeypatch.setenv("HERMES_EXTERNAL_WORKSPACE_ENV_FILE", str(env_file))
    monkeypatch.setattr(workspace.shutil, "which", lambda cmd, path=None: "/usr/bin/agy")

    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout='{"answer":"ok"}', stderr="")

    monkeypatch.setattr(workspace.subprocess, "run", fake_run)

    result = json.loads(
        workspace.workspace_external_provider_tool(
            {
                "task": "Show my Google Calendar plans for today.",
                "scope": "calendar",
                "output_format": "json",
            }
        )
    )

    assert result["success"] is True
    assert result["output"] == '{"answer":"ok"}'
    assert captured["command"][0] == "/usr/bin/agy"
    assert captured["command"][1:3] == ["run", "--prompt"]
    assert "User task:\nShow my Google Calendar plans for today." in captured["command"][3]
    assert "shell" not in captured["kwargs"] or captured["kwargs"]["shell"] is not True
    assert captured["kwargs"]["input"] is None
    assert captured["kwargs"]["cwd"] == str(cwd)
    child_env = captured["kwargs"]["env"]
    assert child_env["HOME"] == str(home_dir)
    assert child_env["GOOGLE_WORKSPACE_MARKER"] == "calendar-ok"
    assert "HERMES_EXTERNAL_WORKSPACE_INTERNAL_SECRET" not in child_env


def test_external_workspace_provider_refuses_write_task_in_read_only_mode(monkeypatch, tmp_path):
    env_file = _write_env(
        tmp_path / ".env.gemini-subscription.local",
        """
        HERMES_EXTERNAL_WORKSPACE_ENABLED=1
        HERMES_EXTERNAL_WORKSPACE_ENV=dev
        HERMES_EXTERNAL_WORKSPACE_CMD=agy
        """,
    )
    monkeypatch.setenv("HERMES_EXTERNAL_WORKSPACE_ENV_FILE", str(env_file))
    monkeypatch.setattr(workspace.shutil, "which", lambda cmd, path=None: "/usr/bin/agy")

    def fail_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called")

    monkeypatch.setattr(workspace.subprocess, "run", fail_run)

    result = json.loads(
        workspace.workspace_external_provider_tool(
            {
                "task": "Create a calendar event tomorrow at 10.",
                "scope": "calendar",
            }
        )
    )

    assert "error" in result
    assert "read-only mode" in result["error"]
