from gateway.config import Platform
from gateway.profile_scopes import normalize_profile_scopes_config, resolve_scope_for_source
from gateway.session import SessionSource, build_session_key


def source(thread_id=None, profile_name="family", scope_name="default"):
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        thread_id=thread_id,
        chat_type="forum" if thread_id else "group",
        user_id="u1",
        profile_name=profile_name,
        scope_name=scope_name,
        memory_scope=scope_name,
    )


def test_scope_exact_topic_wins_over_chat_scope():
    cfg = normalize_profile_scopes_config({
        "default_scope": "default",
        "scopes": [
            {"id": "chat", "platform": "telegram", "chat_id": "-1001", "scope": "family"},
            {"id": "topic", "platform": "telegram", "chat_id": "-1001", "thread_id": "63", "scope": "work", "memory_scope": "work-topic", "skill_sets": {"mode": "allow", "names": ["calendar"]}},
        ],
    })

    exact = resolve_scope_for_source(cfg, source(thread_id="63"))
    fallback = resolve_scope_for_source(cfg, source(thread_id="99"))

    assert exact.scope == "work"
    assert exact.memory_scope == "work-topic"
    assert exact.skill_sets == {"mode": "allow", "names": ["calendar"]}
    assert fallback.scope == "family"


def test_session_key_includes_profile_and_scope():
    family = build_session_key(source(profile_name="family", scope_name="default"))
    work = build_session_key(source(profile_name="work", scope_name="default"))
    family_topic = build_session_key(source(profile_name="family", scope_name="topic-a"))

    assert family != work
    assert family != family_topic
    assert "profile:family:scope:default" in family
    assert "profile:work:scope:default" in work
    assert "profile:family:scope:topic-a" in family_topic


def test_default_profile_scope_preserves_legacy_session_key():
    key = build_session_key(source(profile_name="default", scope_name="default"))

    assert key.startswith("agent:main:telegram:group:-1001")
    assert "profile:default" not in key
