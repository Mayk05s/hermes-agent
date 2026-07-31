"""Tests for Telegram topic/thread routing fallbacks.

Supergroup forum topics route with ``message_thread_id``. Hermes-created
private DM topic lanes are different: live Telegram testing showed they only
stay in the expected lane when sends include both the private topic
``message_thread_id`` and a ``reply_to_message_id`` anchor to the triggering
user message. If either anchor is unavailable or rejected, the adapter must
avoid retrying with a partial topic route that can render outside the lane.
"""

import json
import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig, Platform
from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    SendResult,
    _reply_anchor_for_event,
    _thread_metadata_for_source,
)
from gateway.session import build_session_key


# ── Fake telegram.error hierarchy ──────────────────────────────────────
# Mirrors the real python-telegram-bot hierarchy:
#   BadRequest → NetworkError → TelegramError → Exception


class FakeNetworkError(Exception):
    pass


class FakeBadRequest(FakeNetworkError):
    pass


class FakeTimedOut(FakeNetworkError):
    pass


class FakeRetryAfter(Exception):
    def __init__(self, seconds):
        super().__init__(f"Retry after {seconds}")
        self.retry_after = seconds


# Build a fake telegram module tree so the adapter's internal imports work
class _FakeInlineKeyboardButton:
    def __init__(self, text, callback_data=None, **kwargs):
        self.text = text
        self.callback_data = callback_data
        self.kwargs = kwargs


class _FakeInlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


class _FakeWebAppInfo:
    def __init__(self, url):
        self.url = url


class _FakeInputMediaPhoto:
    def __init__(self, media, caption=None, **kwargs):
        self.media = media
        self.caption = caption
        self.kwargs = kwargs


_fake_telegram = types.ModuleType("telegram")
_fake_telegram.Update = object
_fake_telegram.Bot = object
_fake_telegram.Message = object
_fake_telegram.InlineKeyboardButton = _FakeInlineKeyboardButton
_fake_telegram.InlineKeyboardMarkup = _FakeInlineKeyboardMarkup
_fake_telegram.WebAppInfo = _FakeWebAppInfo
_fake_telegram.InputMediaPhoto = _FakeInputMediaPhoto
_fake_telegram_error = types.ModuleType("telegram.error")
_fake_telegram_error.NetworkError = FakeNetworkError
_fake_telegram_error.BadRequest = FakeBadRequest
_fake_telegram_error.TimedOut = FakeTimedOut
_fake_telegram.error = _fake_telegram_error
_fake_telegram_constants = types.ModuleType("telegram.constants")
_fake_telegram_constants.ParseMode = SimpleNamespace(
    MARKDOWN_V2="MarkdownV2",
    MARKDOWN="Markdown",
    HTML="HTML",
)
_fake_telegram_constants.ChatType = SimpleNamespace(
    GROUP="group",
    SUPERGROUP="supergroup",
    CHANNEL="channel",
    PRIVATE="private",
)
_fake_telegram.constants = _fake_telegram_constants
_fake_telegram_ext = types.ModuleType("telegram.ext")
_fake_telegram_ext.Application = object
_fake_telegram_ext.CommandHandler = object
_fake_telegram_ext.CallbackQueryHandler = object
_fake_telegram_ext.MessageHandler = object
_fake_telegram_ext.TypeHandler = object
_fake_telegram_ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
_fake_telegram_ext.filters = object
_fake_telegram_request = types.ModuleType("telegram.request")
_fake_telegram_request.HTTPXRequest = object


@pytest.fixture(autouse=True)
def _inject_fake_telegram(monkeypatch):
    """Inject fake telegram modules so the adapter can import from them."""
    monkeypatch.setitem(sys.modules, "telegram", _fake_telegram)
    monkeypatch.setitem(sys.modules, "telegram.error", _fake_telegram_error)
    monkeypatch.setitem(sys.modules, "telegram.constants", _fake_telegram_constants)
    monkeypatch.setitem(sys.modules, "telegram.ext", _fake_telegram_ext)
    monkeypatch.setitem(sys.modules, "telegram.request", _fake_telegram_request)


def _make_adapter():
    from gateway.platforms.telegram import TelegramAdapter

    config = PlatformConfig(enabled=True, token="fake-token")
    adapter = object.__new__(TelegramAdapter)
    adapter.config = config
    adapter._config = config
    adapter._platform = Platform.TELEGRAM
    adapter._connected = True
    adapter._dm_topics = {}
    adapter._dm_topics_config = []
    adapter._reply_to_mode = "first"
    adapter._fallback_ips = []
    adapter._polling_conflict_count = 0
    adapter._polling_network_error_count = 0
    adapter._polling_error_callback_ref = None
    adapter.platform = Platform.TELEGRAM
    return adapter


def test_non_forum_group_reply_thread_id_does_not_fork_session_key():
    """Reply-derived thread ids in ordinary groups must not create topic lanes."""
    from gateway.platforms import telegram as telegram_mod

    adapter = _make_adapter()
    message = SimpleNamespace(
        text="Done",
        caption=None,
        chat=SimpleNamespace(
            id=-100123,
            type=telegram_mod.ChatType.SUPERGROUP,
            is_forum=False,
            title="Regular group",
        ),
        from_user=SimpleNamespace(id=456, full_name="Alice"),
        message_thread_id=461,
        is_topic_message=False,
        reply_to_message=SimpleNamespace(
            message_id=460,
            text="Please complete the CAPTCHA/login, then reply done.",
            caption=None,
        ),
        message_id=462,
        date=None,
    )

    event = adapter._build_message_event(message, msg_type=MessageType.TEXT)

    assert event.source.chat_id == "-100123"
    assert event.source.chat_type == "group"
    assert event.source.thread_id is None
    assert build_session_key(event.source) == "agent:main:telegram:group:-100123:456"


def test_forum_group_topic_message_preserves_thread_session_key():
    """Real Telegram forum-topic messages should still route by topic id."""
    from gateway.platforms import telegram as telegram_mod

    adapter = _make_adapter()
    message = SimpleNamespace(
        text="hello from topic",
        caption=None,
        chat=SimpleNamespace(
            id=-100123,
            type=telegram_mod.ChatType.SUPERGROUP,
            is_forum=True,
            title="Forum group",
        ),
        from_user=SimpleNamespace(id=456, full_name="Alice"),
        message_thread_id=17585,
        is_topic_message=True,
        reply_to_message=None,
        message_id=10,
        date=None,
    )

    event = adapter._build_message_event(message, msg_type=MessageType.TEXT)

    assert event.source.chat_id == "-100123"
    assert event.source.chat_type == "group"
    assert event.source.thread_id == "17585"
    assert build_session_key(event.source) == "agent:main:telegram:group:-100123:17585"


def test_forum_general_topic_without_message_thread_id_keeps_thread_context():
    """Forum General-topic messages should keep synthetic thread context."""
    from gateway.platforms import telegram as telegram_mod

    adapter = _make_adapter()
    message = SimpleNamespace(
        text="hello from General",
        caption=None,
        chat=SimpleNamespace(
            id=-100123,
            type=telegram_mod.ChatType.SUPERGROUP,
            is_forum=True,
            title="Forum group",
        ),
        from_user=SimpleNamespace(id=456, full_name="Alice"),
        message_thread_id=None,
        reply_to_message=None,
        message_id=10,
        date=None,
    )

    event = adapter._build_message_event(message, msg_type=SimpleNamespace(value="text"))

    assert event.source.chat_id == "-100123"
    assert event.source.chat_type == "group"
    assert event.source.thread_id == "1"


@pytest.mark.asyncio
async def test_send_omits_general_topic_thread_id():
    """Telegram sends to forum General should omit message_thread_id=1."""
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=42)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="-100123",
        content="test message",
        metadata={"thread_id": "1"},
    )

    assert result.success is True
    assert len(call_log) == 1
    assert call_log[0]["chat_id"] == -100123
    assert call_log[0]["text"] == "test message"
    assert call_log[0]["reply_to_message_id"] is None
    assert call_log[0]["message_thread_id"] is None


@pytest.mark.asyncio
async def test_send_typing_preserves_general_topic_thread_id():
    """Typing for forum General must send message_thread_id=1, not None.

    Asymmetric with _message_thread_id_for_send: sendMessage rejects
    message_thread_id=1, but sendChatAction needs it to scope the typing
    bubble to the General topic. Omitting it (message_thread_id=None) hides
    the bubble from the General-topic view entirely.

    Regression guard for the d5357f816 refactor that mapped "1" → None in
    the typing resolver and silently killed typing indicators in every
    forum-group General topic.
    """
    adapter = _make_adapter()
    call_log = []

    async def mock_send_chat_action(**kwargs):
        call_log.append(dict(kwargs))

    adapter._bot = SimpleNamespace(send_chat_action=mock_send_chat_action)

    await adapter.send_typing("-100123", metadata={"thread_id": "1"})

    assert call_log == [
        {"chat_id": -100123, "action": "typing", "message_thread_id": 1},
    ]


@pytest.mark.asyncio
async def test_send_typing_does_not_fall_back_to_root_for_dm_topic():
    """Typing failures in DM topics should not show an indicator in All Messages."""
    adapter = _make_adapter()
    call_log = []

    async def mock_send_chat_action(**kwargs):
        call_log.append(dict(kwargs))
        raise FakeBadRequest("Message thread not found")

    adapter._bot = SimpleNamespace(send_chat_action=mock_send_chat_action)

    await adapter.send_typing("12345", metadata={"thread_id": "22182"})

    assert call_log == [
        {"chat_id": 12345, "action": "typing", "message_thread_id": 22182},
    ]


@pytest.mark.asyncio
async def test_send_typing_attempts_api_call_for_dm_topic_reply_fallback():
    """Hermes-created DM topic lanes should still attempt scoped typing.

    Some private DM topic lanes route message sends through reply-anchor
    fallback, but live Telegram testing shows sendChatAction accepts the lane's
    message_thread_id. If Telegram rejects a stale or invalid thread later,
    send_typing now falls back to sending typing without thread_id so the
    indicator at least appears in the main DM view.
    """
    adapter = _make_adapter()
    call_log = []

    async def mock_send_chat_action(**kwargs):
        call_log.append(dict(kwargs))

    adapter._bot = SimpleNamespace(send_chat_action=mock_send_chat_action)

    await adapter.send_typing(
        "12345",
        metadata={
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "telegram_reply_to_message_id": "462",
        },
    )

    assert call_log == [
        {"chat_id": 12345, "action": "typing", "message_thread_id": 20197},
    ]


@pytest.mark.asyncio
async def test_send_typing_falls_back_without_thread_on_bad_request():
    """When DM topic typing with message_thread_id fails, retry without it."""
    adapter = _make_adapter()

    call_log = []
    call_count = [0]

    async def mock_send_chat_action(**kwargs):
        call_log.append(dict(kwargs))
        call_count[0] += 1
        if call_count[0] == 1 and kwargs.get("message_thread_id") is not None:
            raise FakeBadRequest("Message thread not found")

    adapter._bot = SimpleNamespace(send_chat_action=mock_send_chat_action)

    await adapter.send_typing(
        "12345",
        metadata={
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "telegram_reply_to_message_id": "462",
        },
    )

    # First call: with message_thread_id (failed)
    # Second call: fallback without message_thread_id (succeeded)
    assert len(call_log) == 2
    assert call_log[0] == {
        "chat_id": 12345,
        "action": "typing",
        "message_thread_id": 20197,
    }
    assert call_log[1] == {
        "chat_id": 12345,
        "action": "typing",
    }


@pytest.mark.asyncio
async def test_group_send_fails_closed_when_thread_not_found():
    """A stale forum topic must never leak the message into General."""
    adapter = _make_adapter()

    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        tid = kwargs.get("message_thread_id")
        raise FakeBadRequest("Message thread not found")

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="-100123",
        content="test message",
        metadata={"thread_id": "99999"},
    )

    assert result.success is False
    assert result.retryable is False
    # A same-topic retry is safe; no call may omit the topic.
    assert len(call_log) == 2
    assert call_log[0]["message_thread_id"] == 99999
    assert call_log[1]["message_thread_id"] == 99999


@pytest.mark.asyncio
async def test_send_retries_transient_thread_not_found_before_fallback():
    """A one-off Telegram thread-not-found response should still land in the topic."""
    adapter = _make_adapter()

    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        if len(call_log) == 1:
            raise FakeBadRequest("Message thread not found")
        return SimpleNamespace(message_id=43)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="-100123",
        content="test message",
        metadata={"thread_id": "99999"},
    )

    assert result.success is True
    assert result.message_id == "43"
    assert result.raw_response["requested_thread_id"] == 99999
    assert result.raw_response["thread_fallback"] is False
    assert len(call_log) == 2
    assert call_log[0]["message_thread_id"] == 99999
    assert call_log[1]["message_thread_id"] == 99999


@pytest.mark.asyncio
async def test_send_private_dm_topic_uses_direct_messages_topic_id():
    """Private Telegram topics route sends via direct_messages_topic_id."""
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=42)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="123",
        content="test message",
        metadata={"thread_id": "99999", "direct_messages_topic_id": "99999"},
    )

    assert result.success is True
    assert call_log[0]["message_thread_id"] is None
    assert call_log[0]["direct_messages_topic_id"] == 99999


def test_base_gateway_metadata_marks_telegram_dm_topics_as_reply_fallback():
    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_type="dm",
        thread_id="20189",
    )

    metadata = _thread_metadata_for_source(source, "462")

    assert metadata == {
        "thread_id": "20189",
        "telegram_dm_topic_reply_fallback": True,
        "direct_messages_topic_id": "20189",
        "telegram_reply_to_message_id": "462",
    }


def test_base_gateway_metadata_for_resumed_telegram_dm_topic_uses_direct_topic():
    """Resumed/synthetic DM-topic events may have no reply anchor."""
    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_type="dm",
        thread_id="20189",
    )

    metadata = _thread_metadata_for_source(source)

    assert metadata == {
        "thread_id": "20189",
        "telegram_dm_topic_reply_fallback": True,
        "direct_messages_topic_id": "20189",
    }


def test_base_gateway_replies_to_triggering_message_for_telegram_dm_topic():
    """Private DM topic lanes should anchor replies to the active user message."""
    event = SimpleNamespace(
        message_id="463",
        reply_to_message_id="462",
        source=SimpleNamespace(
            platform=Platform.TELEGRAM,
            chat_type="dm",
            thread_id="20189",
        ),
    )

    assert _reply_anchor_for_event(event) == "463"


@pytest.mark.asyncio
async def test_gateway_runner_busy_ack_replies_to_triggering_message_for_telegram_dm_topic(monkeypatch, tmp_path):
    """GatewayRunner's duplicate thread metadata must match the base helper."""
    from gateway import run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    GatewayRunner = gateway_run.GatewayRunner

    class BusyAdapter:
        def __init__(self):
            self._pending_messages = {}
            self.calls = []

        async def _send_with_retry(self, **kwargs):
            self.calls.append(kwargs)
            return SendResult(success=True, message_id="ack-1")

    class BusyAgent:
        def interrupt(self, _text):
            return None

        def get_activity_summary(self):
            return {}

    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        thread_id="20197",
        user_id="user-1",
    )
    event = MessageEvent(
        text="busy follow-up",
        message_type=MessageType.TEXT,
        source=source,
        message_id="463",
        reply_to_message_id="462",
    )
    session_key = build_session_key(source)
    adapter = BusyAdapter()

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._running_agents = {session_key: BusyAgent()}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner._busy_input_mode = "interrupt"
    runner._is_user_authorized = lambda _source: True

    assert await runner._handle_active_session_busy_message(event, session_key) is True

    assert adapter.calls
    assert adapter.calls[0]["reply_to"] == "463"
    assert adapter.calls[0]["metadata"] == {
        "thread_id": "20197",
        "telegram_dm_topic_reply_fallback": True,
        "direct_messages_topic_id": "20197",
        "telegram_reply_to_message_id": "463",
    }


@pytest.mark.asyncio
async def test_send_uses_reply_fallback_for_hermes_dm_topics():
    """Hermes-created Telegram DM topics route with thread id plus reply anchor."""
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(kwargs)
        return SimpleNamespace(message_id=777)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="123",
        content="test message",
        reply_to="462",
        metadata={
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
        },
    )

    assert result.success is True
    assert call_log[0]["reply_to_message_id"] == 462
    assert call_log[0]["message_thread_id"] == 20197
    assert "direct_messages_topic_id" not in call_log[0]


@pytest.mark.asyncio
async def test_send_uses_reply_anchor_when_direct_topic_fallback_metadata_exists():
    """Restart/update replay metadata keeps the anchor authoritative when present."""
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(kwargs)
        return SimpleNamespace(message_id=777)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="123",
        content="test message",
        metadata={
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "direct_messages_topic_id": "20197",
            "telegram_reply_to_message_id": "462",
        },
    )

    assert result.success is True
    assert call_log[0]["reply_to_message_id"] == 462
    assert call_log[0]["message_thread_id"] == 20197
    assert "direct_messages_topic_id" not in call_log[0]


@pytest.mark.asyncio
async def test_send_created_private_topic_uses_message_thread_without_anchor():
    """Topics created via createForumTopic are addressable by message_thread_id directly."""
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(kwargs)
        return SimpleNamespace(message_id=781)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="123",
        content="created topic message",
        metadata={
            "thread_id": "38049",
            "telegram_dm_topic_created_for_send": True,
        },
    )

    assert result.success is True
    assert call_log[0]["reply_to_message_id"] is None
    assert call_log[0]["message_thread_id"] == 38049
    assert "direct_messages_topic_id" not in call_log[0]


@pytest.mark.asyncio
async def test_created_private_topic_thread_not_found_fails_without_root_fallback():
    """Created private-topic sends must not retry into All Messages on stale thread IDs."""
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        raise FakeBadRequest("Message thread not found")

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="123",
        content="created topic message",
        metadata={
            "thread_id": "32343",
            "telegram_dm_topic_created_for_send": True,
        },
    )

    assert result.success is False
    assert "thread not found" in str(result.error).lower()
    assert len(call_log) == 1
    assert call_log[0]["message_thread_id"] == 32343


@pytest.mark.asyncio
async def test_send_uses_metadata_reply_fallback_for_streaming_dm_topics():
    """Metadata-only sends still stay in Hermes-created Telegram DM topics."""
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(kwargs)
        return SimpleNamespace(message_id=778)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="123",
        content="streamed text",
        metadata={
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "telegram_reply_to_message_id": "462",
        },
    )

    assert result.success is True
    assert call_log[0]["reply_to_message_id"] == 462
    assert call_log[0]["message_thread_id"] == 20197
    assert "direct_messages_topic_id" not in call_log[0]


@pytest.mark.asyncio
async def test_send_reply_fallback_applies_to_every_chunk_for_dm_topics():
    """Long Telegram DM-topic fallback sends must anchor every chunk."""
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=len(call_log))

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="123",
        content="A" * 5000,
        metadata={
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "telegram_reply_to_message_id": "462",
        },
    )

    assert result.success is True
    assert len(call_log) > 1
    assert all(call["reply_to_message_id"] == 462 for call in call_log)
    assert all(call["message_thread_id"] == 20197 for call in call_log)
    assert all("direct_messages_topic_id" not in call for call in call_log)


@pytest.mark.asyncio
async def test_send_model_picker_uses_metadata_reply_fallback_for_dm_topics():
    """Inline keyboard sends also consume the metadata reply fallback."""
    adapter = _make_adapter()
    adapter._model_picker_state = {}
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(kwargs)
        return SimpleNamespace(message_id=779)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send_model_picker(
        chat_id="123",
        providers=[{"name": "OpenAI", "slug": "openai", "models": [], "total_models": 0}],
        current_model="gpt-test",
        current_provider="openai",
        session_key="telegram:123:20197",
        on_model_selected=lambda *_: None,
        metadata={
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "telegram_reply_to_message_id": "462",
        },
    )

    assert result.success is True
    assert call_log[0]["reply_to_message_id"] == 462
    assert call_log[0]["message_thread_id"] == 20197
    assert "direct_messages_topic_id" not in call_log[0]


@pytest.mark.asyncio
async def test_send_dm_topic_fallback_without_anchor_does_not_crash():
    """DM-topic fallback without an anchor uses direct topic routing."""
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=780)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="123",
        content="source-only send",
        metadata={
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "direct_messages_topic_id": "20197",
        },
    )

    assert result.success is True
    assert call_log[0]["reply_to_message_id"] is None
    assert call_log[0]["message_thread_id"] is None
    assert call_log[0]["direct_messages_topic_id"] == 20197


@pytest.mark.asyncio
async def test_send_dm_topic_reply_not_found_fails_closed():
    """If Telegram deletes the reply anchor, private-topic sends must not fall back elsewhere."""
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        raise FakeBadRequest("Message to be replied not found")

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="123",
        content="anchor disappeared",
        metadata={
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "telegram_reply_to_message_id": "462",
        },
    )

    assert result.success is False
    assert result.retryable is False
    assert call_log[0]["reply_to_message_id"] == 462
    assert call_log[0]["message_thread_id"] == 20197
    assert len(call_log) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "bot_method_name", "path_kw", "filename", "payload"),
    [
        ("send_image_file", "send_photo", "image_path", "photo.png", b"png-data"),
        ("send_document", "send_document", "file_path", "report.txt", b"report-data"),
        ("send_video", "send_video", "video_path", "clip.mp4", b"video-data"),
        ("send_voice", "send_voice", "audio_path", "clip.ogg", b"ogg-data"),
        ("send_voice", "send_audio", "audio_path", "clip.mp3", b"mp3-data"),
    ],
)
async def test_native_media_dm_topic_reply_not_found_retry_drops_thread_id(
    tmp_path,
    method_name,
    bot_method_name,
    path_kw,
    filename,
    payload,
):
    adapter = _make_adapter()
    media_path = tmp_path / filename
    media_path.write_bytes(payload)
    call_log = []

    async def mock_send_media(**kwargs):
        call_log.append(dict(kwargs))
        if len(call_log) == 1:
            raise FakeBadRequest("Message to be replied not found")
        return SimpleNamespace(message_id=782)

    adapter._bot = SimpleNamespace(**{bot_method_name: mock_send_media})

    result = await getattr(adapter, method_name)(
        chat_id="123",
        **{path_kw: str(media_path)},
        metadata={
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "telegram_reply_to_message_id": "462",
        },
    )

    assert result.success is True
    assert call_log[0]["reply_to_message_id"] == 462
    assert call_log[0]["message_thread_id"] == 20197
    assert call_log[1]["reply_to_message_id"] is None
    assert "message_thread_id" not in call_log[1]
    assert "direct_messages_topic_id" not in call_log[1]


@pytest.mark.asyncio
async def test_animation_dm_topic_reply_not_found_retry_drops_thread_id():
    adapter = _make_adapter()
    call_log = []

    async def mock_send_animation(**kwargs):
        call_log.append(dict(kwargs))
        if len(call_log) == 1:
            raise FakeBadRequest("Message to be replied not found")
        return SimpleNamespace(message_id=786)

    adapter._bot = SimpleNamespace(send_animation=mock_send_animation)

    result = await adapter.send_animation(
        chat_id="123",
        animation_url="https://example.com/anim.gif",
        metadata={
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "telegram_reply_to_message_id": "462",
        },
    )

    assert result.success is True
    assert call_log[0]["reply_to_message_id"] == 462
    assert call_log[0]["message_thread_id"] == 20197
    assert call_log[1]["reply_to_message_id"] is None
    assert "message_thread_id" not in call_log[1]
    assert "direct_messages_topic_id" not in call_log[1]


@pytest.mark.asyncio
async def test_media_group_dm_topic_reply_not_found_retry_drops_thread_id(tmp_path):
    adapter = _make_adapter()
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"png-data")
    call_log = []

    async def mock_send_media_group(**kwargs):
        call_log.append(dict(kwargs))
        if len(call_log) == 1:
            raise FakeBadRequest("Message to be replied not found")
        return [SimpleNamespace(message_id=783)]

    adapter._bot = SimpleNamespace(send_media_group=mock_send_media_group)

    await adapter.send_multiple_images(
        chat_id="123",
        images=[(f"file://{image_path}", "caption")],
        metadata={
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "telegram_reply_to_message_id": "462",
        },
    )

    assert call_log[0]["reply_to_message_id"] == 462
    assert call_log[0]["message_thread_id"] == 20197
    assert call_log[1]["reply_to_message_id"] is None
    assert "message_thread_id" not in call_log[1]
    assert "direct_messages_topic_id" not in call_log[1]


@pytest.mark.asyncio
async def test_send_image_url_dm_topic_reply_not_found_retry_drops_thread_id(monkeypatch):
    adapter = _make_adapter()
    call_log = []

    async def mock_send_photo(**kwargs):
        call_log.append(dict(kwargs))
        if len(call_log) == 1:
            raise FakeBadRequest("Message to be replied not found")
        return SimpleNamespace(message_id=784)

    adapter._bot = SimpleNamespace(send_photo=mock_send_photo)
    import tools.url_safety as url_safety

    monkeypatch.setattr(url_safety, "is_safe_url", lambda _url: True)

    result = await adapter.send_image(
        chat_id="123",
        image_url="https://example.com/photo.png",
        metadata={
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "telegram_reply_to_message_id": "462",
        },
    )

    assert result.success is True
    assert call_log[0]["reply_to_message_id"] == 462
    assert call_log[0]["message_thread_id"] == 20197
    assert call_log[1]["reply_to_message_id"] is None
    assert "message_thread_id" not in call_log[1]
    assert "direct_messages_topic_id" not in call_log[1]


@pytest.mark.asyncio
async def test_send_image_upload_dm_topic_reply_not_found_retry_drops_thread_id(monkeypatch):
    adapter = _make_adapter()
    call_log = []

    async def mock_send_photo(**kwargs):
        call_log.append(dict(kwargs))
        if len(call_log) == 1:
            raise RuntimeError("URL is too large")
        if len(call_log) == 2:
            raise FakeBadRequest("Message to be replied not found")
        return SimpleNamespace(message_id=785)

    class _FakeResponse:
        content = b"image-data"

        def raise_for_status(self):
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, _url):
            return _FakeResponse()

    monkeypatch.setitem(
        sys.modules,
        "httpx",
        SimpleNamespace(AsyncClient=_FakeAsyncClient),
    )
    adapter._bot = SimpleNamespace(send_photo=mock_send_photo)
    import tools.url_safety as url_safety

    monkeypatch.setattr(url_safety, "is_safe_url", lambda _url: True)

    result = await adapter.send_image(
        chat_id="123",
        image_url="https://example.com/photo.png",
        metadata={
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "telegram_reply_to_message_id": "462",
        },
    )

    assert result.success is True
    assert call_log[0]["reply_to_message_id"] == 462
    assert call_log[0]["message_thread_id"] == 20197
    assert call_log[1]["reply_to_message_id"] == 462
    assert call_log[1]["message_thread_id"] == 20197
    assert call_log[2]["reply_to_message_id"] is None
    assert "message_thread_id" not in call_log[2]
    assert "direct_messages_topic_id" not in call_log[2]


@pytest.mark.asyncio
async def test_slash_confirm_private_topic_callback_followup_sends_thread_and_reply(monkeypatch):
    adapter = _make_adapter()
    adapter._slash_confirm_state = {"confirm-1": "session-1"}
    adapter._is_callback_user_authorized = lambda *args, **kwargs: True
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=9001)

    async def resolve(_session_key, _confirm_id, _choice):
        return "done"

    from tools import slash_confirm

    monkeypatch.setattr(slash_confirm, "resolve", resolve)
    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    class Query:
        data = "sc:once:confirm-1"
        from_user = SimpleNamespace(id=42, first_name="Alice")
        message = SimpleNamespace(
            chat_id=12345,
            chat=SimpleNamespace(type=_fake_telegram_constants.ChatType.PRIVATE),
            message_thread_id=20197,
            message_id=462,
        )

        async def answer(self, **kwargs):
            return None

        async def edit_message_text(self, **kwargs):
            return None

    await adapter._handle_callback_query(SimpleNamespace(callback_query=Query()), SimpleNamespace())

    assert call_log
    assert call_log[0]["message_thread_id"] == 20197
    assert call_log[0]["reply_to_message_id"] == 462


@pytest.mark.asyncio
async def test_slash_confirm_forum_callback_followup_keeps_existing_thread_behavior(monkeypatch):
    adapter = _make_adapter()
    adapter._slash_confirm_state = {"confirm-1": "session-1"}
    adapter._is_callback_user_authorized = lambda *args, **kwargs: True
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=9001)

    async def resolve(_session_key, _confirm_id, _choice):
        return "done"

    from tools import slash_confirm

    monkeypatch.setattr(slash_confirm, "resolve", resolve)
    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    class Query:
        data = "sc:once:confirm-1"
        from_user = SimpleNamespace(id=42, first_name="Alice")
        message = SimpleNamespace(
            chat_id=-100123,
            chat=SimpleNamespace(type=_fake_telegram_constants.ChatType.SUPERGROUP),
            message_thread_id=20197,
            message_id=462,
        )

        async def answer(self, **kwargs):
            return None

        async def edit_message_text(self, **kwargs):
            return None

    await adapter._handle_callback_query(SimpleNamespace(callback_query=Query()), SimpleNamespace())

    assert call_log
    assert call_log[0]["message_thread_id"] == 20197
    assert "reply_to_message_id" not in call_log[0]
    assert "direct_messages_topic_id" not in call_log[0]


@pytest.mark.asyncio
async def test_base_send_image_fallback_preserves_metadata():
    """Base image fallback should pass metadata through instead of referencing kwargs."""
    from gateway.platforms.base import BasePlatformAdapter

    class _ConcreteBaseAdapter(BasePlatformAdapter):
        async def connect(self):
            return True

        async def disconnect(self):
            return None

        async def send(self, **kwargs):
            call_log.append(kwargs)
            return SendResult(success=True, message_id="781")

        async def get_chat_info(self, chat_id):
            return None

    call_log = []
    adapter = _ConcreteBaseAdapter(Platform.TELEGRAM, None)
    metadata = {"thread_id": "20197"}

    result = await adapter.send_image(
        chat_id="123",
        image_url="https://example.invalid/image.png",
        metadata=metadata,
    )

    assert result.success is True
    assert call_log[0]["metadata"] is metadata


@pytest.mark.asyncio
async def test_send_raises_on_other_bad_request():
    """Non-thread BadRequest errors should NOT be retried — they fail immediately."""
    adapter = _make_adapter()

    async def mock_send_message(**kwargs):
        raise FakeBadRequest("Chat not found")

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="-100123",
        content="test message",
        metadata={"thread_id": "99999"},
    )

    assert result.success is False
    assert "Chat not found" in result.error


@pytest.mark.asyncio
async def test_send_without_thread_id_unaffected():
    """Normal sends without thread_id should work as before."""
    adapter = _make_adapter()

    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=100)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="123",
        content="test message",
    )

    assert result.success is True
    assert result.raw_response["thread_fallback"] is False
    assert len(call_log) == 1
    assert call_log[0]["message_thread_id"] is None


@pytest.mark.asyncio
async def test_send_converts_supported_miniapp_link_to_url_button():
    """Health/planning/menu Telegram Mini App deep links render as Web App buttons in DMs."""
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=100)

    adapter._bot = SimpleNamespace(send_message=mock_send_message, username="TripiooBot")

    result = await adapter.send(
        chat_id="123",
        content="Готово.\nhttps://t.me/TripiooBot?startapp=nutrition",
    )

    assert result.success is True
    assert len(call_log) == 1
    assert "https://t.me" not in call_log[0]["text"]
    assert "reply_markup" in call_log[0]
    button = call_log[0]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Открыть питание"
    web_app = button.kwargs.get("web_app")
    assert web_app.url == "https://miniapp.mayk05.pro/?startapp=nutrition"


@pytest.mark.asyncio
async def test_send_converts_menu_miniapp_link_to_button():
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=100)

    adapter._bot = SimpleNamespace(send_message=mock_send_message, username="TripiooBot")

    result = await adapter.send(
        chat_id="123",
        content="https://t.me/TripiooBot?startapp=menu",
    )

    assert result.success is True
    assert len(call_log) == 1
    assert call_log[0]["text"] == "Открой семейное меню в миниаппе\\."
    button = call_log[0]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Открыть меню"
    web_app = button.kwargs.get("web_app")
    assert web_app.url == "https://miniapp.mayk05.pro/?startapp=menu"


@pytest.mark.asyncio
async def test_send_keeps_supported_miniapp_link_as_url_button_in_groups():
    """Group buttons keep t.me startapp because Web App buttons are DM-only."""
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=100)

    adapter._bot = SimpleNamespace(send_message=mock_send_message, username="TripiooBot")

    result = await adapter.send(
        chat_id="-100123",
        content="Готово.\nhttps://t.me/TripiooBot?startapp=planning",
    )

    assert result.success is True
    button = call_log[0]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Открыть планирование"
    assert button.kwargs.get("url") == "https://t.me/TripiooBot?startapp=planning"
    assert button.kwargs.get("web_app") is None


@pytest.mark.asyncio
async def test_send_does_not_infer_menu_button_from_success_wording():
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=100)

    adapter._bot = SimpleNamespace(send_message=mock_send_message, username="TripiooBot")

    result = await adapter.send(
        chat_id="-1003966683704",
        content=(
            "Готово, добавил в семейное меню на сегодня, завтрак:\n\n"
            "• Хлеб в аэрогриле с яйцом\n"
            "• Бутерброды с творожным сыром"
        ),
    )

    assert result.success is True
    assert len(call_log) == 1
    assert "reply_markup" not in call_log[0]


@pytest.mark.asyncio
async def test_send_does_not_add_menu_button_for_plain_menu_word():
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=100)

    adapter._bot = SimpleNamespace(send_message=mock_send_message, username="TripiooBot")

    result = await adapter.send(
        chat_id="-1003966683704",
        content="В меню ресторана сегодня суп и салат.",
    )

    assert result.success is True
    assert len(call_log) == 1
    assert "reply_markup" not in call_log[0]


@pytest.mark.asyncio
async def test_send_converts_boxmap_miniapp_link_to_scenario_button():
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=100)

    adapter._bot = SimpleNamespace(send_message=mock_send_message, username="TripiooBot")

    result = await adapter.send(
        chat_id="-100123",
        content="Открыть сценарии BoxMap:\nhttps://t.me/TripiooBot?startapp=boxmap",
    )

    assert result.success is True
    assert len(call_log) == 1
    assert "https://t.me" not in call_log[0]["text"]
    button = call_log[0]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Открыть сценарии"
    assert button.kwargs.get("url") == "https://t.me/TripiooBot?startapp=boxmap"
    assert button.kwargs.get("web_app") is None


def test_miniapp_request_uses_configured_planning_topic_context():
    adapter = _make_adapter()
    adapter.config.extra = {
        "group_topics": [
            {
                "chat_id": -1003735932411,
                "topics": [
                    {
                        "name": "planning",
                        "thread_id": 313,
                        "skill": "telegram_planning/telegram_planning_context",
                    },
                ],
            },
        ],
    }

    message = SimpleNamespace(
        text="покажи миниапп",
        chat=SimpleNamespace(id=-1003735932411),
        message_thread_id=313,
    )

    assert adapter._miniapp_start_param_for_request(message) == "planning"


def test_planning_topic_open_today_plans_uses_miniapp_without_saying_miniapp():
    adapter = _make_adapter()
    adapter.config.extra = {
        "group_topics": [
            {
                "chat_id": -1003735932411,
                "topics": [
                    {
                        "name": "planning",
                        "thread_id": 313,
                        "skill": "telegram_planning/telegram_planning_context",
                    },
                ],
            },
        ],
    }

    message = SimpleNamespace(
        text="Открой планы на сегодня",
        chat=SimpleNamespace(id=-1003735932411),
        message_thread_id=313,
    )

    assert adapter._miniapp_start_param_for_request(message) == "planning"


def test_open_plan_outside_planning_topic_still_uses_planning_app():
    adapter = _make_adapter()

    message = SimpleNamespace(
        text="Открой план проекта",
        chat=SimpleNamespace(id=123),
        message_thread_id=None,
    )

    assert adapter._miniapp_start_param_for_request(message) == "planning"


def test_miniapp_data_actions_map_to_app_sections_without_saying_miniapp():
    adapter = _make_adapter()
    adapter.config.extra = {
        "group_topics": [
            {
                "chat_id": -1003735932411,
                "topics": [
                    {"name": "health", "thread_id": 321},
                    {"name": "planning", "thread_id": 313},
                ],
            },
            {
                "chat_id": -1003966683704,
                "topics": [{"name": "family-menu", "thread_id": 1636}],
            },
        ],
    }

    cases = [
        (-1003735932411, 313, "Добавь задачу купить молоко", "planning"),
        (-1003735932411, 321, "Покажи тренировки на сегодня", "fitness"),
        (-1003735932411, 321, "Сделал 20 отжиманий", "fitness"),
        (-1003735932411, 321, "Запиши прием лекарства", "meds"),
        (-1003735932411, 321, "Принял розувастатин", "meds"),
        (-1003735932411, 321, "Добавь завтрак: творог и ягоды", "nutrition"),
        (-1003735932411, 321, "Съел борщ", "nutrition"),
        (-1003966683704, 1636, "Добавь борщ на ужин", "menu"),
        (-1003966683704, 1636, "Добавь молоко в покупки", "shopping"),
    ]

    for chat_id, thread_id, text, expected in cases:
        message = SimpleNamespace(
            text=text,
            chat=SimpleNamespace(id=chat_id),
            message_thread_id=thread_id,
        )
        assert adapter._miniapp_start_param_for_request(message) == expected


def test_miniapp_engineering_request_is_not_treated_as_data_interaction():
    adapter = _make_adapter()

    message = SimpleNamespace(
        text="Почини форму планирования, там ошибка в коде",
        chat=SimpleNamespace(id=123),
        message_thread_id=None,
    )

    assert adapter._miniapp_start_param_for_request(message) is None


def test_direct_social_miniapp_request_uses_social_surface():
    adapter = _make_adapter()
    message = SimpleNamespace(
        text="Открой миниапп с черновиками публикаций",
        chat=SimpleNamespace(id=123),
        message_thread_id=None,
    )

    assert adapter._miniapp_start_param_for_request(message) == "social"


@pytest.mark.asyncio
async def test_successful_miniapp_mcp_write_attaches_button_to_final_response():
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=100 + len(call_log))

    adapter._bot = SimpleNamespace(send_message=mock_send_message, username="TripiooBot")
    adapter.begin_miniapp_turn("-1003735932411", "313")
    recorded = adapter.record_miniapp_tool_completion(
        "-1003735932411",
        "313",
        "mcp_health_actions_create_planning_task",
        is_error=False,
        result='{"result":"{\\"ok\\":true}"}',
    )

    progress = await adapter.send(
        chat_id="-1003735932411",
        content="⏳ Добавляю задачу…",
        metadata={"thread_id": "313"},
    )
    interim = await adapter.send(
        chat_id="-1003735932411",
        content="Проверил результат записи.",
        metadata={"thread_id": "313"},
    )
    final = await adapter.send(
        chat_id="-1003735932411",
        content="Готово.",
        metadata={"thread_id": "313", "notify": True},
    )
    unrelated = await adapter.send(
        chat_id="-1003735932411",
        content="Хорошего дня.",
        metadata={"thread_id": "313"},
    )

    assert recorded == "planning"
    assert progress.success is True
    assert interim.success is True
    assert final.success is True
    assert unrelated.success is True
    assert "reply_markup" not in call_log[0]
    assert "reply_markup" not in call_log[1]
    final_button = call_log[2]["reply_markup"].inline_keyboard[0][0]
    assert final_button.text == "Открыть планирование"
    assert final_button.kwargs.get("url") == "https://t.me/TripiooBot?startapp=planning"
    assert "reply_markup" not in call_log[3]


@pytest.mark.asyncio
async def test_streaming_final_edit_attaches_miniapp_mcp_button_once():
    adapter = _make_adapter()
    edit_log = []

    async def mock_edit_message_text(**kwargs):
        edit_log.append(dict(kwargs))
        return True

    adapter._bot = SimpleNamespace(
        edit_message_text=mock_edit_message_text,
        username="TripiooBot",
    )
    adapter.begin_miniapp_turn("-1003735932411", "313")
    adapter.record_miniapp_tool_completion(
        "-1003735932411",
        "313",
        "mcp_health_actions_update_planning_task",
    )

    interim = await adapter.edit_message(
        chat_id="-1003735932411",
        message_id="900",
        content="Готов",
        finalize=False,
        metadata={"thread_id": "313"},
    )
    final = await adapter.edit_message(
        chat_id="-1003735932411",
        message_id="900",
        content="Готово.",
        finalize=True,
        metadata={"thread_id": "313", "hermes_turn_final": True},
    )

    assert interim.success is True
    assert final.success is True
    assert "reply_markup" not in edit_log[0]
    button = edit_log[1]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Открыть планирование"
    assert button.kwargs.get("url") == "https://t.me/TripiooBot?startapp=planning"
    assert adapter._pending_miniapp_buttons == {}


@pytest.mark.asyncio
async def test_send_never_infers_buttons_from_response_wording():
    contents = [
        "Добавил задачу «Купить молоко».",
        "Записал тренировку и 20 повторов.",
        "Обновил список покупок.",
        "Записал прием лекарства.",
        (
            "Не удалось создать событие: инструмент Google Calendar недоступен. "
            "Событие не создано."
        ),
    ]

    for content in contents:
        adapter = _make_adapter()
        call_log = []

        async def mock_send_message(**kwargs):
            call_log.append(dict(kwargs))
            return SimpleNamespace(message_id=100)

        adapter._bot = SimpleNamespace(send_message=mock_send_message, username="TripiooBot")
        result = await adapter.send(chat_id="-100123", content=content)

        assert result.success is True
        assert "reply_markup" not in call_log[0]


@pytest.mark.parametrize(
    ("tool_name", "expected_param"),
    [
        ("mcp_health_actions_create_workout_slots", "fitness"),
        ("mcp.health-actions.record-completed-set", "fitness"),
        ("mcp_health_actions_create_planning_task", "planning"),
        ("mcp_health_actions_add_shopping_items", "shopping"),
        ("mcp_health_actions_replace_menu_entries", "menu"),
    ],
)
def test_mutating_miniapp_mcp_tools_map_to_changed_app(tool_name, expected_param):
    adapter = _make_adapter()

    assert adapter._miniapp_param_for_mutating_mcp_tool(tool_name) == expected_param


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp_health_actions_list_planning_tasks",
        "mcp_health_actions_list_menu_entries",
        "mcp_google_calendar_create_event",
        "terminal",
    ],
)
def test_reads_and_non_miniapp_tools_do_not_map_to_buttons(tool_name):
    adapter = _make_adapter()

    assert adapter._miniapp_param_for_mutating_mcp_tool(tool_name) is None


@pytest.mark.parametrize(
    ("start_param", "expected_label"),
    [
        ("fitness", "Открыть тренировки"),
        ("nutrition", "Открыть питание"),
        ("medications", "Открыть лекарства"),
        ("planning", "Открыть планирование"),
        ("menu", "Открыть меню"),
        ("shopping", "Открыть покупки"),
        ("boxmap", "Открыть сценарии"),
        ("social", "Открыть публикации"),
    ],
)
def test_full_miniapp_catalog_has_supported_buttons(start_param, expected_label):
    adapter = _make_adapter()

    assert adapter._is_supported_inline_miniapp_param(start_param) is True
    assert adapter._miniapp_button_label(start_param) == expected_label


@pytest.mark.asyncio
async def test_generic_miniapp_mutation_contract_supports_boxmap_and_social():
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=100)

    adapter._bot = SimpleNamespace(send_message=mock_send_message, username="TripiooBot")
    adapter.begin_miniapp_turn("-100123")
    boxmap = adapter.record_miniapp_tool_completion(
        "-100123",
        None,
        "mcp_miniapps_update_scenario",
        result=json.dumps(
            {
                "result": "Scenario updated",
                "structuredContent": {
                    "_miniapp": {
                        "changed": True,
                        "start_param": "boxmap",
                    }
                },
            }
        ),
    )
    social = adapter.record_miniapp_tool_completion(
        "-100123",
        None,
        "social_post",
        result=json.dumps(
            {
                "ok": True,
                "_miniapp": {
                    "changed": True,
                    "start_param": "social",
                },
            }
        ),
    )

    result = await adapter.send(
        chat_id="-100123",
        content="Готово.",
        metadata={"hermes_turn_final": True},
    )

    assert boxmap == "boxmap"
    assert social == "social"
    assert result.success is True
    rows = call_log[0]["reply_markup"].inline_keyboard
    assert [row[0].text for row in rows] == [
        "Открыть сценарии",
        "Открыть публикации",
    ]


def test_explicit_unchanged_contract_overrides_legacy_tool_mapping():
    adapter = _make_adapter()

    recorded = adapter.record_miniapp_tool_completion(
        "-100123",
        None,
        "mcp_health_actions_create_workout_slots",
        result=json.dumps(
            {
                "structuredContent": {
                    "_miniapp": {
                        "changed": False,
                        "start_param": "fitness",
                    }
                }
            }
        ),
    )

    assert recorded is None
    assert getattr(adapter, "_pending_miniapp_buttons", {}) == {}


def test_plain_miniapp_url_without_mutation_contract_does_not_add_button():
    adapter = _make_adapter()

    recorded = adapter.record_miniapp_tool_completion(
        "-100123",
        None,
        "social_post",
        result=json.dumps(
            {
                "drafts": [],
                "miniapp_url": "https://t.me/TripiooBot?startapp=social",
            }
        ),
    )

    assert recorded is None
    assert getattr(adapter, "_pending_miniapp_buttons", {}) == {}


def test_non_mcp_tool_cannot_claim_miniapp_mutation_contract():
    adapter = _make_adapter()

    recorded = adapter.record_miniapp_tool_completion(
        "-100123",
        None,
        "terminal",
        result=json.dumps(
            {
                "_miniapp": {
                    "changed": True,
                    "start_param": "boxmap",
                }
            }
        ),
    )

    assert recorded is None
    assert getattr(adapter, "_pending_miniapp_buttons", {}) == {}


@pytest.mark.asyncio
async def test_failed_or_read_only_miniapp_mcp_does_not_attach_button():
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=100)

    adapter._bot = SimpleNamespace(send_message=mock_send_message, username="TripiooBot")
    adapter.begin_miniapp_turn("-100123")
    failed = adapter.record_miniapp_tool_completion(
        "-100123",
        None,
        "mcp_health_actions_create_planning_task",
        is_error=True,
        result='{"error":"write failed"}',
    )
    read_only = adapter.record_miniapp_tool_completion(
        "-100123",
        None,
        "mcp_health_actions_list_planning_tasks",
        is_error=False,
        result='{"result":"[]"}',
    )

    result = await adapter.send(
        chat_id="-100123",
        content="Событие не создано.",
    )

    assert failed is None
    assert read_only is None
    assert result.success is True
    assert "reply_markup" not in call_log[0]


@pytest.mark.asyncio
async def test_multiple_miniapp_mcp_writes_attach_only_changed_apps():
    adapter = _make_adapter()
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=100)

    adapter._bot = SimpleNamespace(send_message=mock_send_message, username="TripiooBot")
    adapter.begin_miniapp_turn("-100123")
    adapter.record_miniapp_tool_completion(
        "-100123", None, "mcp_health_actions_add_menu_entries"
    )
    adapter.record_miniapp_tool_completion(
        "-100123", None, "mcp_health_actions_add_shopping_items"
    )
    adapter.record_miniapp_tool_completion(
        "-100123", None, "mcp_google_calendar_create_event"
    )

    result = await adapter.send(
        chat_id="-100123",
        content="Готово.",
        metadata={"hermes_turn_final": True},
    )

    assert result.success is True
    rows = call_log[0]["reply_markup"].inline_keyboard
    assert [row[0].text for row in rows] == ["Открыть меню", "Открыть покупки"]


@pytest.mark.asyncio
async def test_send_suppresses_inline_miniapp_buttons_for_disabled_dm():
    adapter = _make_adapter()
    adapter.config.extra = {
        "inline_miniapp_buttons": {"disabled_chat_ids": ["179555559"]},
    }
    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        return SimpleNamespace(message_id=100)

    adapter._bot = SimpleNamespace(send_message=mock_send_message, username="TripiooBot")
    pending_key = adapter._miniapp_pending_key("179555559")
    adapter._pending_miniapp_buttons = {pending_key: ["planning"]}
    result = await adapter.send(chat_id="179555559", content="Создал техническую задачу.")

    assert result.success is True
    assert "reply_markup" not in call_log[0]
    assert adapter._pending_miniapp_buttons == {}


def test_miniapp_request_uses_family_menu_section_in_family_topic():
    adapter = _make_adapter()
    adapter.config.extra = {
        "group_topics": [
            {
                "chat_id": -1003966683704,
                "topics": [
                    {
                        "name": "family",
                        "thread_id": 1636,
                        "skill": "telegram_family/telegram_family_context",
                    },
                ],
            },
        ],
    }

    message = SimpleNamespace(
        text="сделай миниапп секцию для меню которое можно будет конвертировать в список продуктов",
        chat=SimpleNamespace(id=-1003966683704),
        message_thread_id=1636,
    )

    assert adapter._miniapp_start_param_for_request(message) == "menu"


def test_miniapp_engineering_edit_request_is_not_shortcut_outside_family():
    adapter = _make_adapter()

    message = SimpleNamespace(
        text="почини миниапп меню, там сломалась форма",
        chat=SimpleNamespace(id=123),
        message_thread_id=None,
    )

    assert adapter._miniapp_start_param_for_request(message) is None


@pytest.mark.asyncio
async def test_send_retries_network_errors_normally():
    """Real transient network errors (not BadRequest) should still be retried."""
    adapter = _make_adapter()

    attempt = [0]

    async def mock_send_message(**kwargs):
        attempt[0] += 1
        if attempt[0] < 3:
            raise FakeNetworkError("Connection reset")
        return SimpleNamespace(message_id=200)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="123",
        content="test message",
    )

    assert result.success is True
    assert attempt[0] == 3  # Two retries then success


@pytest.mark.asyncio
async def test_send_does_not_retry_timeout():
    """TimedOut (subclass of NetworkError) should NOT be retried in send().

    The request may have already been delivered to the user — retrying
    would send duplicate messages.
    """
    adapter = _make_adapter()

    attempt = [0]

    async def mock_send_message(**kwargs):
        attempt[0] += 1
        raise FakeTimedOut("Timed out waiting for Telegram response")

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(
        chat_id="123",
        content="test message",
    )

    assert result.success is False
    assert "Timed out" in result.error
    # CRITICAL: only 1 attempt — no retry for TimedOut
    assert attempt[0] == 1


@pytest.mark.asyncio
async def test_send_retries_wrapped_connect_timeout():
    """Retry TimedOut only when it wraps a TCP connect timeout.

    A generic Telegram TimedOut may have reached Telegram and must not be
    retried, but an underlying ConnectTimeout means the connection was never
    established. Retrying prevents a silent drop without risking duplicates.
    """
    adapter = _make_adapter()

    class FakeConnectTimeout(Exception):
        pass

    attempt = [0]

    async def mock_send_message(**kwargs):
        attempt[0] += 1
        if attempt[0] < 3:
            err = FakeTimedOut("Timed out")
            err.__cause__ = FakeConnectTimeout("connect timed out")
            raise err
        return SimpleNamespace(message_id=201)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(chat_id="123", content="test message")

    assert result.success is True
    assert result.message_id == "201"
    assert attempt[0] == 3


@pytest.mark.asyncio
async def test_send_marks_wrapped_connect_timeout_retryable_after_exhaustion():
    """Final SendResult remains retryable for outer gateway retry handling."""
    adapter = _make_adapter()

    class FakeConnectTimeout(Exception):
        pass

    attempt = [0]

    async def mock_send_message(**kwargs):
        attempt[0] += 1
        err = FakeTimedOut("Timed out")
        err.__context__ = FakeConnectTimeout("ConnectTimeout")
        raise err

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(chat_id="123", content="test message")

    assert result.success is False
    assert result.retryable is True
    assert attempt[0] == 3


@pytest.mark.asyncio
async def test_send_retries_pool_timeout():
    """Retry TimedOut when it is an httpx pool-timeout (request not sent).

    PTB wraps ``httpx.PoolTimeout`` into ``TimedOut`` with a message that
    explicitly states the request was *not* sent to Telegram. Re-sending is
    safe and prevents a silent drop when the pool frees up.
    """
    adapter = _make_adapter()

    attempt = [0]

    async def mock_send_message(**kwargs):
        attempt[0] += 1
        if attempt[0] < 3:
            raise FakeTimedOut(
                "Pool timeout: All connections in the connection pool are "
                "occupied. Request was *not* sent to Telegram. Consider "
                "adjusting the connection pool size or the pool timeout."
            )
        return SimpleNamespace(message_id=202)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(chat_id="123", content="test message")

    assert result.success is True
    assert result.message_id == "202"
    assert attempt[0] == 3


@pytest.mark.asyncio
async def test_send_marks_pool_timeout_retryable_after_exhaustion():
    """Pool timeout that never clears stays retryable for outer retry handling."""
    adapter = _make_adapter()

    attempt = [0]

    async def mock_send_message(**kwargs):
        attempt[0] += 1
        raise FakeTimedOut(
            "Pool timeout: All connections in the connection pool are occupied. "
            "Request was *not* sent to Telegram."
        )

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(chat_id="123", content="test message")

    assert result.success is False
    assert result.retryable is True
    assert attempt[0] == 3


@pytest.mark.asyncio
async def test_multichunk_group_send_never_falls_back_to_general():
    """A failed first topic chunk stops the send before anything reaches General."""
    adapter = _make_adapter()

    call_log = []

    async def mock_send_message(**kwargs):
        call_log.append(dict(kwargs))
        tid = kwargs.get("message_thread_id")
        if tid is not None:
            raise FakeBadRequest("Message thread not found")
        return SimpleNamespace(message_id=42)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    # Send a long message that gets split into chunks
    long_msg = "A" * 5000  # Exceeds Telegram's 4096 limit
    result = await adapter.send(
        chat_id="-100123",
        content=long_msg,
        metadata={"thread_id": "99999"},
    )

    assert result.success is False
    assert result.retryable is False
    assert len(call_log) == 2
    assert all(call["message_thread_id"] == 99999 for call in call_log)


@pytest.mark.asyncio
async def test_send_retries_retry_after_errors():
    """Telegram flood control should back off and retry instead of failing fast."""
    adapter = _make_adapter()

    attempt = [0]

    async def mock_send_message(**kwargs):
        attempt[0] += 1
        if attempt[0] == 1:
            raise FakeRetryAfter(2)
        return SimpleNamespace(message_id=300)

    adapter._bot = SimpleNamespace(send_message=mock_send_message)

    result = await adapter.send(chat_id="123", content="test message")

    assert result.success is True
    assert result.message_id == "300"
    assert attempt[0] == 2
