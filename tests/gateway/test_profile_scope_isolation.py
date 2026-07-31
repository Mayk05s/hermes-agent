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
    assert exact.topic_isolation is True
    assert exact.skill_sets == {"mode": "allow", "names": ["calendar"]}
    assert fallback.scope == "family"


def test_topic_scope_can_inherit_profile_memory_when_topic_isolation_disabled():
    cfg = normalize_profile_scopes_config({
        "default_scope": "default",
        "scopes": [
            {"id": "chat", "platform": "telegram", "chat_id": "-1001", "scope": "family", "memory_scope": "family"},
            {
                "id": "topic",
                "platform": "telegram",
                "chat_id": "-1001",
                "thread_id": "63",
                "scope": "work",
                "memory_scope": "work-topic",
                "topic_isolation": False,
                "skill_sets": {"mode": "allow", "names": ["calendar"]},
            },
        ],
    })

    exact = resolve_scope_for_source(cfg, source(thread_id="63"))

    assert exact.scope == "family"
    assert exact.memory_scope == "family"
    assert exact.topic_isolation is False
    assert exact.skill_sets == {"mode": "allow", "names": ["calendar"]}


def test_unconfigured_topics_remain_isolated_by_default():
    cfg = normalize_profile_scopes_config({
        "default_scope": "default",
        "scopes": [],
    })

    resolved = resolve_scope_for_source(cfg, source(thread_id="63"))

    assert resolved.topic_isolation is True


def test_topic_isolation_false_keeps_parallel_live_sessions_across_topics():
    first = source(thread_id="2", scope_name="default")
    first.topic_isolation = False
    second = source(thread_id="35", scope_name="default")
    second.topic_isolation = False
    isolated = source(thread_id="1", scope_name="default")
    isolated.topic_isolation = True

    first_key = build_session_key(first)
    second_key = build_session_key(second)
    isolated_key = build_session_key(isolated)

    assert first_key != second_key
    assert first_key.endswith("telegram:forum:-1001:2")
    assert second_key.endswith("telegram:forum:-1001:35")
    assert isolated_key.endswith("telegram:forum:-1001:1")
    assert first.memory_scope == second.memory_scope == isolated.memory_scope


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
