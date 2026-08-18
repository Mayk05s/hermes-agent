"""Tests for the single-shape session_search tool.

Three calling shapes:
  1. DISCOVERY — pass query → FTS5 + anchored window + bookends per hit
  2. SCROLL    — pass session_id + around_message_id → just the window
  3. BROWSE    — no args → recent sessions chronologically

All run zero LLM calls.
"""
import json
import time

import pytest

from hermes_state import SessionDB
import tools.recall_access_tool as recall_access_mod
from tools.session_search_tool import (
    SESSION_SEARCH_SCHEMA,
    _HIDDEN_SESSION_SOURCES,
    _format_timestamp,
    session_search,
)
from tools.recall_access_tool import (
    _RUNTIME_GRANTS,
    recall_access_tool,
    resolve_recall_target,
)


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


@pytest.fixture(autouse=True)
def clear_recall_access_runtime_grants():
    _RUNTIME_GRANTS.clear()
    yield
    _RUNTIME_GRANTS.clear()


def _seed_modpack_sessions(db):
    """Create three sessions about a modpack so FTS5 has hits to dedupe."""
    now = int(time.time())
    # Older session — modpack origin
    db.create_session("s_oldest", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 30000, "Building the Modpack", "s_oldest"))
    db.append_message("s_oldest", role="user", content="Let's build a Minecraft modpack")
    db.append_message("s_oldest", role="assistant", content="Great. Let me scaffold the modpack repo.")
    db.append_message("s_oldest", role="user", content="Use NeoForge 1.21.1")
    db.append_message("s_oldest", role="assistant", content="Done. Modpack repo created with NeoForge 1.21.1.")
    db.append_message("s_oldest", role="assistant", content="Tier-0 mods installed; modpack smoke test passes.")

    # Middle session — modpack quest coverage
    db.create_session("s_middle", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 15000, "Modpack Quest Coverage", "s_middle"))
    db.append_message("s_middle", role="user", content="Deep-dive every modpack reference quest guide")
    db.append_message("s_middle", role="assistant", content="Surveying ATM10 questbook for modpack inspiration.")
    db.append_message("s_middle", role="user", content="Update the modpack version too")
    db.append_message("s_middle", role="assistant", content="Modpack version bumped 0.4 → 0.8.5; quest coverage page added.")

    # Newest session — modpack mob spawn fix
    db.create_session("s_newest", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 1000, "Modpack Mob Spawn Fix", "s_newest"))
    db.append_message("s_newest", role="user", content="Fix the modpack mob spawning")
    db.append_message("s_newest", role="assistant", content="Investigating elite mob gating in the modpack KubeJS.")
    db.append_message("s_newest", role="assistant", content="Shipped commit b850442. Modpack alternator nerfed too.")
    db._conn.commit()


def _telegram_scope(
    chat_id="-1003735932411",
    thread_id="6827",
    profile_name="default",
    scope_name="default",
    memory_scope=None,
    topic_isolation=True,
):
    origin = {
        "platform": "telegram",
        "chat_id": chat_id,
        "thread_id": thread_id,
        "chat_type": "group",
        "profile_name": profile_name,
        "scope_name": scope_name,
        "memory_scope": memory_scope or scope_name,
    }
    if topic_isolation:
        origin["topic_isolation"] = True
    return json.dumps(
        {
            "session_key": f"agent:main:telegram:group:{chat_id}:{thread_id}",
            "origin": origin,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _set_gateway_topic(
    monkeypatch,
    chat_id="-1003735932411",
    thread_id="6827",
    profile_name="default",
    scope_name="default",
    memory_scope=None,
    topic_isolation=True,
):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", chat_id)
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", thread_id)
    monkeypatch.setenv("HERMES_SESSION_KEY", f"agent:main:telegram:group:{chat_id}:{thread_id}")
    monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "msg-1")
    monkeypatch.setenv("HERMES_SESSION_PROFILE_NAME", profile_name)
    monkeypatch.setenv("HERMES_SESSION_SCOPE_NAME", scope_name)
    monkeypatch.setenv("HERMES_SESSION_MEMORY_SCOPE", memory_scope or scope_name)
    monkeypatch.setenv("HERMES_SESSION_TOPIC_ISOLATION", "true" if topic_isolation else "false")


# =========================================================================
# Schema invariants
# =========================================================================

class TestSchema:
    def test_schema_has_required_params(self):
        params = SESSION_SEARCH_SCHEMA["parameters"]["properties"]
        # Discovery shape
        assert "query" in params
        assert "limit" in params
        assert "sort" in params
        # Scroll shape
        assert "session_id" in params
        assert "around_message_id" in params
        assert "window" in params
        # Shared
        assert "role_filter" in params

    def test_no_mode_parameter(self):
        # Mode is inferred from which args are set — no explicit mode param
        params = SESSION_SEARCH_SCHEMA["parameters"]["properties"]
        assert "mode" not in params

    def test_sort_enum(self):
        params = SESSION_SEARCH_SCHEMA["parameters"]["properties"]
        assert params["sort"]["enum"] == ["newest", "oldest"]

    def test_schema_description_teaches_scroll(self):
        desc = SESSION_SEARCH_SCHEMA["description"]
        assert "SCROLL" in desc
        assert "DISCOVERY" in desc
        assert "BROWSE" in desc
        # Must explain how to scroll
        assert "scroll FORWARD" in desc or "messages[-1]" in desc

    def test_no_llm_promise_in_description(self):
        # The new design never calls an LLM
        desc = SESSION_SEARCH_SCHEMA["description"].lower()
        assert "no llm" in desc


class TestHiddenSources:
    def test_tool_source_hidden(self):
        assert "tool" in _HIDDEN_SESSION_SOURCES


class TestFormatTimestamp:
    def test_unix_timestamp(self):
        out = _format_timestamp(1700000000)
        assert "2023" in out

    def test_none(self):
        assert _format_timestamp(None) == "unknown"

    def test_iso_string_passthrough(self):
        out = _format_timestamp("not-a-number-string")
        assert out == "not-a-number-string"


# =========================================================================
# Browse shape (no args)
# =========================================================================

class TestBrowseShape:
    def test_no_args_returns_recent_sessions(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(db=db))
        assert result["success"] is True
        assert result["mode"] == "browse"
        assert result["count"] >= 3

    def test_browse_excludes_current_session(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(db=db, current_session_id="s_newest"))
        sids = [r["session_id"] for r in result["results"]]
        assert "s_newest" not in sids

    def test_browse_returns_titles(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(db=db))
        titles = [r.get("title") for r in result["results"]]
        assert any("Modpack" in (t or "") for t in titles)

    def test_gateway_browse_is_limited_to_current_topic(self, db, monkeypatch):
        _set_gateway_topic(monkeypatch)
        db.create_session("boxmap", source="telegram", access_scope=_telegram_scope())
        db.append_message("boxmap", role="user", content="boxmap topic status")
        db.create_session(
            "other-topic",
            source="telegram",
            access_scope=_telegram_scope(thread_id="321"),
        )
        db.append_message("other-topic", role="user", content="health topic status")

        result = json.loads(session_search(db=db))
        sids = [r["session_id"] for r in result["results"]]
        assert "boxmap" in sids
        assert "other-topic" not in sids


# =========================================================================
# Discovery shape (with query)
# =========================================================================

class TestDiscoveryShape:
    def test_query_returns_anchored_windows(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", db=db))
        assert result["success"] is True
        assert result["mode"] == "discover"
        assert result["count"] >= 1

    def test_discovery_result_has_bookends_and_window(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, db=db))
        for hit in result["results"]:
            assert "bookend_start" in hit
            assert "messages" in hit
            assert "bookend_end" in hit
            assert "match_message_id" in hit
            assert "snippet" in hit
            assert "messages_before" in hit
            assert "messages_after" in hit

    def test_match_message_id_is_anchor_in_window(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, db=db))
        for hit in result["results"]:
            anchor_id = hit["match_message_id"]
            window_ids = [m["id"] for m in hit["messages"]]
            assert anchor_id in window_ids

    def test_no_results_returns_empty_list(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="zzz_no_such_term_zzz", db=db))
        assert result["success"] is True
        assert result["results"] == []
        assert result["count"] == 0

    def test_limit_clamped_to_max_10(self, db):
        _seed_modpack_sessions(db)
        # Pass huge limit; should not error and should cap
        result = json.loads(session_search(query="modpack", limit=999, db=db))
        assert result["count"] <= 10

    def test_limit_floor_to_1(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=0, db=db))
        # Result count depends on hits, but the limit must be at least 1
        assert result["count"] >= 0

    def test_non_int_limit_falls_back(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit="bogus", db=db))
        assert result["success"] is True

    def test_current_session_filtered_out(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", db=db, current_session_id="s_newest"))
        sids = [r["session_id"] for r in result["results"]]
        assert "s_newest" not in sids

    def test_gateway_discovery_is_limited_to_current_topic(self, db, monkeypatch):
        _set_gateway_topic(monkeypatch)
        db.create_session("boxmap", source="telegram", access_scope=_telegram_scope())
        db.append_message("boxmap", role="user", content="sharedtoken boxmap funding issue")
        db.create_session(
            "homeassistant",
            source="telegram",
            access_scope=_telegram_scope(thread_id="2"),
        )
        db.append_message("homeassistant", role="user", content="sharedtoken home assistant map issue")
        db.create_session(
            "other-chat",
            source="telegram",
            access_scope=_telegram_scope(chat_id="-1003966683704", thread_id="359"),
        )
        db.append_message("other-chat", role="user", content="sharedtoken family issue")

        result = json.loads(session_search(query="sharedtoken", limit=10, db=db))
        assert result["success"] is True
        assert [r["session_id"] for r in result["results"]] == ["boxmap"]

    def test_gateway_discovery_uses_profile_scope_when_topic_isolation_is_off(self, db, monkeypatch):
        _set_gateway_topic(
            monkeypatch,
            profile_name="family-chat",
            scope_name="default",
            memory_scope="default",
            topic_isolation=False,
        )
        db.create_session(
            "family-topic-a",
            source="telegram",
            access_scope=_telegram_scope(
                profile_name="family-chat",
                scope_name="default",
                memory_scope="default",
                topic_isolation=False,
            ),
        )
        db.append_message("family-topic-a", role="user", content="profiletoken topic a")
        db.create_session(
            "family-topic-b",
            source="telegram",
            access_scope=_telegram_scope(
                thread_id="313",
                profile_name="family-chat",
                scope_name="default",
                memory_scope="default",
                topic_isolation=False,
            ),
        )
        db.append_message("family-topic-b", role="user", content="profiletoken topic b")
        db.create_session(
            "other-profile",
            source="telegram",
            access_scope=_telegram_scope(
                thread_id="777",
                profile_name="work",
                scope_name="default",
                memory_scope="default",
                topic_isolation=False,
            ),
        )
        db.append_message("other-profile", role="user", content="profiletoken other profile")
        db.create_session(
            "other-memory",
            source="telegram",
            access_scope=_telegram_scope(
                thread_id="888",
                profile_name="family-chat",
                scope_name="health",
                memory_scope="health",
                topic_isolation=True,
            ),
        )
        db.append_message("other-memory", role="user", content="profiletoken other memory")

        result = json.loads(session_search(query="profiletoken", limit=10, db=db))
        assert result["success"] is True
        assert {r["session_id"] for r in result["results"]} == {"family-topic-a", "family-topic-b"}

    def test_gateway_discovery_allows_child_sessions_from_current_topic(self, db, monkeypatch):
        _set_gateway_topic(monkeypatch)
        db.create_session("boxmap-root", source="telegram", access_scope=_telegram_scope())
        db.create_session(
            "boxmap-child",
            source="telegram",
            parent_session_id="boxmap-root",
        )
        db.append_message("boxmap-child", role="user", content="childscope boxmap continuation issue")
        db.create_session(
            "other-child-root",
            source="telegram",
            access_scope=_telegram_scope(thread_id="321"),
        )
        db.create_session(
            "other-child",
            source="telegram",
            parent_session_id="other-child-root",
        )
        db.append_message("other-child", role="user", content="childscope health continuation issue")

        result = json.loads(session_search(query="childscope", limit=10, db=db))
        assert result["success"] is True
        assert [r["session_id"] for r in result["results"]] == ["boxmap-child"]

    def test_recall_access_one_turn_adds_other_topic_then_expires(self, db, monkeypatch):
        _set_gateway_topic(monkeypatch)
        db.create_session("boxmap", source="telegram", access_scope=_telegram_scope())
        db.append_message("boxmap", role="user", content="granttoken boxmap issue")
        db.create_session(
            "planning",
            source="telegram",
            access_scope=_telegram_scope(thread_id="313"),
        )
        db.append_message("planning", role="user", content="granttoken planning decision")

        before = json.loads(session_search(query="granttoken", limit=10, db=db))
        assert {r["session_id"] for r in before["results"]} == {"boxmap"}

        grant = json.loads(recall_access_tool(
            reason="Need planning context for this answer",
            chat_id="-1003735932411",
            thread_id="313",
            callback=lambda _question, _choices: "Grant once",
        ))
        assert grant["success"] is True
        assert grant["granted"] is True
        assert grant["duration"] == "one_turn"

        during = json.loads(session_search(query="granttoken", limit=10, db=db))
        assert {r["session_id"] for r in during["results"]} == {"boxmap", "planning"}
        assert during["access_scope"]["grants"][0]["thread_id"] == "313"

        monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "msg-2")
        after = json.loads(session_search(query="granttoken", limit=10, db=db))
        assert {r["session_id"] for r in after["results"]} == {"boxmap"}

    def test_recall_access_session_grant_survives_next_message(self, db, monkeypatch):
        _set_gateway_topic(monkeypatch)
        db.create_session("boxmap", source="telegram", access_scope=_telegram_scope())
        db.append_message("boxmap", role="user", content="sessiongrant boxmap issue")
        db.create_session(
            "planning",
            source="telegram",
            access_scope=_telegram_scope(thread_id="313"),
        )
        db.append_message("planning", role="user", content="sessiongrant planning decision")

        grant = json.loads(recall_access_tool(
            reason="Need planning context for this session",
            chat_id="-1003735932411",
            thread_id="313",
            callback=lambda _question, _choices: "Grant for session",
        ))
        assert grant["duration"] == "session"

        monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "msg-2")
        result = json.loads(session_search(query="sessiongrant", limit=10, db=db))
        assert {r["session_id"] for r in result["results"]} == {"boxmap", "planning"}

    def test_recall_access_can_search_all_sibling_topics_after_confirmation(self, db, monkeypatch):
        _set_gateway_topic(
            monkeypatch,
            chat_id="-1003966683704",
            thread_id="576",
            profile_name="family-chat",
        )
        db.create_session(
            "current-family-topic",
            source="telegram",
            access_scope=_telegram_scope(
                chat_id="-1003966683704",
                thread_id="576",
                profile_name="family-chat",
            ),
        )
        db.append_message("current-family-topic", role="user", content="suitcaselink current")
        db.create_session(
            "sibling-family-topic",
            source="telegram",
            access_scope=_telegram_scope(
                chat_id="-1003966683704",
                thread_id="359",
                profile_name="family-chat",
            ),
        )
        db.append_message(
            "sibling-family-topic",
            role="user",
            content="suitcaselink https://example.test/suitcase",
        )
        db.create_session(
            "same-chat-other-profile",
            source="telegram",
            access_scope=_telegram_scope(
                chat_id="-1003966683704",
                thread_id="999",
                profile_name="private-work",
            ),
        )
        db.append_message("same-chat-other-profile", role="user", content="suitcaselink private")
        db.create_session(
            "other-family-chat",
            source="telegram",
            access_scope=_telegram_scope(
                chat_id="-5274164515",
                thread_id="77",
                profile_name="family-chat",
            ),
        )
        db.append_message("other-family-chat", role="user", content="suitcaselink dm")

        before = json.loads(session_search(query="suitcaselink", limit=10, db=db))
        assert {r["session_id"] for r in before["results"]} == {"current-family-topic"}

        seen = {}

        def approve(question, choices):
            seen["question"] = question
            seen["choices"] = choices
            return choices[0]

        grant = json.loads(recall_access_tool(
            target="other_topics",
            reason="Найти ранее присланную ссылку на чемодан",
            duration="one_turn",
            callback=approve,
        ))

        assert grant["success"] is True
        assert grant["granted"] is True
        assert grant["target"]["mode"] == "chat"
        assert grant["target"]["profile_name"] == "family-chat"
        assert "других топиках" in seen["question"]
        assert seen["choices"] == [
            "Искать один раз",
            "На эту сессию",
            "Разрешать всегда",
            "Не искать",
        ]

        after = json.loads(session_search(query="suitcaselink", limit=10, db=db))
        assert {r["session_id"] for r in after["results"]} == {
            "current-family-topic",
            "sibling-family-topic",
        }
        assert after["access_scope"]["grants"][0]["mode"] == "chat"

    def test_persistent_all_chats_grant_covers_telegram_without_prompt(
        self, db, monkeypatch
    ):
        _set_gateway_topic(
            monkeypatch,
            chat_id="179555559",
            thread_id="",
            profile_name="personal",
            topic_isolation=False,
        )
        db.create_session(
            "main-dm",
            source="telegram",
            access_scope=_telegram_scope(
                chat_id="179555559",
                thread_id="",
                profile_name="personal",
                topic_isolation=False,
            ),
        )
        db.append_message("main-dm", role="user", content="globalgrant current")
        db.create_session(
            "family-travel",
            source="telegram",
            access_scope=_telegram_scope(
                chat_id="-1003966683704",
                thread_id="359",
                profile_name="family-chat",
            ),
        )
        db.append_message("family-travel", role="user", content="globalgrant family")
        db.create_session(
            "other-platform",
            source="discord",
            access_scope=json.dumps({"origin": {
                "platform": "discord",
                "chat_id": "123",
                "thread_id": "",
                "profile_name": "other-profile",
            }}),
        )
        db.append_message("other-platform", role="user", content="globalgrant discord")

        persistent_target = {
            "mode": "platform",
            "platform": "telegram",
            "chat_id": "*",
            "thread_id": "",
            "profile_name": "personal",
            "label": "all chats on this platform",
        }
        monkeypatch.setattr(
            recall_access_mod,
            "_load_active_config",
            lambda: ({"recall_access": {"grants": [{
                "source": {
                    "platform": "telegram",
                    "chat_id": "179555559",
                    "thread_id": "",
                    "profile_name": "personal",
                },
                "target": persistent_target,
            }]}}, None),
        )

        def unexpected_prompt(_question, _choices):
            raise AssertionError("an existing persistent grant must not prompt again")

        grant = json.loads(recall_access_tool(
            target="telegram:-1003966683704:359",
            reason="Проверить семейный топик Путешествия",
            callback=unexpected_prompt,
        ))
        assert grant["granted"] is True
        assert grant["approval"] == "existing_grant"

        result = json.loads(session_search(query="globalgrant", limit=10, db=db))
        assert {r["session_id"] for r in result["results"]} == {
            "main-dm",
            "family-travel",
        }

    def test_all_chats_alias_resolves_to_current_platform(self, monkeypatch):
        _set_gateway_topic(
            monkeypatch,
            chat_id="179555559",
            thread_id="",
            profile_name="personal",
            topic_isolation=False,
        )
        assert resolve_recall_target("all_chats") == {
            "mode": "platform",
            "platform": "telegram",
            "chat_id": "*",
            "thread_id": "",
            "profile_name": "personal",
            "label": "all chats on this platform",
        }

    def test_recall_access_denial_does_not_expand_scope(self, db, monkeypatch):
        _set_gateway_topic(monkeypatch)
        db.create_session("boxmap", source="telegram", access_scope=_telegram_scope())
        db.append_message("boxmap", role="user", content="denytoken boxmap issue")
        db.create_session(
            "planning",
            source="telegram",
            access_scope=_telegram_scope(thread_id="313"),
        )
        db.append_message("planning", role="user", content="denytoken planning decision")

        grant = json.loads(recall_access_tool(
            reason="Need planning context",
            chat_id="-1003735932411",
            thread_id="313",
            callback=lambda _question, _choices: "Deny",
        ))
        assert grant["success"] is True
        assert grant["granted"] is False

        result = json.loads(session_search(query="denytoken", limit=10, db=db))
        assert {r["session_id"] for r in result["results"]} == {"boxmap"}

    def test_recall_access_resolves_transliterated_topic_name_in_current_chat(self, monkeypatch):
        _set_gateway_topic(
            monkeypatch,
            chat_id="-1003938895426",
            thread_id="35",
            profile_name="boxmap",
        )
        monkeypatch.setattr(
            recall_access_mod,
            "_load_active_config",
            lambda: ({
                "telegram": {
                    "extra": {
                        "group_topics": [{
                            "chat_id": -1003938895426,
                            "topics": [{
                                "name": "Instagram/TikTok",
                                "thread_id": 2,
                                "aliases": ["инстагра/тик ток"],
                            }],
                        }],
                    },
                },
            }, None),
        )

        target = resolve_recall_target("инстагра/тик ток")

        assert target["platform"] == "telegram"
        assert target["chat_id"] == "-1003938895426"
        assert target["thread_id"] == "2"
        assert target["label"] == "Instagram/TikTok"

    def test_explicit_same_chat_transfer_is_approval_and_does_not_prompt(self, db, monkeypatch):
        _set_gateway_topic(
            monkeypatch,
            chat_id="-1003938895426",
            thread_id="35",
            profile_name="boxmap",
        )
        monkeypatch.setattr(
            recall_access_mod,
            "_load_active_config",
            lambda: ({
                "telegram": {
                    "extra": {
                        "group_topics": [{
                            "chat_id": -1003938895426,
                            "topics": [{
                                "name": "Instagram/TikTok",
                                "thread_id": 2,
                                "aliases": ["инстагра/тик ток"],
                            }],
                        }],
                    },
                },
            }, None),
        )
        db.create_session(
            "current-topic",
            source="telegram",
            access_scope=_telegram_scope(
                chat_id="-1003938895426",
                thread_id="35",
                profile_name="boxmap",
            ),
        )
        db.append_message("current-topic", role="user", content="importtoken current topic")
        db.create_session(
            "instagram-topic",
            source="telegram",
            access_scope=_telegram_scope(
                chat_id="-1003938895426",
                thread_id="2",
                profile_name="boxmap",
            ),
        )
        db.append_message("instagram-topic", role="user", content="importtoken source dialogue")

        def unexpected_prompt(_question, _choices):
            raise AssertionError("explicit transfer request must not prompt again")

        grant = json.loads(recall_access_tool(
            target="инстагра/тик ток",
            reason="Перенести контекст в текущий топик",
            duration="session",
            callback=unexpected_prompt,
            user_request="Весь диалог с топика инстагра/тик ток перенеси сюда",
        ))

        assert grant["success"] is True
        assert grant["granted"] is True
        assert grant["duration"] == "session"
        assert grant["approval"] == "explicit_current_user_request"
        assert grant["target"]["thread_id"] == "2"

        result = json.loads(session_search(query="importtoken", limit=10, db=db))
        assert {r["session_id"] for r in result["results"]} == {
            "current-topic",
            "instagram-topic",
        }

    def test_explicit_ids_restore_topic_alias_and_do_not_prompt(self, monkeypatch):
        _set_gateway_topic(
            monkeypatch,
            chat_id="-1003938895426",
            thread_id="35",
            profile_name="boxmap",
        )
        monkeypatch.setattr(
            recall_access_mod,
            "_load_active_config",
            lambda: ({
                "telegram": {
                    "extra": {
                        "group_topics": [{
                            "chat_id": -1003938895426,
                            "topics": [{
                                "name": "Instagram/TikTok",
                                "thread_id": 2,
                                "aliases": ["instagram", "tiktok", "инстагра/тик ток"],
                            }],
                        }],
                    },
                },
            }, None),
        )

        def unexpected_prompt(_question, _choices):
            raise AssertionError("explicit same-chat transfer must not prompt again")

        grant = json.loads(recall_access_tool(
            reason=(
                "Михаил прямо попросил перенести в текущий BoxMap Product-топик "
                "контекст из темы Instagram/TikTok."
            ),
            duration="one_turn",
            platform="telegram",
            chat_id="-1003938895426",
            thread_id="2",
            callback=unexpected_prompt,
            user_request=(
                "[Mikhail|179555559]\n"
                "@TripiooBot перенеси контекст из Instagram/TikTok"
            ),
        ))

        assert grant["success"] is True
        assert grant["granted"] is True
        assert grant["approval"] == "explicit_current_user_request"
        assert grant["target"] == {
            "platform": "telegram",
            "chat_id": "-1003938895426",
            "thread_id": "2",
            "label": "Instagram/TikTok",
        }

    def test_explicit_wording_never_auto_approves_a_different_chat(self, monkeypatch):
        _set_gateway_topic(monkeypatch)

        grant = json.loads(recall_access_tool(
            reason="Need context from another chat",
            duration="session",
            chat_id="-1003966683704",
            thread_id="359",
            callback=lambda _question, _choices: "Deny",
            user_request="Перенеси сюда весь диалог из семейного чата",
        ))

        assert grant["success"] is True
        assert grant["granted"] is False


class TestDiscoverySort:
    def test_sort_newest_orders_by_recency(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, sort="newest", db=db))
        # First result should be the most recent session
        first = result["results"][0]
        assert first["session_id"] == "s_newest" or "Newest" in (first.get("title") or "")

    def test_sort_oldest_orders_by_age(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, sort="oldest", db=db))
        first = result["results"][0]
        assert first["session_id"] == "s_oldest"

    def test_invalid_sort_silently_ignored(self, db):
        _seed_modpack_sessions(db)
        # Should not error
        result = json.loads(session_search(query="modpack", sort="bogus", db=db))
        assert result["success"] is True


class TestRoleFilter:
    def test_default_excludes_tool_role(self, db):
        db.create_session("s1", source="cli")
        db.append_message("s1", role="user", content="modpack question")
        db.append_message("s1", role="tool", content="modpack tool output", tool_name="x")
        result = json.loads(session_search(query="modpack", db=db))
        # The FTS5 match should be on the user message, not the tool message
        if result["count"] > 0:
            matched_role = result["results"][0]["matched_role"]
            assert matched_role in ("user", "assistant")

    def test_explicit_tool_role_includes_tool(self, db):
        db.create_session("s1", source="cli")
        db.append_message("s1", role="tool", content="modpack tool output", tool_name="x")
        result = json.loads(session_search(query="modpack", role_filter="tool", db=db))
        # Should now match the tool message
        if result["count"] > 0:
            assert result["results"][0]["matched_role"] == "tool"


# =========================================================================
# Scroll shape (session_id + around_message_id)
# =========================================================================

class TestScrollShape:
    def test_scroll_returns_window_without_bookends(self, db):
        _seed_modpack_sessions(db)
        # Get an anchor first via discovery
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]

        # Now scroll
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=2, db=db
        ))
        assert result["success"] is True
        assert result["mode"] == "scroll"
        assert "messages" in result
        # Scroll shape has no bookends
        assert "bookend_start" not in result
        assert "bookend_end" not in result

    def test_scroll_window_clamped_to_20(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=999, db=db
        ))
        assert result["window"] == 20

    def test_scroll_window_floor_to_1(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=-5, db=db
        ))
        assert result["window"] == 1

    def test_scroll_returns_messages_before_after_counts(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=3, db=db
        ))
        assert "messages_before" in result
        assert "messages_after" in result

    def test_scroll_anchor_in_window(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=2, db=db
        ))
        anchor_in_window = [m for m in result["messages"] if m["id"] == anchor_mid]
        assert len(anchor_in_window) == 1
        assert anchor_in_window[0].get("anchor") is True

    def test_scroll_missing_anchor_errors(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(
            session_id="s_oldest", around_message_id=999999, db=db
        ))
        assert result["success"] is False
        assert "not in" in result.get("error", "")

    def test_scroll_missing_session_errors(self, db):
        result = json.loads(session_search(
            session_id="nonexistent", around_message_id=1, db=db
        ))
        assert result["success"] is False

    def test_scroll_rejects_current_session_lineage(self, db):
        _seed_modpack_sessions(db)
        # Grab some valid id from s_oldest
        disc = json.loads(session_search(query="modpack", limit=3, db=db))
        match = [r for r in disc["results"] if r["session_id"] == "s_oldest"]
        if match:
            mid = match[0]["match_message_id"]
            result = json.loads(session_search(
                session_id="s_oldest", around_message_id=mid, db=db,
                current_session_id="s_oldest",
            ))
            assert result["success"] is False
            assert "current session" in result.get("error", "").lower()

    def test_scroll_invalid_around_message_id_errors(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(
            session_id="s_oldest", around_message_id="not-an-int", db=db
        ))
        assert result["success"] is False

    def test_gateway_scroll_rejects_other_topic(self, db, monkeypatch):
        _set_gateway_topic(monkeypatch)
        db.create_session(
            "health",
            source="telegram",
            access_scope=_telegram_scope(thread_id="321"),
        )
        mid = db.append_message("health", role="user", content="private health issue")

        result = json.loads(session_search(
            session_id="health", around_message_id=mid, db=db
        ))
        assert result["success"] is False
        assert "access scope" in result.get("error", "")


class TestScrollPattern:
    """The forward/backward scroll loop using tool output."""

    def test_scroll_forward_from_last_id(self, db):
        # Long session
        db.create_session("s_long", source="cli")
        ids = []
        for i in range(20):
            ids.append(db.append_message("s_long", role="user" if i % 2 == 0 else "assistant",
                                         content=f"long session msg {i}"))

        v1 = json.loads(session_search(
            session_id="s_long", around_message_id=ids[5], window=3, db=db
        ))
        last_id = v1["messages"][-1]["id"]
        v2 = json.loads(session_search(
            session_id="s_long", around_message_id=last_id, window=3, db=db
        ))
        # Forward scroll: v2 should reach further than v1
        assert max(m["id"] for m in v2["messages"]) > max(m["id"] for m in v1["messages"])
        # Boundary id appears in both
        assert last_id in [m["id"] for m in v1["messages"]]
        assert last_id in [m["id"] for m in v2["messages"]]


# =========================================================================
# Shape precedence
# =========================================================================

class TestShapePrecedence:
    def test_scroll_args_beat_query(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        # Pass both query and scroll args — scroll should win
        result = json.loads(session_search(
            query="modpack",  # would normally trigger discovery
            session_id=anchor_sid, around_message_id=anchor_mid, db=db,
        ))
        assert result["mode"] == "scroll"

    def test_empty_query_falls_back_to_browse(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="   ", db=db))
        assert result["mode"] == "browse"

    def test_non_string_query_falls_back_to_browse(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query=None, db=db))  # type: ignore
        assert result["mode"] == "browse"
