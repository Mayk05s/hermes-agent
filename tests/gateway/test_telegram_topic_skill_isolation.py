"""Regression tests for Telegram forum-topic skill/tool isolation."""

import json
from types import SimpleNamespace

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageType
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
                            },
                            {
                                "name": "english",
                                "thread_id": 796,
                                "skills": [
                                    "telegram_family/telegram-english-learning-posts",
                                    "telegram_family/natalia-english-blog-post",
                                ],
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


def test_family_calendar_toolset_is_owner_only():
    owner = _telegram_source("-1003966683704", "576")
    owner.profile_name = "family-chat"
    owner.user_id = "179555559"
    member = _telegram_source("-1003966683704", "576")
    member.profile_name = "family-chat"
    member.user_id = "367599252"

    owner_scoped = gateway_run._scope_gateway_toolsets_for_source(
        ["codex_calendar", "web"],
        _topic_config(),
        "telegram",
        owner,
    )
    member_scoped = gateway_run._scope_gateway_toolsets_for_source(
        ["codex_calendar", "web"],
        _topic_config(),
        "telegram",
        member,
    )

    assert "codex_calendar" in owner_scoped
    assert "codex_calendar" not in member_scoped


def test_shared_personal_topic_uses_current_attributed_owner_for_calendar_toolset():
    source = _telegram_source("-1003735932411", "313")
    source.profile_name = "personal"
    source.user_id = None
    tokens = set_session_vars(requester_user_id="179555559")
    try:
        scoped = gateway_run._scope_gateway_toolsets_for_source(
            ["codex_calendar", "web"],
            _topic_config(),
            "telegram",
            source,
        )
    finally:
        clear_session_vars(tokens)

    assert "codex_calendar" in scoped


def test_personal_calendar_toolset_is_removed_for_owner_in_other_group():
    owner = _telegram_source("-1002757852891", "28112")
    owner.profile_name = "family-chat"
    owner.user_id = "179555559"

    scoped = gateway_run._scope_gateway_toolsets_for_source(
        ["codex_calendar", "web"],
        _topic_config(),
        "telegram",
        owner,
    )

    assert "codex_calendar" not in scoped


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


def test_nonisolated_topic_gets_same_chat_cross_topic_guard():
    source = _telegram_source("-1003966683704", "359")
    source.topic_isolation = False

    guard = gateway_run._telegram_topic_skill_isolation_prompt(
        _topic_config(),
        source,
    )

    assert "cross-topic context is active" in guard
    assert "same chat" in guard
    assert "do not move messages" in guard


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


def test_long_task_handoff_keeps_chat_and_selects_configured_topic():
    config = _topic_config()
    config["telegram"]["extra"]["group_topics"].append({
        "chat_id": "-1003938895426",
        "topics": [{
            "name": "Instagram/TikTok",
            "thread_id": 2,
            "long_task_handoff": {
                "enabled": True,
                "target_thread_id": 35,
                "target_label": "работа Трипио",
                "min_text_length": 200,
                "keywords": ["сгенерируй", "переделай"],
            },
        }],
    })
    source = _telegram_source("-1003938895426", "2")
    event = SimpleNamespace(
        text="Пожалуйста, переделай эти карточки",
        media_urls=[],
        media_types=[],
    )

    handoff = gateway_run._telegram_long_task_handoff(config, source, event)

    assert handoff["target_thread_id"] == "35"
    assert handoff["target_label"] == "работа Трипио"
    assert source.chat_id == "-1003938895426"


def test_long_task_handoff_does_not_move_short_conversation():
    config = _topic_config()
    config["telegram"]["extra"]["group_topics"].append({
        "chat_id": "-1003938895426",
        "topics": [{
            "name": "Instagram/TikTok",
            "thread_id": 2,
            "long_task_handoff": {
                "enabled": True,
                "target_thread_id": 35,
                "min_text_length": 200,
                "keywords": ["сгенерируй"],
            },
        }],
    })

    handoff = gateway_run._telegram_long_task_handoff(
        config,
        _telegram_source("-1003938895426", "2"),
        SimpleNamespace(text="Почему?", media_urls=[], media_types=[]),
    )

    assert handoff is None


def test_long_task_handoff_never_moves_voice_or_audio_as_generic_media():
    config = _topic_config()
    config["telegram"]["extra"]["group_topics"].append({
        "chat_id": "-1003938895426",
        "topics": [{
            "name": "Instagram/TikTok",
            "thread_id": 2,
            "long_task_handoff": {
                "enabled": True,
                "target_thread_id": 35,
                "always": True,
                "on_media": True,
                "min_text_length": 1,
                "keywords": ["сделай"],
            },
        }],
    })

    for message_type, path, media_type in (
        (MessageType.VOICE, "/tmp/voice.ogg", "audio/ogg"),
        (MessageType.AUDIO, "/tmp/audio.mp3", "audio/mpeg"),
    ):
        handoff = gateway_run._telegram_long_task_handoff(
            config,
            _telegram_source("-1003938895426", "2"),
            SimpleNamespace(
                text="сделай",
                message_type=message_type,
                media_urls=[path],
                media_types=[media_type],
            ),
        )

        assert handoff is None


def test_long_task_handoff_still_moves_non_audio_media():
    config = _topic_config()
    config["telegram"]["extra"]["group_topics"].append({
        "chat_id": "-1003938895426",
        "topics": [{
            "name": "Instagram/TikTok",
            "thread_id": 2,
            "long_task_handoff": {
                "enabled": True,
                "target_thread_id": 35,
                "on_media": True,
            },
        }],
    })

    handoff = gateway_run._telegram_long_task_handoff(
        config,
        _telegram_source("-1003938895426", "2"),
        SimpleNamespace(
            text="",
            message_type=MessageType.VIDEO,
            media_urls=["/tmp/video.mp4"],
            media_types=["video/mp4"],
        ),
    )

    assert handoff is not None
    assert handoff["target_thread_id"] == "35"


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


def test_topic_binding_supports_multiple_skills():
    allowed = gateway_run._gateway_allowed_skills_for_source(
        _topic_config(),
        _telegram_source("-1003966683704", "796"),
    )

    assert allowed == [
        "telegram_family/telegram-english-learning-posts",
        "telegram-english-learning-posts",
        "telegram_family/natalia-english-blog-post",
        "natalia-english-blog-post",
    ]

    guard = gateway_run._telegram_topic_skill_isolation_prompt(
        _topic_config(),
        _telegram_source("-1003966683704", "796"),
    )

    assert "telegram_family/telegram-english-learning-posts" in guard
    assert "telegram_family/natalia-english-blog-post" in guard


def test_auto_load_topic_skill_uses_profile_skills_dir(tmp_path):
    profile_home = tmp_path / "profiles" / "family-chat"
    _write_skill(
        profile_home / "skills",
        "telegram_family/telegram-english-learning-posts",
        "telegram-english-learning-posts",
    )

    original_skills_dir = skills_tool.SKILLS_DIR
    text, loaded, missing = gateway_run._auto_load_skill_text_for_profile(
        ["telegram_family/telegram-english-learning-posts"],
        "Hello",
        task_id="session-1",
        profile_home=profile_home,
    )

    assert loaded == ["telegram_family/telegram-english-learning-posts"]
    assert missing == []
    assert "telegram-english-learning-posts skill" in text
    assert text.endswith("Hello")
    assert skills_tool.SKILLS_DIR == original_skills_dir


def test_auto_skills_to_load_backfills_existing_sessions_once(tmp_path):
    skill_name = "telegram_boxmap/telegram_boxmap_context"
    _write_skill(tmp_path / "skills", skill_name, "telegram_boxmap_context")
    entry = SimpleNamespace(auto_loaded_skills=[], auto_loaded_skill_versions={})

    first = gateway_run._auto_skills_to_load_for_session(
        entry,
        [skill_name],
        is_new_session=False,
        profile_home=tmp_path,
    )
    assert first == [skill_name]

    entry.auto_loaded_skills = list(first)
    entry.auto_loaded_skill_versions = {
        skill_name: gateway_run._auto_skill_content_version(tmp_path, skill_name)
    }
    second = gateway_run._auto_skills_to_load_for_session(
        entry,
        [skill_name],
        is_new_session=False,
        profile_home=tmp_path,
    )
    assert second == []

    skill_file = tmp_path / "skills" / skill_name / "SKILL.md"
    skill_file.write_text(skill_file.read_text() + "\nchanged\n")
    changed = gateway_run._auto_skills_to_load_for_session(
        entry,
        [skill_name],
        is_new_session=False,
        profile_home=tmp_path,
    )
    assert changed == [skill_name]


def test_auto_skills_to_load_keeps_new_session_behavior():
    entry = SimpleNamespace(
        auto_loaded_skills=["telegram_boxmap/telegram_boxmap_context"],
        auto_loaded_skill_versions={},
    )

    assert gateway_run._auto_skills_to_load_for_session(
        entry,
        ["telegram_boxmap/telegram_boxmap_context"],
        is_new_session=True,
    ) == ["telegram_boxmap/telegram_boxmap_context"]


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
