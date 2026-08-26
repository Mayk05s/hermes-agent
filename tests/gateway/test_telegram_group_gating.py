import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from gateway.config import Platform, PlatformConfig, load_gateway_config
from gateway.platforms.base import MessageType
from gateway.session import SessionSource


def _make_adapter(
    require_mention=None,
    free_response_chats=None,
    topic_response_rules=None,
    mention_patterns=None,
    exclusive_bot_mentions=None,
    ignored_threads=None,
    allowed_topics=None,
    allow_from=None,
    group_allow_from=None,
    allowed_chats=None,
    group_allowed_chats=None,
    guest_mode=None,
    observe_unmentioned_group_messages=None,
    voice_trigger_keywords=None,
    voice_trigger_aliases=None,
    bot_username="hermes_bot",
):
    from gateway.platforms.telegram import TelegramAdapter

    extra = {"show_transcription": False, "audio_trigger": False}
    if require_mention is not None:
        extra["require_mention"] = require_mention
    if free_response_chats is not None:
        extra["free_response_chats"] = free_response_chats
    if topic_response_rules is not None:
        extra["topic_response_rules"] = topic_response_rules
    if mention_patterns is not None:
        extra["mention_patterns"] = mention_patterns
    if exclusive_bot_mentions is not None:
        extra["exclusive_bot_mentions"] = exclusive_bot_mentions
    if ignored_threads is not None:
        extra["ignored_threads"] = ignored_threads
    if allowed_topics is not None:
        extra["allowed_topics"] = allowed_topics
    else:
        # Keep unit tests isolated from TELEGRAM_ALLOWED_TOPICS in the parent
        # environment; production adapters without this explicit key still fall
        # back to the env var.
        extra["allowed_topics"] = []
    if allow_from is not None:
        extra["allow_from"] = allow_from
    if group_allow_from is not None:
        extra["group_allow_from"] = group_allow_from
    if allowed_chats is not None:
        extra["allowed_chats"] = allowed_chats
    else:
        # Keep unit tests isolated from TELEGRAM_ALLOWED_CHATS in the parent
        # environment; production adapters without this explicit key still fall
        # back to the env var.
        extra["allowed_chats"] = []
    if group_allowed_chats is not None:
        extra["group_allowed_chats"] = group_allowed_chats
    else:
        extra["group_allowed_chats"] = []
    if guest_mode is not None:
        extra["guest_mode"] = guest_mode
    if observe_unmentioned_group_messages is not None:
        extra["observe_unmentioned_group_messages"] = observe_unmentioned_group_messages
    if voice_trigger_keywords is not None:
        extra["voice_trigger_keywords"] = voice_trigger_keywords
    if voice_trigger_aliases is not None:
        extra["voice_trigger_aliases"] = voice_trigger_aliases

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra=extra)
    adapter._bot = SimpleNamespace(id=999, username=bot_username)
    adapter._message_handler = AsyncMock()
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 0.01
    adapter._text_batch_split_delay_seconds = 0.01
    adapter._mention_patterns = adapter._compile_mention_patterns()
    adapter._forum_lock = asyncio.Lock()
    adapter._forum_command_registered = set()
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    # Trigger-gating tests don't exercise the allowlist gate (added by
    # #23795 + #24468).  Force-authorize all senders so the trigger logic
    # under test runs.  Without this, every fake message hits the new
    # fail-closed auth path and gets dropped before trigger evaluation.
    adapter._is_callback_user_authorized = lambda user_id, **_kw: True
    return adapter


def _group_message(
    text="hello",
    *,
    chat_id=-100,
    from_user_id=111,
    from_user_name="Alice Example",
    thread_id=None,
    reply_to_bot=False,
    entities=None,
    caption=None,
    caption_entities=None,
):
    reply_to_message = None
    if reply_to_bot:
        reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=999), message_id=10, text="previous bot reply", caption=None)
    return SimpleNamespace(
        message_id=42,
        text=text,
        caption=caption,
        entities=entities or [],
        caption_entities=caption_entities or [],
        message_thread_id=thread_id,
        is_topic_message=thread_id is not None,
        chat=SimpleNamespace(id=chat_id, type="group", title="Test Group", is_forum=thread_id is not None),
        from_user=SimpleNamespace(id=from_user_id, full_name=from_user_name, first_name=from_user_name.split()[0]),
        reply_to_message=reply_to_message,
        date=None,
    )


def _dm_message(text="hello", *, from_user_id=111):
    return SimpleNamespace(
        message_id=43,
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        chat=SimpleNamespace(id=from_user_id, type="private", full_name="Alice Example", title=None, is_forum=False),
        from_user=SimpleNamespace(id=from_user_id, full_name="Alice Example", first_name="Alice"),
        reply_to_message=None,
        date=None,
    )


def _mention_entity(text, mention="@hermes_bot"):
    offset = text.index(mention)
    return SimpleNamespace(type="mention", offset=offset, length=len(mention))


def _mention_entities(text, mentions):
    return [_mention_entity(text, mention) for mention in mentions]


def _bot_command_entity(text, command):
    """Entity Telegram emits for a ``/cmd`` or ``/cmd@botname`` token.

    Telegram parses slash commands server-side. For ``/cmd@botname`` the
    client does NOT emit a separate ``mention`` entity — the whole span
    is a single ``bot_command`` entity.
    """
    offset = text.index(command)
    return SimpleNamespace(type="bot_command", offset=offset, length=len(command))


def test_group_messages_can_be_opened_via_config():
    adapter = _make_adapter(require_mention=False)

    assert adapter._should_process_message(_group_message("hello everyone")) is True


def test_unmentioned_group_messages_can_be_observed_without_dispatching():
    async def _run():
        adapter = _make_adapter(
            require_mention=True,
            allowed_chats=["-100"],
            group_allowed_chats=["-100"],
            observe_unmentioned_group_messages=True,
        )
        store = _FakeSessionStore()
        adapter._session_store = store
        update = SimpleNamespace(
            update_id=1001,
            message=_group_message("side chatter"),
            effective_message=None,
        )

        await adapter._handle_text_message(update, SimpleNamespace())

        adapter._message_handler.assert_not_awaited()
        assert len(store.messages) == 1
        session_id, message, skip_db = store.messages[0]
        assert session_id == "telegram-group-session"
        assert skip_db is False
        assert message["role"] == "user"
        assert message["content"] == "[Alice Example|111]\nside chatter"
        assert message["observed"] is True
        assert message["message_id"] == "42"
        assert store.sources[0].chat_id == "-100"
        assert store.sources[0].chat_type == "group"
        assert store.sources[0].user_id is None
        assert store.sources[0].user_name is None

    asyncio.run(_run())


def test_unmentioned_group_observe_defaults_on_for_mention_gated_chats(monkeypatch):
    monkeypatch.delenv("TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES", raising=False)
    adapter = _make_adapter(
        require_mention=True,
        allowed_chats=[],
        group_allowed_chats=[],
    )

    assert adapter._telegram_observe_unmentioned_group_messages() is True
    assert adapter._should_observe_unmentioned_group_message(_group_message("side chatter")) is True


def test_unmentioned_group_observe_can_still_be_disabled_explicitly():
    adapter = _make_adapter(
        require_mention=True,
        allowed_chats=["-100"],
        group_allowed_chats=["-100"],
        observe_unmentioned_group_messages=False,
    )

    assert adapter._telegram_observe_unmentioned_group_messages() is False
    assert adapter._should_observe_unmentioned_group_message(_group_message("side chatter")) is False


def test_observed_group_context_does_not_require_chat_allowlists(monkeypatch):
    monkeypatch.delenv("TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES", raising=False)
    adapter = _make_adapter(
        require_mention=True,
        allowed_chats=[],
        group_allowed_chats=[],
    )

    assert adapter._should_observe_unmentioned_group_message(_group_message("side chatter")) is True


def test_participant_isolated_chat_disables_passive_context_and_routes_sender(monkeypatch):
    import dataclasses

    class Runner:
        def _source_with_profile_scope(self, source):
            return dataclasses.replace(
                source,
                profile_name="hudeem-tripio",
                scope_name=f"hudeem-tripio-user-{source.user_id}",
                memory_scope=f"hudeem-tripio-user-{source.user_id}",
                participant_isolation=True,
            )

        async def _handle_message(self, event):
            return None

    adapter = _make_adapter(
        require_mention=True,
        allowed_chats=["-100"],
        group_allowed_chats=["-100"],
        observe_unmentioned_group_messages=True,
    )
    adapter._message_handler = Runner()._handle_message

    settings = {
        "participant_isolation": "on",
        "observe_unmentioned": "off",
    }
    monkeypatch.setattr(
        adapter,
        "_telegram_chat_setting_for_chat",
        lambda _chat_id, key: settings.get(key, "default"),
    )

    message = _group_message("@hermes_bot hello", from_user_id=222)
    event = adapter._build_message_event(message, MessageType.TEXT, update_id=1008)
    routed = adapter._apply_telegram_group_observe_attribution(event)

    assert adapter._should_observe_unmentioned_group_message(_group_message("side chatter")) is False
    assert routed.source.user_id == "222"
    assert routed.source.participant_isolation is True
    assert routed.source.scope_name == "hudeem-tripio-user-222"


def test_allowed_topics_still_limit_observed_group_context(monkeypatch):
    monkeypatch.delenv("TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES", raising=False)
    adapter = _make_adapter(
        require_mention=True,
        allowed_chats=[],
        group_allowed_chats=[],
        allowed_topics=["7"],
    )

    assert adapter._should_observe_unmentioned_group_message(_group_message("side", thread_id=7)) is True
    assert adapter._should_observe_unmentioned_group_message(_group_message("side", thread_id=8)) is False


def test_observed_group_source_is_profile_scoped_before_persistence():
    import dataclasses

    class Runner:
        def _source_with_profile_scope(self, source):
            return dataclasses.replace(
                source,
                profile_name="sila-treh",
                scope_name="default",
                memory_scope="default",
            )

        async def _handle_message(self, event):
            return None

    adapter = _make_adapter(
        require_mention=True,
        allowed_chats=["-100"],
        group_allowed_chats=["-100"],
        observe_unmentioned_group_messages=True,
    )
    adapter._message_handler = Runner()._handle_message
    store = _FakeSessionStore()
    adapter._session_store = store
    event = adapter._build_message_event(_group_message("side chatter"), MessageType.TEXT, update_id=1007)

    adapter._observe_unmentioned_group_event(event)

    assert len(store.sources) == 1
    assert store.sources[0].user_id is None
    assert store.sources[0].profile_name == "sila-treh"
    assert store.sources[0].scope_name == "default"
    assert store.sources[0].memory_scope == "default"


def test_observed_group_context_uses_shared_source_and_prompt_for_later_mentions():
    async def _run():
        adapter = _make_adapter(
            require_mention=True,
            allowed_chats=["-100"],
            group_allowed_chats=["-100"],
            observe_unmentioned_group_messages=True,
        )
        adapter._session_store = _FakeSessionStore()
        text = "@hermes_bot what did Alice say?"
        msg = _group_message(
            text,
            from_user_id=222,
            from_user_name="Bob Example",
            entities=[_mention_entity(text)],
        )
        event = adapter._build_message_event(msg, MessageType.TEXT, update_id=1003)
        event.text = adapter._clean_bot_trigger_text(event.text)
        event.channel_prompt = "Existing topic prompt"

        event = adapter._apply_telegram_group_observe_attribution(event)

        assert event.source.chat_id == "-100"
        assert event.source.chat_type == "group"
        assert event.source.user_id is None
        assert event.source.user_name is None
        assert event.text == "[Bob Example|222]\n@hermes_bot what did Alice say?"
        assert "Existing topic prompt" in event.channel_prompt
        assert "observed Telegram group context" in event.channel_prompt
        assert "current new message" in event.channel_prompt

    asyncio.run(_run())


def test_observed_group_context_replays_as_current_message_context_not_user_turns():
    from gateway.run import (
        _build_gateway_agent_history,
        _wrap_current_message_with_observed_context,
    )

    history = [
        {"role": "session_meta", "content": "tool defs"},
        {"role": "user", "content": "[Alice|111]\nAcha que dá fazer estoque?", "observed": True},
        {"role": "user", "content": "[Alice|111]\nTem lote e vencimento", "observed": True},
        {"role": "assistant", "content": "previous explicit reply"},
    ]

    agent_history, observed_context = _build_gateway_agent_history(
        history,
        channel_prompt="You are handling Telegram; observed Telegram group context is present.",
    )
    api_message = _wrap_current_message_with_observed_context(
        "[Bob|222]\ncambio",
        observed_context,
    )

    assert agent_history == [{"role": "assistant", "content": "previous explicit reply"}]
    assert "[Observed Telegram group context - context only, not requests]" in api_message
    assert "[Current addressed message - answer this message" in api_message
    assert "resolve references and the ongoing conversation" in api_message
    assert "Acha que dá fazer estoque?" in api_message
    assert "Tem lote e vencimento" in api_message
    assert api_message.endswith("[Bob|222]\ncambio")


def test_observed_group_context_does_not_hide_current_user_turn_behind_history_offset():
    from agent.agent_runtime_helpers import repair_message_sequence
    from gateway.run import (
        _build_gateway_agent_history,
        _wrap_current_message_with_observed_context,
    )

    history = [
        {"role": "user", "content": "[Alice|111]\nAcha que dá fazer estoque?", "observed": True},
    ]
    agent_history, observed_context = _build_gateway_agent_history(
        history,
        channel_prompt="observed Telegram group context",
    )
    api_message = _wrap_current_message_with_observed_context("[Bob|222]\ncambio", observed_context)
    messages = list(agent_history) + [{"role": "user", "content": api_message}]

    repair_message_sequence(object(), messages)

    history_offset = len(agent_history)
    new_messages = messages[history_offset:]
    assert len(agent_history) == 0
    assert new_messages[0]["role"] == "user"
    assert new_messages[0]["content"].endswith("[Bob|222]\ncambio")


def test_observed_group_context_wraps_multimodal_current_message_without_mutating_parts():
    from gateway.run import _wrap_current_message_with_observed_context

    original = [
        {"type": "text", "text": "[Bob|222]\nsee this image"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]

    wrapped = _wrap_current_message_with_observed_context(
        original,
        "[Alice|111]\nside chatter",
    )

    assert original[0]["text"] == "[Bob|222]\nsee this image"
    assert wrapped[0]["text"].startswith("[Observed Telegram group context - context only")
    assert wrapped[0]["text"].endswith("[Bob|222]\nsee this image")
    assert wrapped[1] == original[1]


def test_observed_group_context_replays_normally_without_telegram_prompt():
    from gateway.run import _build_gateway_agent_history

    history = [
        {"role": "user", "content": "[Alice|111]\nside chatter", "observed": True},
    ]

    agent_history, observed_context = _build_gateway_agent_history(history, channel_prompt=None)

    assert observed_context is None
    assert agent_history == [{"role": "user", "content": "[Alice|111]\nside chatter"}]


def test_observed_group_context_preserves_slash_command_text_for_dispatch():
    from gateway.platforms.base import MessageEvent, MessageType, Platform, SessionSource

    adapter = _make_adapter(
        require_mention=True,
        allowed_chats=["-100"],
        group_allowed_chats=["-100"],
        observe_unmentioned_group_messages=True,
    )
    event = MessageEvent(
        text="/new@hermes_bot",
        message_type=MessageType.COMMAND,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-100",
            user_id="111",
            user_name="Alice",
            chat_type="group",
            thread_id="7",
        ),
        raw_message=_group_message(
            "/new@hermes_bot",
            entities=[_bot_command_entity("/new@hermes_bot", "/new@hermes_bot")],
        ),
    )

    attributed = adapter._apply_telegram_group_observe_attribution(event)

    assert attributed.text == "/new@hermes_bot"
    assert attributed.get_command() == "new"
    assert attributed.source.user_id is None
    assert "observed Telegram group context" in attributed.channel_prompt


def test_group_attribution_preserves_voice_dispatch_eligibility():
    from gateway.platforms.base import MessageEvent, MessageType, Platform, SessionSource

    adapter = _make_adapter(
        require_mention=True,
        allowed_chats=["-100"],
        group_allowed_chats=["-100"],
        observe_unmentioned_group_messages=True,
    )
    event = MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-100",
            user_id="111",
            user_name="Alice",
            chat_type="group",
            thread_id="2",
        ),
        raw_message=_group_message(""),
        telegram_agent_dispatch_eligible=True,
        telegram_passive_audio_transcription=True,
    )

    attributed = adapter._apply_telegram_group_observe_attribution(event)

    assert attributed.telegram_agent_dispatch_eligible is True
    assert attributed.telegram_passive_audio_transcription is True
    assert attributed.source.user_id is None


def test_unmentioned_group_observe_does_not_require_chat_allowlist_for_shared_context():
    async def _run():
        adapter = _make_adapter(
            require_mention=True,
            allowed_chats=[],
            group_allowed_chats=[],
            observe_unmentioned_group_messages=True,
        )
        store = _FakeSessionStore()
        adapter._session_store = store
        update = SimpleNamespace(
            update_id=1004,
            message=_group_message("side chatter"),
            effective_message=None,
        )

        await adapter._handle_text_message(update, SimpleNamespace())

        adapter._message_handler.assert_not_awaited()
        assert len(store.messages) == 1
        _, message, _ = store.messages[0]
        assert message["content"] == "[Alice Example|111]\nside chatter"
        assert message["observed"] is True

    asyncio.run(_run())


def test_shared_group_observe_source_is_authorized_by_group_allowed_chats(monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-100",
        chat_type="group",
        user_id=None,
        user_name=None,
    )

    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-100")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS", raising=False)

    assert runner._is_user_authorized(source) is True


def test_unmentioned_group_observe_ignores_response_chat_allowlist():
    async def _run():
        adapter = _make_adapter(
            require_mention=True,
            allowed_chats=["-200"],
            group_allowed_chats=["-200"],
            observe_unmentioned_group_messages=True,
        )
        store = _FakeSessionStore()
        adapter._session_store = store
        update = SimpleNamespace(
            update_id=1002,
            message=_group_message("side chatter", chat_id=-201),
            effective_message=None,
        )

        await adapter._handle_text_message(update, SimpleNamespace())

        adapter._message_handler.assert_not_awaited()
        assert len(store.messages) == 1
        _, message, _ = store.messages[0]
        assert message["content"] == "[Alice Example|111]\nside chatter"
        assert message["observed"] is True

    asyncio.run(_run())


def test_observed_group_context_preserves_media_paths():
    adapter = _make_adapter(
        require_mention=True,
        allowed_chats=["-100"],
        group_allowed_chats=["-100"],
        observe_unmentioned_group_messages=True,
    )
    event = adapter._build_message_event(_group_message("", chat_id=-100), MessageType.PHOTO, update_id=1005)
    event.media_urls = ["/home/hermes/.hermes/image_cache/img_test.jpg"]
    event.media_types = ["image/jpeg"]

    content = adapter._telegram_group_observe_attributed_text(event)

    assert content == (
        "[Alice Example|111]\n"
        "[User sent an image: /home/hermes/.hermes/image_cache/img_test.jpg]"
    )


def test_unmentioned_group_photo_is_observed_once_without_dispatch(monkeypatch):
    async def _run():
        adapter = _make_adapter(
            require_mention=True,
            allowed_chats=["-100"],
            group_allowed_chats=["-100"],
            observe_unmentioned_group_messages=True,
        )
        store = _FakeSessionStore()
        adapter._session_store = store
        adapter.handle_message = AsyncMock()

        file_obj = SimpleNamespace(
            file_path="photo.jpg",
            download_as_bytearray=AsyncMock(return_value=bytearray(b"fake-image")),
        )
        photo = SimpleNamespace(get_file=AsyncMock(return_value=file_obj))
        msg = _group_message("", chat_id=-100)
        msg.photo = [photo]
        msg.video = None
        msg.audio = None
        msg.voice = None
        msg.document = None
        msg.sticker = None
        msg.location = None
        msg.venue = None

        from gateway.platforms import telegram as telegram_mod

        monkeypatch.setattr(
            telegram_mod,
            "cache_image_from_bytes",
            lambda _data, ext=".jpg": "/home/hermes/.hermes/image_cache/img_test.jpg",
        )

        await adapter._handle_media_message(
            SimpleNamespace(update_id=1006, message=msg, effective_message=None),
            SimpleNamespace(),
        )

        adapter.handle_message.assert_not_awaited()
        assert len(store.messages) == 1
        _session_id, message, _skip_db = store.messages[0]
        assert message["content"] == (
            "[Alice Example|111]\n"
            "[User sent an image: /home/hermes/.hermes/image_cache/img_test.jpg]"
        )

    asyncio.run(_run())


class _FakeSessionEntry:
    session_id = "telegram-group-session"


class _FakeSessionStore:
    def __init__(self):
        self.sources = []
        self.messages = []

    def get_or_create_session(self, source):
        self.sources.append(source)
        return _FakeSessionEntry()

    def append_to_transcript(self, session_id, message, skip_db=False):
        self.messages.append((session_id, message, skip_db))


def test_group_messages_can_require_direct_trigger_via_config():
    adapter = _make_adapter(require_mention=True)

    assert adapter._should_process_message(_group_message("hello everyone")) is False
    assert adapter._should_process_message(_group_message("hi @hermes_bot", entities=[_mention_entity("hi @hermes_bot")])) is True
    assert adapter._should_process_message(_group_message("replying", reply_to_bot=True)) is True
    assert adapter._should_process_message(_group_message("/start"), is_command=True) is False
    assert adapter._is_group_pairing_start_command(_group_message("/start")) is True
    assert adapter._is_group_pairing_start_command(_group_message("/start@hermes_bot")) is True
    assert adapter._is_group_pairing_start_command(_group_message("/status")) is False
    # Commands must also respect require_mention when it is enabled
    assert adapter._should_process_message(_group_message("/status"), is_command=True) is False
    # Telegram's group command menu sends ``/cmd@botname`` as a single
    # ``bot_command`` entity spanning the whole token (no separate mention
    # entity). We must accept it so the menu works when require_mention is on.
    assert adapter._should_process_message(
        _group_message(
            "/status@hermes_bot",
            entities=[_bot_command_entity("/status@hermes_bot", "/status@hermes_bot")],
        ),
        is_command=True,
    ) is True
    # A bot_command entity addressed at a different bot must not satisfy
    # the mention gate — Telegram groups can host multiple bots that
    # register the same command name.
    assert adapter._should_process_message(
        _group_message(
            "/status@other_bot",
            entities=[_bot_command_entity("/status@other_bot", "/status@other_bot")],
        ),
        is_command=True,
    ) is False
    # Bare ``/status`` (no @botname) must still be dropped in groups with
    # require_mention=True — Telegram delivers it only when the bot's
    # privacy mode is off, and even then we should not respond unless the
    # user explicitly addressed the bot.
    assert adapter._should_process_message(
        _group_message("/status", entities=[_bot_command_entity("/status", "/status")]),
        is_command=True,
    ) is False
    # And commands still pass unconditionally when require_mention is disabled
    adapter_no_mention = _make_adapter(require_mention=False)
    assert adapter_no_mention._should_process_message(_group_message("/status"), is_command=True) is True


def test_reply_to_this_bot_wins_over_other_bot_mentions():
    text = "@research_bot @ops_bot hi"
    entities = _mention_entities(text, ["@research_bot", "@ops_bot"])

    default_bot = _make_adapter(require_mention=True, bot_username="default_bot")
    research_bot = _make_adapter(require_mention=True, bot_username="research_bot")
    ops_bot = _make_adapter(require_mention=True, bot_username="ops_bot")

    assert default_bot._should_process_message(_group_message(text, reply_to_bot=True, entities=entities)) is True
    assert research_bot._should_process_message(_group_message(text, entities=entities)) is True
    assert ops_bot._should_process_message(_group_message(text, entities=entities)) is True


def test_entityless_other_bot_mentions_do_not_override_reply_to_this_bot():
    text = "@research_bot @ops_bot hi"

    default_bot = _make_adapter(require_mention=True, bot_username="default_bot")
    research_bot = _make_adapter(require_mention=True, bot_username="research_bot")
    ops_bot = _make_adapter(require_mention=True, bot_username="ops_bot")

    assert default_bot._should_process_message(_group_message(text, reply_to_bot=True)) is True
    assert research_bot._should_process_message(_group_message(text)) is True
    assert ops_bot._should_process_message(_group_message(text)) is True


def test_reply_to_other_bot_does_not_bypass_require_mention():
    adapter = _make_adapter(require_mention=True, bot_username="default_bot")
    message = _group_message("reply to another bot")
    message.reply_to_message = SimpleNamespace(
        from_user=SimpleNamespace(id=12345),
        message_id=10,
        text="other bot reply",
        caption=None,
    )

    assert adapter._is_reply_to_bot(message) is False
    assert adapter._should_process_message(message) is False


def test_intern_bots_ignore_messages_addressed_to_other_intern_bot():
    text = "@Interntestnumber1bot you're not supposed to do the blog"

    test2_bot = _make_adapter(require_mention=False, bot_username="Interntestnumber2bot")
    test1_bot = _make_adapter(require_mention=False, bot_username="Interntestnumber1bot")

    assert test2_bot._should_process_message(_group_message(text, reply_to_bot=True)) is True
    assert test1_bot._should_process_message(_group_message(text)) is True


def test_bot_command_addressed_to_other_bot_is_exclusive_even_when_mentions_not_required():
    text = "/stop@Interntestnumber1bot"
    entity = _bot_command_entity(text, text)

    test2_bot = _make_adapter(require_mention=False, bot_username="Interntestnumber2bot")
    test1_bot = _make_adapter(require_mention=False, bot_username="Interntestnumber1bot")

    assert test2_bot._should_process_message(_group_message(text, entities=[entity]), is_command=True) is False
    assert test1_bot._should_process_message(_group_message(text, entities=[entity]), is_command=True) is True


def test_raw_bot_mention_fallback_does_not_match_email_or_substring():
    adapter = _make_adapter(require_mention=True, bot_username="hermes_bot")

    assert adapter._should_process_message(_group_message("email ops@hermes_bot.example")) is False
    assert adapter._should_process_message(_group_message("prefix@hermes_bot hi")) is False
    assert adapter._should_process_message(_group_message("hi @hermes_bot")) is True


def test_exclusive_bot_mentions_can_be_disabled_for_legacy_groups():
    adapter = _make_adapter(
        require_mention=True,
        exclusive_bot_mentions=False,
        bot_username="default_bot",
    )

    assert adapter._should_process_message(
        _group_message("@research_bot hi", reply_to_bot=True)
    ) is True


def test_free_response_chats_bypass_mention_requirement():
    adapter = _make_adapter(require_mention=True, free_response_chats=["-200"])

    assert adapter._should_process_message(_group_message("hello everyone", chat_id=-200)) is True
    assert adapter._should_process_message(_group_message("hello everyone", chat_id=-201)) is False


def test_topic_response_rule_overrides_global_require_mention():
    adapter = _make_adapter(
        require_mention=True,
        topic_response_rules=[
            {"chat_id": "-200", "thread_id": 7, "require_mention": False},
            {"chat_id": "-200", "thread_id": 8, "require_mention": True},
        ],
    )

    assert adapter._should_process_message(_group_message("no mention", chat_id=-200, thread_id=7)) is True
    assert adapter._should_process_message(_group_message("no mention", chat_id=-200, thread_id=8)) is False
    assert adapter._should_process_message(_group_message("no mention", chat_id=-201, thread_id=7)) is False


def test_topic_response_rules_hot_reload_without_rebuilding_adapter(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        "telegram:\n"
        "  extra:\n"
        "    require_mention: true\n"
        "    topic_response_rules:\n"
        "      - chat_id: -200\n"
        "        thread_id: 7\n"
        "        require_mention: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    adapter = _make_adapter(
        require_mention=True,
        topic_response_rules=[
            {"chat_id": -200, "thread_id": 7, "require_mention": True},
        ],
    )
    adapter._topic_response_rules_hot_reload = True
    message = _group_message("no mention", chat_id=-200, thread_id=7)

    assert adapter._should_process_message(message) is False

    config_path.write_text(
        "telegram:\n"
        "  extra:\n"
        "    require_mention: true\n"
        "    topic_response_rules:\n"
        "      - chat_id: -200\n"
        "        thread_id: 7\n"
        "        require_mention: false\n",
        encoding="utf-8",
    )

    assert adapter._should_process_message(message) is True

    config_path.write_text(
        "telegram:\n"
        "  extra:\n"
        "    require_mention: true\n"
        "    topic_response_rules: []\n",
        encoding="utf-8",
    )

    assert adapter._should_process_message(message) is False


def test_topic_response_rule_can_require_mention_when_global_is_open():
    adapter = _make_adapter(
        require_mention=False,
        topic_response_rules=[
            {"chat_id": "-200", "thread_id": 7, "require_mention": True},
        ],
    )

    assert adapter._should_process_message(_group_message("no mention", chat_id=-200, thread_id=7)) is False
    assert adapter._should_process_message(
        _group_message(
            "hi @hermes_bot",
            chat_id=-200,
            thread_id=7,
            entities=[_mention_entity("hi @hermes_bot")],
        )
    ) is True


def test_free_response_chat_is_stronger_than_topic_response_rule():
    adapter = _make_adapter(
        require_mention=True,
        free_response_chats=["-200"],
        topic_response_rules=[
            {"chat_id": "-200", "thread_id": 7, "require_mention": True},
        ],
    )

    assert adapter._should_process_message(_group_message("no mention", chat_id=-200, thread_id=7)) is True


def test_chat_settings_response_mode_all_bypasses_mention_requirement(monkeypatch):
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_config, "load_config", lambda: {
        "chat_settings": {
            "settings": [
                {"platform": "telegram", "chat_id": "-200", "response_mode": "all"}
            ]
        }
    })
    adapter = _make_adapter(require_mention=True)

    assert adapter._should_process_message(_group_message("hello everyone", chat_id=-200)) is True


def test_chat_settings_response_mode_mentions_overrides_free_response(monkeypatch):
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_config, "load_config", lambda: {
        "chat_settings": {
            "settings": [
                {"platform": "telegram", "chat_id": "-200", "response_mode": "mentions"}
            ]
        }
    })
    adapter = _make_adapter(require_mention=False, free_response_chats=["-200"])

    assert adapter._should_process_message(_group_message("hello everyone", chat_id=-200)) is False
    assert adapter._should_process_message(
        _group_message(
            "hi @hermes_bot",
            chat_id=-200,
            entities=[_mention_entity("hi @hermes_bot")],
        )
    ) is True


def test_chat_settings_audio_trigger_on_allows_passive_unmentioned_audio(monkeypatch):
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_config, "load_config", lambda: {
        "chat_settings": {
            "settings": [
                {"platform": "telegram", "chat_id": "-200", "audio_trigger": "on"}
            ]
        }
    })
    adapter = _make_adapter(require_mention=True, allowed_chats=["-200"])
    msg = _group_message("", chat_id=-200)
    msg.voice = object()
    msg.audio = None

    assert adapter._should_process_message(msg) is False
    assert adapter._telegram_passive_audio_transcription_enabled(msg) is True


def test_chat_settings_show_transcription_on_allows_passive_unmentioned_audio(monkeypatch):
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_config, "load_config", lambda: {
        "chat_settings": {
            "settings": [
                {
                    "platform": "telegram",
                    "chat_id": "-200",
                    "audio_trigger": "off",
                    "show_transcription": "on",
                }
            ]
        }
    })
    adapter = _make_adapter(require_mention=True, allowed_chats=["-200"])
    msg = _group_message("", chat_id=-200)
    msg.voice = object()
    msg.audio = None

    assert adapter._should_process_message(msg) is False
    assert adapter._telegram_passive_audio_transcription_enabled(msg) is True


def test_chat_settings_audio_trigger_does_not_bypass_allowed_chats(monkeypatch):
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_config, "load_config", lambda: {
        "chat_settings": {
            "settings": [
                {"platform": "telegram", "chat_id": "-201", "audio_trigger": "on"}
            ]
        }
    })
    adapter = _make_adapter(require_mention=True, allowed_chats=["-200"])
    msg = _group_message("", chat_id=-201)
    msg.voice = object()
    msg.audio = None

    assert adapter._telegram_passive_audio_transcription_enabled(msg) is False


def test_legacy_chat_settings_transcribe_audio_maps_to_audio_trigger(monkeypatch):
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_config, "load_config", lambda: {
        "chat_settings": {
            "settings": [
                {"platform": "telegram", "chat_id": "-200", "transcribe_audio": "on"}
            ]
        }
    })
    adapter = _make_adapter(require_mention=True, allowed_chats=["-200"])
    msg = _group_message("", chat_id=-200)
    msg.voice = object()
    msg.audio = None

    assert adapter._telegram_passive_audio_transcription_enabled(msg) is True


def test_audio_transcription_rule_matches_audio_attachments():
    adapter = _make_adapter(require_mention=True)
    adapter.config.extra["audio_transcription_rules"] = [
        {"chat_id": "-200", "message_types": ["audio"], "send_transcript": True}
    ]
    msg = _group_message("", chat_id=-200)
    msg.voice = None
    msg.audio = object()

    assert adapter._telegram_audio_transcription_rule_matches_message(msg) is True


def test_guest_mode_allows_only_direct_mentions_outside_allowed_chats():
    adapter = _make_adapter(
        require_mention=True,
        allowed_chats=["-200"],
        guest_mode=True,
        mention_patterns=[r"^\s*chompy\b"],
    )

    mentioned = _group_message(
        "hi @hermes_bot",
        chat_id=-201,
        entities=[_mention_entity("hi @hermes_bot")],
    )
    assert adapter._should_process_message(mentioned) is True
    assert adapter._should_process_message(_group_message("reply", chat_id=-201, reply_to_bot=True)) is False
    assert adapter._should_process_message(_group_message("chompy status", chat_id=-201)) is False
    assert adapter._should_process_message(_group_message("hello", chat_id=-201)) is False


def test_guest_mode_defaults_to_false_for_allowed_chat_bypass():
    adapter = _make_adapter(require_mention=True, allowed_chats=["-200"], guest_mode=False)

    mentioned = _group_message(
        "hi @hermes_bot",
        chat_id=-201,
        entities=[_mention_entity("hi @hermes_bot")],
    )
    assert adapter._should_process_message(mentioned) is False


def test_guest_mode_mention_dropped_in_ignored_thread():
    """A guest mention in an ignored thread is still dropped — thread gate runs first."""
    adapter = _make_adapter(
        require_mention=True,
        allowed_chats=["-200"],
        guest_mode=True,
        ignored_threads=[42],
    )
    mentioned = _group_message(
        "hi @hermes_bot",
        chat_id=-201,
        entities=[_mention_entity("hi @hermes_bot")],
        thread_id=42,
    )
    assert adapter._should_process_message(mentioned) is False


def test_ignored_threads_drop_group_messages_before_other_gates():
    adapter = _make_adapter(require_mention=False, free_response_chats=["-200"], ignored_threads=[31, "42"])

    assert adapter._should_process_message(_group_message("hello everyone", chat_id=-200, thread_id=31)) is False
    assert adapter._should_process_message(_group_message("hello everyone", chat_id=-200, thread_id=42)) is False
    assert adapter._should_process_message(_group_message("hello everyone", chat_id=-200, thread_id=99)) is True


def test_allowed_topics_drop_other_forum_topics_before_other_gates():
    adapter = _make_adapter(require_mention=False, allowed_chats=["-100"], allowed_topics=["8"])

    assert adapter._should_process_message(_group_message("hello", chat_id=-100, thread_id=8)) is True
    assert adapter._should_process_message(_group_message("hello", chat_id=-100, thread_id=11)) is False
    assert adapter._should_process_message(
        _group_message("hi @hermes_bot", chat_id=-100, thread_id=11, entities=[_mention_entity("hi @hermes_bot")])
    ) is False


def test_allowed_topics_do_not_filter_dms():
    adapter = _make_adapter(require_mention=False, allowed_topics=["8"])

    assert adapter._should_process_message(_dm_message("hello")) is True


def test_allowed_topics_treat_missing_thread_as_general_topic():
    adapter = _make_adapter(require_mention=False, allowed_topics=["1"])

    assert adapter._should_process_message(_group_message("hello", thread_id=None)) is True
    assert adapter._should_process_message(_group_message("hello", thread_id=8)) is False


def test_regex_mention_patterns_allow_custom_wake_words():
    adapter = _make_adapter(require_mention=True, mention_patterns=[r"^\s*chompy\b"])

    assert adapter._should_process_message(_group_message("chompy status")) is True
    assert adapter._should_process_message(_group_message("   chompy help")) is True
    assert adapter._should_process_message(_group_message("hey chompy")) is False


def test_text_wake_words_mirror_voice_trigger_vocabulary():
    adapter = _make_adapter(
        require_mention=True,
        voice_trigger_keywords=["трипио", "трипи", "tripio", "tripioo"],
        voice_trigger_aliases=["3p", "3 p си", "три пи о"],
    )

    assert adapter._should_process_message(_group_message("Трипио, что это?")) is True
    assert adapter._should_process_message(_group_message("@Tripioo что это?")) is True
    assert adapter._should_process_message(_group_message("tripioo check")) is True
    assert adapter._should_process_message(_group_message("3p си проверь")) is True
    assert adapter._should_process_message(_group_message("три-пи-о проверь")) is True
    assert adapter._should_process_message(_group_message("просто болтовня")) is False


def test_text_wake_words_preserve_custom_regex_patterns():
    adapter = _make_adapter(
        require_mention=True,
        mention_patterns=[r"^\s*chompy\b"],
        voice_trigger_keywords=["трипио"],
    )

    assert adapter._should_process_message(_group_message("chompy status")) is True
    assert adapter._should_process_message(_group_message("трипио статус")) is True
    assert adapter._should_process_message(_group_message("hey chompy")) is False


def test_invalid_regex_patterns_are_ignored():
    adapter = _make_adapter(require_mention=True, mention_patterns=[r"(", r"^\s*chompy\b"])

    assert adapter._should_process_message(_group_message("chompy status")) is True
    assert adapter._should_process_message(_group_message("hello everyone")) is False


def test_config_bridges_telegram_group_settings(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "telegram:\n"
        "  require_mention: true\n"
        "  guest_mode: true\n"
        "  exclusive_bot_mentions: true\n"
        "  observe_unmentioned_group_messages: true\n"
        "  mention_patterns:\n"
        "    - \"^\\\\s*chompy\\\\b\"\n"
        "  voice_trigger_keywords:\n"
        "    - \"трипио\"\n"
        "    - tripioo\n"
        "  voice_trigger_aliases:\n"
        "    - 3p\n"
        "  free_response_chats:\n"
        "    - \"-123\"\n"
        "  allowed_chats:\n"
        "    - \"-100\"\n"
        "  group_allowed_chats:\n"
        "    - \"-100\"\n"
        "  allowed_topics:\n"
        "    - 8\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("TELEGRAM_REQUIRE_MENTION", raising=False)
    monkeypatch.delenv("TELEGRAM_MENTION_PATTERNS", raising=False)
    monkeypatch.delenv("TELEGRAM_EXCLUSIVE_BOT_MENTIONS", raising=False)
    monkeypatch.delenv("TELEGRAM_GUEST_MODE", raising=False)
    monkeypatch.delenv("TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES", raising=False)
    monkeypatch.delenv("TELEGRAM_FREE_RESPONSE_CHATS", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS", raising=False)
    monkeypatch.delenv("TELEGRAM_GROUP_ALLOWED_CHATS", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_TOPICS", raising=False)

    config = load_gateway_config()

    assert config is not None
    assert __import__("os").environ["TELEGRAM_REQUIRE_MENTION"] == "true"
    assert __import__("os").environ["TELEGRAM_GUEST_MODE"] == "true"
    assert __import__("os").environ["TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES"] == "true"
    assert __import__("os").environ["TELEGRAM_EXCLUSIVE_BOT_MENTIONS"] == "true"
    assert json.loads(__import__("os").environ["TELEGRAM_MENTION_PATTERNS"]) == [r"^\s*chompy\b"]
    assert __import__("os").environ["TELEGRAM_FREE_RESPONSE_CHATS"] == "-123"
    assert __import__("os").environ["TELEGRAM_ALLOWED_CHATS"] == "-100"
    assert __import__("os").environ["TELEGRAM_GROUP_ALLOWED_CHATS"] == "-100"
    assert __import__("os").environ["TELEGRAM_ALLOWED_TOPICS"] == "8"
    tg_cfg = config.platforms.get(Platform.TELEGRAM)
    assert tg_cfg is not None
    assert tg_cfg.extra.get("guest_mode") is True
    assert tg_cfg.extra.get("allowed_chats") == ["-100"]
    assert tg_cfg.extra.get("group_allowed_chats") == ["-100"]
    assert tg_cfg.extra.get("allowed_topics") == [8]
    assert tg_cfg.extra.get("exclusive_bot_mentions") is True
    assert tg_cfg.extra.get("observe_unmentioned_group_messages") is True
    assert tg_cfg.extra.get("voice_trigger_keywords") == ["трипио", "tripioo"]
    assert tg_cfg.extra.get("voice_trigger_aliases") == ["3p"]


def test_config_bridges_telegram_extra_topic_response_rules(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "telegram:\n"
        "  extra:\n"
        "    topic_response_rules:\n"
        "      - chat_id: \"-100\"\n"
        "        thread_id: 7\n"
        "        require_mention: false\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config = load_gateway_config()

    assert config is not None
    tg_cfg = config.platforms.get(Platform.TELEGRAM)
    assert tg_cfg is not None
    assert tg_cfg.extra.get("topic_response_rules") == [
        {"chat_id": "-100", "thread_id": 7, "require_mention": False}
    ]


def test_config_bridges_telegram_user_allowlists(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "telegram:\n"
        "  allow_from:\n"
        "    - \"111\"\n"
        "    - \"222\"\n"
        "  group_allow_from:\n"
        "    - \"333\"\n"
        "  group_allowed_chats:\n"
        "    - \"-100\"\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("TELEGRAM_GROUP_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("TELEGRAM_GROUP_ALLOWED_CHATS", raising=False)

    config = load_gateway_config()

    assert config is not None
    assert __import__("os").environ["TELEGRAM_ALLOWED_USERS"] == "111,222"
    assert __import__("os").environ["TELEGRAM_GROUP_ALLOWED_USERS"] == "333"
    assert __import__("os").environ["TELEGRAM_GROUP_ALLOWED_CHATS"] == "-100"


def test_config_env_overrides_telegram_user_allowlists(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "telegram:\n"
        "  allow_from: \"111\"\n"
        "  group_allow_from: \"222\"\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "999")
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_USERS", "888")

    config = load_gateway_config()

    assert config is not None
    assert __import__("os").environ["TELEGRAM_ALLOWED_USERS"] == "999"
    assert __import__("os").environ["TELEGRAM_GROUP_ALLOWED_USERS"] == "888"


def test_dm_allow_from_is_enforced_by_gateway_authorization_not_trigger_gate():
    adapter = _make_adapter(allow_from=["111", "222"])

    assert adapter._should_process_message(_dm_message("hello", from_user_id=111)) is True
    assert adapter._should_process_message(_dm_message("hello", from_user_id=333)) is True


def test_group_allow_from_is_enforced_by_gateway_authorization_not_trigger_gate():
    adapter = _make_adapter(group_allow_from=["111"])

    assert adapter._should_process_message(_group_message("hello", from_user_id=333)) is True


def test_top_level_require_mention_bridges_to_telegram(monkeypatch, tmp_path):
    """require_mention at the config.yaml top level (alongside group_sessions_per_user)
    must behave identically to telegram.require_mention: true (#3979).
    """
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    # Intentionally no "telegram:" section — keys are at the top level.
    (hermes_home / "config.yaml").write_text(
        "require_mention: true\n"
        "group_sessions_per_user: true\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("TELEGRAM_REQUIRE_MENTION", raising=False)

    config = load_gateway_config()

    assert config is not None
    assert __import__("os").environ.get("TELEGRAM_REQUIRE_MENTION") == "true"

    # The adapter's extra dict must also carry the setting so that
    # _telegram_require_mention() works even without the env var.
    tg_cfg = config.platforms.get(__import__("gateway.config", fromlist=["Platform"]).Platform.TELEGRAM)
    if tg_cfg is not None:
        assert tg_cfg.extra.get("require_mention") is True


def test_top_level_require_mention_does_not_override_telegram_section(monkeypatch, tmp_path):
    """When telegram.require_mention is explicitly set, top-level require_mention
    must not override it (platform-specific config takes precedence).
    """
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "require_mention: true\n"
        "telegram:\n"
        "  require_mention: false\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("TELEGRAM_REQUIRE_MENTION", raising=False)

    config = load_gateway_config()

    assert config is not None
    # The telegram-specific "false" must win over the top-level "true".
    assert __import__("os").environ.get("TELEGRAM_REQUIRE_MENTION") == "false"


def test_config_bridges_telegram_ignored_threads(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "telegram:\n"
        "  ignored_threads:\n"
        "    - 31\n"
        "    - \"42\"\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("TELEGRAM_IGNORED_THREADS", raising=False)

    config = load_gateway_config()

    assert config is not None
    assert __import__("os").environ["TELEGRAM_IGNORED_THREADS"] == "31,42"


# ---------------------------------------------------------------------------
# Helpers for location / media observe+attribution tests
# ---------------------------------------------------------------------------

def _group_location_message(
    *,
    chat_id=-100,
    from_user_id=111,
    from_user_name="Alice Example",
    lat=37.7749,
    lon=-122.4194,
):
    return SimpleNamespace(
        message_id=50,
        text=None,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(id=chat_id, type="group", title="Test Group", is_forum=False),
        from_user=SimpleNamespace(
            id=from_user_id, full_name=from_user_name,
            first_name=from_user_name.split()[0],
        ),
        reply_to_message=None,
        date=None,
        location=SimpleNamespace(latitude=lat, longitude=lon),
        venue=None,
        sticker=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
    )


def _group_voice_message(
    *,
    chat_id=-100,
    from_user_id=111,
    from_user_name="Alice Example",
    caption=None,
):
    return SimpleNamespace(
        message_id=51,
        text=None,
        caption=caption,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(id=chat_id, type="group", title="Test Group", is_forum=False),
        from_user=SimpleNamespace(
            id=from_user_id, full_name=from_user_name,
            first_name=from_user_name.split()[0],
        ),
        reply_to_message=None,
        date=None,
        location=None,
        venue=None,
        sticker=None,
        photo=None,
        video=None,
        audio=None,
        voice=SimpleNamespace(
            get_file=AsyncMock(side_effect=Exception("simulated download failure"))
        ),
        document=None,
    )


# ---------------------------------------------------------------------------
# Observe + attribution parity: location messages
# ---------------------------------------------------------------------------

def test_unmentioned_location_message_observed_in_group():
    async def _run():
        adapter = _make_adapter(
            require_mention=True,
            allowed_chats=["-100"],
            group_allowed_chats=["-100"],
            observe_unmentioned_group_messages=True,
        )
        store = _FakeSessionStore()
        adapter._session_store = store
        update = SimpleNamespace(
            update_id=2001,
            message=_group_location_message(),
            effective_message=None,
        )

        await adapter._handle_location_message(update, SimpleNamespace())

        adapter._message_handler.assert_not_awaited()
        assert len(store.messages) == 1
        _, message, _ = store.messages[0]
        assert message["observed"] is True
        assert store.sources[0].user_id is None

    asyncio.run(_run())


def test_triggered_location_message_uses_shared_session_in_observe_mode():
    async def _run():
        adapter = _make_adapter(
            require_mention=False,
            group_allowed_chats=["-100"],
            observe_unmentioned_group_messages=True,
        )
        adapter.handle_message = AsyncMock()
        update = SimpleNamespace(
            update_id=2002,
            message=_group_location_message(),
            effective_message=None,
        )

        await adapter._handle_location_message(update, SimpleNamespace())

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.source.user_id is None
        assert "[Alice Example|111]" in event.text

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Observe + attribution parity: media messages (voice as representative)
# ---------------------------------------------------------------------------

def test_unmentioned_voice_message_observed_in_group():
    async def _run():
        adapter = _make_adapter(
            require_mention=True,
            allowed_chats=["-100"],
            group_allowed_chats=["-100"],
            observe_unmentioned_group_messages=True,
        )
        store = _FakeSessionStore()
        adapter._session_store = store
        update = SimpleNamespace(
            update_id=3001,
            message=_group_voice_message(),
            effective_message=None,
        )

        await adapter._handle_media_message(update, SimpleNamespace())

        adapter._message_handler.assert_not_awaited()
        assert len(store.messages) == 1
        _, message, _ = store.messages[0]
        assert message["observed"] is True
        assert store.sources[0].user_id is None

    asyncio.run(_run())


def test_triggered_voice_message_uses_shared_session_in_observe_mode():
    async def _run():
        adapter = _make_adapter(
            require_mention=False,
            group_allowed_chats=["-100"],
            observe_unmentioned_group_messages=True,
        )
        adapter.handle_message = AsyncMock()
        update = SimpleNamespace(
            update_id=3002,
            message=_group_voice_message(caption="check this audio"),
            effective_message=None,
        )

        await adapter._handle_media_message(update, SimpleNamespace())

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.source.user_id is None
        assert "[Alice Example|111]" in event.text

    asyncio.run(_run())


def test_show_transcription_marks_voice_as_gateway_transcription_even_when_reply_triggers_agent(monkeypatch):
    async def _run():
        import hermes_cli.config as hermes_config

        monkeypatch.setattr(hermes_config, "load_config", lambda: {
            "chat_settings": {
                "settings": [
                    {
                        "platform": "telegram",
                        "chat_id": "-100",
                        "audio_trigger": "on",
                        "show_transcription": "on",
                    }
                ]
            }
        })
        adapter = _make_adapter(
            require_mention=True,
            allowed_chats=["-100"],
            group_allowed_chats=["-100"],
            voice_trigger_keywords=["tripioo"],
        )
        adapter.handle_message = AsyncMock()
        file_obj = SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"ogg voice")),
        )
        msg = _group_voice_message()
        msg.reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(id=999),
            message_id=10,
            text="previous bot reply",
            caption=None,
        )
        msg.voice = SimpleNamespace(get_file=AsyncMock(return_value=file_obj))
        update = SimpleNamespace(update_id=3003, message=msg, effective_message=None)

        with patch(
            "gateway.platforms.telegram.cache_audio_from_bytes",
            return_value="/tmp/voice.ogg",
        ):
            await adapter._handle_media_message(update, SimpleNamespace())

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.telegram_passive_audio_transcription is True
        assert not hasattr(event, "telegram_audio_force_agent_response")
        assert event.media_urls == ["/tmp/voice.ogg"]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Observed-media caching (unmentioned group attachments)
# ---------------------------------------------------------------------------

def _group_photo_message(*, chat_id=-100, caption="Veja esta foto", file_size=1024):
    file_obj = SimpleNamespace(
        file_path="photos/observed.png",
        download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\n observed")),
    )
    photo = SimpleNamespace(file_size=file_size, get_file=AsyncMock(return_value=file_obj))
    return SimpleNamespace(
        message_id=52, text=None, caption=caption, entities=[], caption_entities=[],
        message_thread_id=None, is_topic_message=False,
        chat=SimpleNamespace(id=chat_id, type="group", title="Test Group", is_forum=False),
        from_user=SimpleNamespace(id=111, full_name="Alice Example", first_name="Alice"),
        reply_to_message=None, date=None, location=None, venue=None,
        sticker=None, photo=[photo], video=None, audio=None, voice=None, document=None,
    )


def _group_document_message(*, chat_id=-100, caption="Este arquivo", document=None):
    file_obj = SimpleNamespace(
        file_path="documents/report.pdf",
        download_as_bytearray=AsyncMock(return_value=bytearray(b"%PDF observed bytes")),
    )
    document = document or SimpleNamespace(
        file_name="RESULTADO BIOLOGICO - PROTOCOLO 103- URBAN.pdf",
        mime_type="application/pdf", file_size=1024,
        get_file=AsyncMock(return_value=file_obj),
    )
    return SimpleNamespace(
        message_id=53, text=None, caption=caption, entities=[], caption_entities=[],
        message_thread_id=None, is_topic_message=False,
        chat=SimpleNamespace(id=chat_id, type="group", title="Test Group", is_forum=False),
        from_user=SimpleNamespace(id=111, full_name="Alice Example", first_name="Alice"),
        reply_to_message=None, date=None, location=None, venue=None,
        sticker=None, photo=None, video=None, audio=None, voice=None, document=document,
    )


def test_unmentioned_photo_observed_with_cached_path(monkeypatch, tmp_path):
    async def _run():
        adapter = _make_adapter(
            require_mention=True, allowed_chats=["-100"],
            group_allowed_chats=["-100"], observe_unmentioned_group_messages=True,
        )
        store = _FakeSessionStore()
        adapter._session_store = store
        cached_path = tmp_path / "img_abc_observed.png"
        monkeypatch.setattr(
            "gateway.platforms.telegram.cache_image_from_bytes",
            lambda _data, ext=".jpg": str(cached_path),
        )
        update = SimpleNamespace(update_id=3003, message=_group_photo_message(), effective_message=None)

        await adapter._handle_media_message(update, SimpleNamespace())

        adapter._message_handler.assert_not_awaited()
        assert len(store.messages) == 1
        _, message, _ = store.messages[0]
        assert message["observed"] is True
        assert "Veja esta foto" in message["content"]
        assert "image" in message["content"]
        assert str(cached_path) in message["content"]
        assert store.sources[0].user_id is None

    asyncio.run(_run())


def test_unmentioned_document_observed_with_cached_path(monkeypatch, tmp_path):
    async def _run():
        adapter = _make_adapter(
            require_mention=True, allowed_chats=["-100"],
            group_allowed_chats=["-100"], observe_unmentioned_group_messages=True,
        )
        store = _FakeSessionStore()
        adapter._session_store = store
        cached_path = tmp_path / "doc_abc_report.pdf"
        monkeypatch.setattr(
            "gateway.platforms.telegram.cache_document_from_bytes",
            lambda _data, _filename: str(cached_path),
        )
        update = SimpleNamespace(update_id=3004, message=_group_document_message(), effective_message=None)

        await adapter._handle_media_message(update, SimpleNamespace())

        adapter._message_handler.assert_not_awaited()
        assert len(store.messages) == 1
        _, message, _ = store.messages[0]
        assert message["observed"] is True
        assert "Este arquivo" in message["content"]
        assert str(cached_path) in message["content"]

    asyncio.run(_run())


def test_unmentioned_large_document_observed_without_download(monkeypatch):
    async def _run():
        adapter = _make_adapter(
            require_mention=True, allowed_chats=["-100"],
            group_allowed_chats=["-100"], observe_unmentioned_group_messages=True,
        )
        adapter._max_doc_bytes = 100
        store = _FakeSessionStore()
        adapter._session_store = store
        cache_doc = Mock(return_value="/tmp/huge.pdf")
        monkeypatch.setattr("gateway.platforms.telegram.cache_document_from_bytes", cache_doc)
        document = SimpleNamespace(
            file_name="huge.pdf", mime_type="application/pdf",
            file_size=101, get_file=AsyncMock(),
        )
        update = SimpleNamespace(
            update_id=3005, message=_group_document_message(document=document), effective_message=None,
        )

        await adapter._handle_media_message(update, SimpleNamespace())

        cache_doc.assert_not_called()
        document.get_file.assert_not_called()
        _, message, _ = store.messages[0]
        assert "too large" in message["content"]
        assert "/tmp/huge.pdf" not in message["content"]

    asyncio.run(_run())


def test_unmentioned_unsupported_document_observed_without_caching(monkeypatch):
    async def _run():
        adapter = _make_adapter(
            require_mention=True, allowed_chats=["-100"],
            group_allowed_chats=["-100"], observe_unmentioned_group_messages=True,
        )
        store = _FakeSessionStore()
        adapter._session_store = store
        cache_doc = Mock(return_value="/tmp/malware.exe")
        monkeypatch.setattr("gateway.platforms.telegram.cache_document_from_bytes", cache_doc)
        file_obj = SimpleNamespace(
            file_path="documents/malware.exe",
            download_as_bytearray=AsyncMock(return_value=bytearray(b"MZ")),
        )
        document = SimpleNamespace(
            file_name="malware.exe", mime_type="application/x-msdownload",
            file_size=2, get_file=AsyncMock(return_value=file_obj),
        )
        update = SimpleNamespace(
            update_id=3006, message=_group_document_message(document=document), effective_message=None,
        )

        await adapter._handle_media_message(update, SimpleNamespace())

        cache_doc.assert_not_called()
        _, message, _ = store.messages[0]
        assert "unsupported" in message["content"].lower()

    asyncio.run(_run())


def test_unmentioned_voice_with_matching_transcription_rule_dispatches_for_runner_gating():
    async def _run():
        adapter = _make_adapter(
            require_mention=True,
            allowed_chats=["-100"],
            group_allowed_chats=["-100"],
            observe_unmentioned_group_messages=False,
        )
        adapter.config.extra["audio_transcription_rules"] = [
            {
                "chat_id": -100,
                "enabled": True,
                "send_transcript": True,
                "trigger_keywords": ["напомни"],
                "on_keyword_match": "run_ai",
                "on_no_match": "transcript_only",
            }
        ]
        adapter.handle_message = AsyncMock()
        update = SimpleNamespace(
            update_id=3003,
            message=_group_voice_message(caption=None),
            effective_message=None,
        )

        await adapter._handle_media_message(update, SimpleNamespace())

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.message_type == MessageType.VOICE
        assert event.source.chat_id == "-100"

    asyncio.run(_run())


def test_boxmap_miniapp_scenario_request_sends_button_without_agent():
    async def _run():
        adapter = _make_adapter(require_mention=False)
        adapter._command_surface_profile_for_message = Mock(return_value="boxmap")
        adapter._ensure_forum_commands = AsyncMock()
        adapter._enqueue_text_event = Mock()
        adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7001))
        update = SimpleNamespace(
            update_id=4001,
            message=_group_message("TripiooBot покажи миниап сценарии", thread_id=35),
            effective_message=None,
        )

        await adapter._handle_text_message(update, SimpleNamespace())

        adapter._enqueue_text_event.assert_not_called()
        adapter._bot.send_message.assert_awaited_once()
        kwargs = adapter._bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == -100
        assert kwargs["message_thread_id"] == 35
        assert kwargs["reply_markup"] is not None
        assert "BoxMap" in kwargs["text"]

    asyncio.run(_run())


def test_boxmap_link_complaint_reply_sends_scenario_button_without_agent():
    async def _run():
        class _Button:
            def __init__(self, text, **kwargs):
                self.text = text
                self.kwargs = kwargs

        class _Markup:
            def __init__(self, rows):
                self.inline_keyboard = rows

        adapter = _make_adapter(require_mention=False)
        adapter._command_surface_profile_for_message = Mock(return_value="boxmap")
        adapter._ensure_forum_commands = AsyncMock()
        adapter._enqueue_text_event = Mock()
        adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7002))
        message = _group_message("TripiooBot где ссылка? нужна кнопка", thread_id=35, reply_to_bot=True)
        message.reply_to_message.text = "Открой BoxMap-сценарии в миниаппе."
        update = SimpleNamespace(
            update_id=4003,
            message=message,
            effective_message=None,
        )

        with (
            patch("gateway.platforms.telegram.InlineKeyboardButton", _Button),
            patch("gateway.platforms.telegram.InlineKeyboardMarkup", _Markup),
        ):
            await adapter._handle_text_message(update, SimpleNamespace())

        adapter._enqueue_text_event.assert_not_called()
        adapter._bot.send_message.assert_awaited_once()
        kwargs = adapter._bot.send_message.await_args.kwargs
        assert kwargs["message_thread_id"] == 35
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        assert button.text == "Открыть сценарии"
        assert button.kwargs.get("url") == "https://t.me/hermes_bot?startapp=boxmap"

    asyncio.run(_run())


def test_non_boxmap_miniapp_scenario_request_still_reaches_agent():
    async def _run():
        adapter = _make_adapter(require_mention=False)
        adapter._command_surface_profile_for_message = Mock(return_value="default")
        adapter._ensure_forum_commands = AsyncMock()
        adapter._enqueue_text_event = Mock()
        adapter._bot.send_message = AsyncMock()
        update = SimpleNamespace(
            update_id=4002,
            message=_group_message("TripiooBot покажи миниапп сценарии", thread_id=35),
            effective_message=None,
        )

        await adapter._handle_text_message(update, SimpleNamespace())

        adapter._bot.send_message.assert_not_called()
        adapter._enqueue_text_event.assert_called_once()

    asyncio.run(_run())
