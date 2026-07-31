from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def family_cron_home(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    profile_home = root / "profiles" / "family-chat"
    (root / "cron").mkdir(parents=True)
    profile_home.mkdir(parents=True)

    (root / "config.yaml").write_text(
        """
profile_routes:
  default_profile: default
  routes:
    - id: telegram-family
      enabled: true
      platform: telegram
      chat_id: "-1003966683704"
      profile: family-chat
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        """
memory:
  memory_enabled: true
  user_profile_enabled: true
platform_toolsets:
  telegram:
    - image_gen
    - memory
    - skills
telegram:
  extra:
    group_topics:
      - chat_id: "-1003966683704"
        topics:
          - name: english
            thread_id: 796
            skills:
              - telegram_family/telegram-english-learning-posts
              - telegram_family/natalia-english-blog-post
profile_scopes:
  default_scope: default
  scopes:
    - id: telegram-family-english
      enabled: true
      platform: telegram
      chat_id: "-1003966683704"
      thread_id: "796"
      scope: english
      memory_scope: english
      skill_sets:
        mode: allow
        names:
          - telegram_family/telegram-english-learning-posts
          - telegram-english-learning-posts
          - telegram_family/natalia-english-blog-post
          - natalia-english-blog-post
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr("cron.jobs.CRON_DIR", root / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", root / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", root / "cron" / "output")
    return root, profile_home


@pytest.fixture()
def family_english_session():
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(
        platform="telegram",
        chat_id="-1003966683704",
        chat_name="Family",
        thread_id="796",
        profile_name="family-chat",
        scope_name="english",
        memory_scope="english",
        allowed_skills=(
            "telegram_family/telegram-english-learning-posts,"
            "telegram-english-learning-posts,"
            "telegram_family/natalia-english-blog-post,"
            "natalia-english-blog-post"
        ),
    )
    try:
        yield
    finally:
        clear_session_vars(tokens)


def test_create_from_chat_binds_profile_scope_skills_and_toolsets(
    family_cron_home,
    family_english_session,
):
    from tools.cronjob_tools import cronjob

    created = json.loads(
        cronjob(
            action="create",
            prompt="Make today's post",
            schedule="every 1d",
            skills=["natalia-english-blog-post"],
            enabled_toolsets=["image_gen"],
        )
    )

    assert created["success"] is True
    job = created["job"]
    assert job["profile"] == "family-chat"
    assert job["skills"] == ["telegram_family/natalia-english-blog-post"]
    assert "profile=family-chat" in created["access_context"]
    assert "memory_scope=english" in created["access_context"]

    from cron.jobs import get_job

    stored = get_job(created["job_id"])
    assert stored["origin"]["thread_id"] == "796"
    assert stored["access_context"]["profile"] == "family-chat"
    assert stored["access_context"]["memory_scope"] == "english"
    assert stored["enabled_toolsets"] == ["image_gen"]


def test_create_from_chat_rejects_foreign_skill(
    family_cron_home,
    family_english_session,
):
    from tools.cronjob_tools import cronjob

    created = json.loads(
        cronjob(
            action="create",
            prompt="Do HA work",
            schedule="every 1d",
            skills=["telegram_homeassistant/telegram_homeassistant_context"],
        )
    )

    assert created["success"] is False
    assert "outside the creator chat/topic" in created["error"]


def test_chat_list_hides_jobs_from_other_contexts(
    family_cron_home,
    family_english_session,
):
    from cron.jobs import create_job
    from tools.cronjob_tools import cronjob

    family_access = {
        "version": 1,
        "source": "gateway",
        "platform": "telegram",
        "chat_id": "-1003966683704",
        "thread_id": "796",
        "profile": "family-chat",
        "scope": "english",
        "memory_scope": "english",
        "allowed_skills": ["telegram_family/natalia-english-blog-post"],
        "enabled_toolsets": ["image_gen", "memory", "skills"],
    }
    other_access = {
        **family_access,
        "chat_id": "-1000000000000",
        "thread_id": "",
        "profile": "default",
        "scope": "default",
        "memory_scope": "default",
    }
    visible = create_job(
        prompt="visible",
        schedule="every 1d",
        origin={"platform": "telegram", "chat_id": "-1003966683704", "thread_id": "796"},
        profile="family-chat",
        access_context=family_access,
    )
    hidden = create_job(
        prompt="hidden",
        schedule="every 1d",
        origin={"platform": "telegram", "chat_id": "-1000000000000"},
        profile="default",
        access_context=other_access,
    )

    listing = json.loads(cronjob(action="list", include_disabled=True))
    ids = {job["job_id"] for job in listing["jobs"]}

    assert visible["id"] in ids
    assert hidden["id"] not in ids


def test_scheduler_runs_with_creator_memory_scope_and_not_live_chat_context(
    family_cron_home,
):
    root, _profile_home = family_cron_home
    observed = {}
    access_context = {
        "version": 1,
        "source": "gateway",
        "platform": "telegram",
        "chat_id": "-1003966683704",
        "thread_id": "796",
        "profile": "family-chat",
        "scope": "english",
        "memory_scope": "english",
        "allowed_skills": ["telegram_family/natalia-english-blog-post"],
        "enabled_toolsets": ["image_gen", "memory", "skills"],
    }
    job = {
        "id": "family-job",
        "name": "family job",
        "prompt": "hello",
        "origin": {
            "platform": "telegram",
            "chat_id": "-1003966683704",
            "thread_id": "796",
        },
        "profile": "family-chat",
        "access_context": access_context,
    }

    class FakeAgent:
        def __init__(self, **kwargs):
            from gateway.session_context import get_session_env
            from hermes_constants import get_hermes_home

            observed["hermes_home"] = str(get_hermes_home())
            observed["memory_scope"] = get_session_env("HERMES_SESSION_MEMORY_SCOPE")
            observed["profile_name"] = get_session_env("HERMES_SESSION_PROFILE_NAME")
            observed["platform"] = get_session_env("HERMES_SESSION_PLATFORM")
            observed["allowed_skills"] = get_session_env("HERMES_SESSION_ALLOWED_SKILLS")
            observed["skip_memory"] = kwargs.get("skip_memory")
            observed["enabled_toolsets"] = kwargs.get("enabled_toolsets")

        def run_conversation(self, *_args, **_kwargs):
            return {"final_response": "ok"}

    fake_db = MagicMock()
    with patch("cron.scheduler._hermes_home", root), \
         patch("dotenv.load_dotenv"), \
         patch("hermes_state.SessionDB", return_value=fake_db), \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             return_value={
                 "api_key": "test-key",
                 "base_url": "https://example.invalid/v1",
                 "provider": "openrouter",
                 "api_mode": "chat_completions",
             },
         ), \
         patch("run_agent.AIAgent", FakeAgent):
        from cron.scheduler import run_job

        success, _doc, final, error = run_job(job)

    assert success is True
    assert final == "ok"
    assert error is None
    assert observed["hermes_home"] == str(root / "profiles" / "family-chat")
    assert observed["memory_scope"] == "english"
    assert observed["profile_name"] == "family-chat"
    assert observed["platform"] == ""
    assert observed["allowed_skills"] == "telegram_family/natalia-english-blog-post"
    assert observed["skip_memory"] is False
    assert observed["enabled_toolsets"] == ["image_gen", "memory", "skills"]


def test_access_context_empty_toolsets_do_not_fall_back_to_cron_defaults():
    from cron.scheduler import _resolve_cron_enabled_toolsets

    job = {
        "id": "locked-down",
        "access_context": {"enabled_toolsets": []},
        "enabled_toolsets": [],
    }

    assert _resolve_cron_enabled_toolsets(job, {}) == []


def test_prepare_job_accepts_canonical_skill_when_snapshot_also_has_bare_alias(monkeypatch):
    from cron.access_control import prepare_job_for_run

    current = {
        "version": 1,
        "source": "gateway",
        "platform": "telegram",
        "chat_id": "-1003735932411",
        "thread_id": "2",
        "profile": "personal",
        "scope": "default",
        "memory_scope": "default",
        "allowed_skills": ["telegram_homeassistant/telegram_homeassistant_context"],
        "enabled_toolsets": ["homeassistant"],
    }
    job = {
        "id": "ha-alias-regression",
        "origin": {"platform": "telegram", "chat_id": "-1003735932411", "thread_id": "2"},
        "profile": "personal",
        "access_context": {
            **current,
            "allowed_skills": [
                "telegram_homeassistant/telegram_homeassistant_context",
                "telegram_homeassistant_context",
            ],
        },
        "enabled_toolsets": ["homeassistant"],
    }
    monkeypatch.setattr("cron.access_control.access_context_for_origin", lambda *_args, **_kwargs: current)

    prepared = prepare_job_for_run(job)

    assert prepared["access_context"]["allowed_skills"] == job["access_context"]["allowed_skills"]


def test_prepare_job_rejects_unrelated_skill_even_when_aliases_are_allowed(monkeypatch):
    from cron.access_control import CronAccessError, prepare_job_for_run

    current = {
        "version": 1,
        "source": "gateway",
        "platform": "telegram",
        "chat_id": "-1003735932411",
        "thread_id": "2",
        "profile": "personal",
        "scope": "default",
        "memory_scope": "default",
        "allowed_skills": ["telegram_homeassistant/telegram_homeassistant_context"],
        "enabled_toolsets": ["homeassistant"],
    }
    job = {
        "id": "foreign-skill-regression",
        "origin": {"platform": "telegram", "chat_id": "-1003735932411", "thread_id": "2"},
        "profile": "personal",
        "access_context": {**current, "allowed_skills": ["telegram_health/health-coordinator"]},
        "enabled_toolsets": ["homeassistant"],
    }
    monkeypatch.setattr("cron.access_control.access_context_for_origin", lambda *_args, **_kwargs: current)

    with pytest.raises(CronAccessError, match="skill allowlist"):
        prepare_job_for_run(job)
