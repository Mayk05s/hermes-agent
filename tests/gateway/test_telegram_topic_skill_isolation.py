"""Regression tests for Telegram forum-topic skill/tool isolation."""

import json
from types import SimpleNamespace

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_context import clear_session_vars, set_session_vars
from tools import skills_tool


def _telegram_source(chat_id: str, thread_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="group",
        thread_id=thread_id,
        user_id="42",
    )


def _topic_config() -> dict:
    return {
        "telegram": {
            "extra": {
                "group_topics": [
                    {
                        "chat_id": "-1003735932411",
                        "topics": [
                            {
                                "name": "homeassistant",
                                "thread_id": 2,
                                "skill": "telegram_homeassistant/telegram_homeassistant_context",
                            }
                        ],
                    },
                    {
                        "chat_id": "-1003966683704",
                        "topics": [
                            {
                                "name": "family",
                                "thread_id": 359,
                                "skill": "telegram_family/telegram_family_context",
                            }
                        ],
                    },
                ]
            }
        }
    }


def test_family_topic_removes_homeassistant_toolset_even_if_platform_enabled():
    scoped = gateway_run._scope_gateway_toolsets_for_source(
        ["core", "homeassistant", "web"],
        _topic_config(),
        "telegram",
        _telegram_source("-1003966683704", "359"),
    )

    assert scoped == ["core", "web"]


def test_homeassistant_topic_keeps_homeassistant_toolset():
    scoped = gateway_run._scope_gateway_toolsets_for_source(
        ["core", "web"],
        _topic_config(),
        "telegram",
        _telegram_source("-1003735932411", "2"),
    )

    assert "homeassistant" in scoped


def test_family_topic_gets_generic_topic_visibility_guard():
    guard = gateway_run._telegram_topic_skill_isolation_prompt(
        _topic_config(),
        _telegram_source("-1003966683704", "359"),
    )

    assert guard is not None
    assert "telegram_family/telegram_family_context" in guard
    assert "Only this topic's configured instructions" in guard
    assert "other Telegram topics are out of scope" in guard
    assert "Home Assistant" not in guard
    assert "статус" not in guard


def test_homeassistant_topic_does_not_get_negative_homeassistant_guard():
    guard = gateway_run._telegram_topic_skill_isolation_prompt(
        _topic_config(),
        _telegram_source("-1003735932411", "2"),
    )

    assert guard is None


def test_non_telegram_sources_do_not_get_telegram_guard():
    source = SimpleNamespace(platform=Platform.DISCORD, chat_id="x", thread_id="359")

    guard = gateway_run._telegram_topic_skill_isolation_prompt(_topic_config(), source)

    assert guard is None


def _write_skill(root, rel: str, name: str) -> None:
    skill_dir = root / rel
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_gateway_allowed_skills_for_family_and_ha_topics():
    assert gateway_run._gateway_allowed_skills_for_source(
        _topic_config(),
        _telegram_source("-1003966683704", "359"),
    ) == ["telegram_family/telegram_family_context", "telegram_family_context"]
    assert gateway_run._gateway_allowed_skills_for_source(
        _topic_config(),
        _telegram_source("-1003735932411", "2"),
    ) == ["telegram_homeassistant/telegram_homeassistant_context", "telegram_homeassistant_context"]


def test_family_topic_skill_view_cannot_load_homeassistant_skill(tmp_path, monkeypatch):
    _write_skill(tmp_path, "telegram_homeassistant/telegram_homeassistant_context", "telegram_homeassistant_context")
    _write_skill(tmp_path, "telegram_family/telegram_family_context", "telegram_family_context")
    _write_skill(tmp_path, "productivity/global_safe", "global_safe")
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path)

    tokens = set_session_vars(
        platform="telegram",
        chat_id="-1003966683704",
        thread_id="359",
        allowed_skills="telegram_family/telegram_family_context,telegram_family_context",
    )
    try:
        denied = json.loads(skills_tool.skill_view("telegram_homeassistant/telegram_homeassistant_context"))
        assert denied["success"] is False
        assert "not visible" in denied["error"] or "not found" in denied["error"]

        family = json.loads(skills_tool.skill_view("telegram_family/telegram_family_context"))
        assert family["success"] is True
        assert family["name"] == "telegram_family_context"

        global_safe = json.loads(skills_tool.skill_view("global_safe"))
        assert global_safe["success"] is True
    finally:
        clear_session_vars(tokens)


def test_ha_topic_skill_view_can_load_homeassistant_skill(tmp_path, monkeypatch):
    _write_skill(tmp_path, "telegram_homeassistant/telegram_homeassistant_context", "telegram_homeassistant_context")
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path)

    tokens = set_session_vars(
        platform="telegram",
        chat_id="-1003735932411",
        thread_id="2",
        allowed_skills="telegram_homeassistant/telegram_homeassistant_context,telegram_homeassistant_context",
    )
    try:
        result = json.loads(skills_tool.skill_view("telegram_homeassistant/telegram_homeassistant_context"))
        assert result["success"] is True
        assert result["name"] == "telegram_homeassistant_context"
    finally:
        clear_session_vars(tokens)


def test_skills_list_hides_other_telegram_topic_skills_but_keeps_global(tmp_path, monkeypatch):
    _write_skill(tmp_path, "telegram_homeassistant/telegram_homeassistant_context", "telegram_homeassistant_context")
    _write_skill(tmp_path, "telegram_family/telegram_family_context", "telegram_family_context")
    _write_skill(tmp_path, "productivity/global_safe", "global_safe")
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path)

    tokens = set_session_vars(
        platform="telegram",
        chat_id="-1003966683704",
        thread_id="359",
        allowed_skills="telegram_family/telegram_family_context,telegram_family_context",
    )
    try:
        result = json.loads(skills_tool.skills_list())
        names = {skill["name"] for skill in result["skills"]}
        assert "telegram_family_context" in names
        assert "global_safe" in names
        assert "telegram_homeassistant_context" not in names
    finally:
        clear_session_vars(tokens)
