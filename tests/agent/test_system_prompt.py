"""Tests for agent/system_prompt.py — context-file cwd wiring."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.system_prompt import (
    bind_system_prompt_signature,
    build_system_prompt_parts,
)
from agent.prompt_builder import GOOGLE_CALENDAR_TOOL_GUIDANCE


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _captured_context_cwd(agent):
    """The cwd build_system_prompt_parts hands to build_context_files_prompt."""
    captured = {}

    def fake_context_files(cwd=None, skip_soul=False):
        captured["cwd"] = cwd
        return ""

    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", side_effect=fake_context_files),
    ):
        build_system_prompt_parts(agent)
    return captured["cwd"]


class TestContextFileCwd:
    def test_none_when_terminal_cwd_unset(self, monkeypatch):
        # Unset → None, so discovery falls back to the launch dir inside
        # build_context_files_prompt (the local-CLI #19242 contract).
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        assert _captured_context_cwd(_make_agent()) is None

    def test_configured_dir_when_terminal_cwd_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        assert _captured_context_cwd(_make_agent()) == tmp_path


class TestConnectedToolGuidance:
    @staticmethod
    def _stable_prompt(agent):
        with (
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_environment_hints", return_value=""),
            patch("run_agent.build_context_files_prompt", return_value=""),
        ):
            return build_system_prompt_parts(agent)["stable"]

    def test_calendar_guidance_present_when_function_is_callable(self):
        prompt = self._stable_prompt(
            _make_agent(valid_tool_names={"google_calendar"})
        )

        assert GOOGLE_CALENDAR_TOOL_GUIDANCE in prompt
        assert "prior assistant messages" in prompt
        assert "only after calling `google_calendar`" in prompt

    def test_calendar_guidance_absent_without_function(self):
        prompt = self._stable_prompt(_make_agent(valid_tool_names={"terminal"}))

        assert GOOGLE_CALENDAR_TOOL_GUIDANCE not in prompt


class TestSystemPromptSignatureBinding:
    def test_live_tool_surface_changes_persisted_prompt_signature(self):
        without_calendar = bind_system_prompt_signature("gateway-sig", {"terminal"})
        with_calendar = bind_system_prompt_signature(
            "gateway-sig",
            {"terminal", "google_calendar"},
        )

        assert without_calendar != with_calendar
        assert bind_system_prompt_signature(
            "gateway-sig",
            {"google_calendar", "terminal"},
        ) == with_calendar

    def test_missing_base_signature_remains_unversioned(self):
        assert bind_system_prompt_signature(None, {"google_calendar"}) is None
