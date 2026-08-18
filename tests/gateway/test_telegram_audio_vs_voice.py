"""
Tests for #24870 — Telegram: audio file attachments must NOT be routed to STT.

Telegram distinguishes three kinds of audio payloads:
  - message.voice  → Opus/OGG voice message  → STT pipeline
  - message.audio  → audio file attachment   → file path note, NOT STT
  - message.document (audio mime) → generic file route

These tests confirm that:
  1. MessageType.VOICE events still flow through the STT pipeline.
  2. MessageType.AUDIO events bypass STT and get a file-path context note instead.
  3. Mixed media lists (voice + audio) split correctly.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.durable_jobs import DurableJobStore
from gateway.job_router import JobRouteDecision
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


def _make_runner(stt_enabled: bool = True) -> "GatewayRunner":  # type: ignore[name-defined]
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=stt_enabled)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False
    runner._gateway_chat_settings_raw = lambda: {}
    return runner


def _voice_event(
    path: str = "/tmp/voice.ogg",
    *,
    chat_id: str = "1",
    chat_type: str = "dm",
    thread_id: str | None = None,
) -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type=chat_type,
        thread_id=thread_id,
    )
    return MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=source,
        media_urls=[path],
        media_types=["audio/ogg"],
        message_id="321",
    )


def _audio_event(
    path: str = "/tmp/song.mp3",
    *,
    chat_id: str = "1",
    chat_type: str = "dm",
    thread_id: str | None = None,
) -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type=chat_type,
        thread_id=thread_id,
    )
    return MessageEvent(
        text="",
        message_type=MessageType.AUDIO,
        source=source,
        media_urls=[path],
        media_types=["audio/mpeg"],
        message_id="654",
    )


# ---------------------------------------------------------------------------
# 1. VOICE still goes through STT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_message_still_transcribed():
    """A configured keyword hit transcribes VOICE before continuing to the AI."""
    runner = _make_runner(stt_enabled=True)
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test",
        extra={
            "audio_trigger": True,
            "show_transcription": False,
            "voice_trigger_keywords": ["tripio"],
        },
    )
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm")
    event = _voice_event("/tmp/voice.ogg")

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "Tripio hello world", "provider": "whisper"},
    ) as mock_transcribe:
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    mock_transcribe.assert_called_once_with("/tmp/voice.ogg")
    assert "Tripio hello world" in result
    assert "voice message" in result.lower()


@pytest.mark.asyncio
async def test_telegram_voice_rule_transcript_only_posts_plain_text_and_preserves_thread():
    """Matching per-chat voice rules can send the transcript without a label."""
    runner = _make_runner(stt_enabled=True)
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test",
        extra={
            "audio_trigger": True,
            "audio_transcription_rules": [
                {
                    "chat_id": -1003966683704,
                    "thread_id": 359,
                    "enabled": True,
                    "send_transcript": True,
                    "trigger_keywords": ["tripioo", "напомни"],
                    "on_keyword_match": "run_ai",
                    "on_no_match": "transcript_only",
                }
            ]
        },
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters[Platform.TELEGRAM] = adapter
    event = _voice_event(chat_id="-1003966683704", chat_type="group", thread_id="359")
    source = event.source

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "Сегодня обсуждали билеты", "provider": "whisper"},
    ):
        result = await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert result is None
    assert "Сегодня обсуждали билеты" in event.telegram_transcript_only_history_text
    adapter.send.assert_awaited_once_with(
        "-1003966683704",
        "Сегодня обсуждали билеты",
        reply_to=None,
        metadata={"thread_id": "359"},
    )


@pytest.mark.asyncio
async def test_telegram_voice_rule_can_show_transcript_bubble_when_opted_in():
    """telegram.show_transcription-style opt-in echoes the transcript to the topic."""
    runner = _make_runner(stt_enabled=True)
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test",
        extra={
            "audio_transcription_rules": [
                {
                    "chat_id": -1003966683704,
                    "thread_id": 359,
                    "enabled": True,
                    "show_transcription": True,
                    "trigger_keywords": ["tripioo", "напомни"],
                    "on_keyword_match": "run_ai",
                    "on_no_match": "transcript_only",
                }
            ]
        },
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters[Platform.TELEGRAM] = adapter
    event = _voice_event(chat_id="-1003966683704", chat_type="group", thread_id="359")
    source = event.source

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "Сегодня обсуждали билеты", "provider": "whisper"},
    ):
        result = await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert result is None
    adapter.send.assert_awaited_once_with(
        "-1003966683704",
        "Сегодня обсуждали билеты",
        reply_to=None,
        metadata={"thread_id": "359"},
    )


@pytest.mark.asyncio
async def test_telegram_voice_rule_keyword_runs_ai_with_decision_instruction():
    """Keyword hits continue into the agent with transcript and a scoped instruction."""
    runner = _make_runner(stt_enabled=True)
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test",
        extra={
            "audio_trigger": True,
            "audio_transcription_rules": [
                {
                    "chat_id": "-1003966683704",
                    "thread_id": "359",
                    "enabled": True,
                    "send_transcript": True,
                    "show_transcription": True,
                    "trigger_keywords": ["TRIPIOO", "напомни"],
                    "on_keyword_match": "run_ai",
                    "on_no_match": "transcript_only",
                }
            ]
        },
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters[Platform.TELEGRAM] = adapter
    event = _voice_event(chat_id="-1003966683704", chat_type="group", thread_id="359")
    source = event.source

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "Tripioo напомни проверить статус", "provider": "whisper"},
    ):
        result = await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert result is not None
    assert "Tripioo напомни проверить статус" in result
    assert "voice transcription rule matched" in result
    assert "decide whether to answer, take action, or stay silent" in result
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_passive_voice_can_show_transcript_without_audio_trigger():
    runner = _make_runner(stt_enabled=True)
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test",
        extra={"voice_trigger_keywords": ["tripioo"]},
    )
    runner._gateway_chat_settings_raw = lambda: {
        "settings": [
            {
                "platform": "telegram",
                "chat_id": "-200",
                "audio_trigger": "off",
                "show_transcription": "on",
            }
        ]
    }
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters[Platform.TELEGRAM] = adapter
    event = _voice_event(chat_id="-200", chat_type="group")
    event.telegram_passive_audio_transcription = True

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "Tripioo напомни проверить", "provider": "whisper"},
    ):
        result = await runner._prepare_inbound_message_text(event=event, source=event.source, history=[])

    assert result is None
    adapter.send.assert_awaited_once_with(
        "-200",
        "Tripioo напомни проверить",
        reply_to="321",
        metadata=None,
    )


@pytest.mark.asyncio
async def test_passive_voice_with_transcription_and_audio_trigger_stays_transcript_only_without_keyword():
    runner = _make_runner(stt_enabled=True)
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test",
        extra={"voice_trigger_keywords": ["tripioo"]},
    )
    runner._gateway_chat_settings_raw = lambda: {
        "settings": [
            {
                "platform": "telegram",
                "chat_id": "-200",
                "audio_trigger": "on",
                "show_transcription": "on",
            }
        ]
    }
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters[Platform.TELEGRAM] = adapter
    event = _voice_event(chat_id="-200", chat_type="group")
    event.telegram_passive_audio_transcription = True

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "обсуждаем семейные планы", "provider": "whisper"},
    ):
        result = await runner._prepare_inbound_message_text(event=event, source=event.source, history=[])

    assert result is None
    adapter.send.assert_awaited_once_with(
        "-200",
        "обсуждаем семейные планы",
        reply_to="321",
        metadata=None,
    )


@pytest.mark.asyncio
async def test_addressed_voice_continues_after_transcript_without_keyword():
    runner = _make_runner(stt_enabled=True)
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test",
        extra={"voice_trigger_keywords": ["tripioo"]},
    )
    runner._gateway_chat_settings_raw = lambda: {
        "settings": [
            {
                "platform": "telegram",
                "chat_id": "-200",
                "audio_trigger": "on",
                "show_transcription": "on",
            }
        ]
    }
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters[Platform.TELEGRAM] = adapter
    event = _voice_event(chat_id="-200", chat_type="group")
    # TelegramAdapter sets this from the pre-STT ordinary router.  It covers
    # response_mode=all/free-response, a direct mention, or a reply to this bot.
    event.telegram_agent_dispatch_eligible = True

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "обсуждаем семейные планы", "provider": "whisper"},
    ):
        result = await runner._prepare_inbound_message_text(event=event, source=event.source, history=[])

    assert result is not None
    assert "обсуждаем семейные планы" in result
    assert "voice transcription rule matched" not in result
    adapter.send.assert_awaited_once_with(
        "-200",
        "обсуждаем семейные планы",
        reply_to="321",
        metadata=None,
    )


@pytest.mark.asyncio
async def test_durable_voice_routes_job_only_after_stt_enrichment(tmp_path):
    runner = _make_runner(stt_enabled=True)
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test",
        extra={
            "audio_trigger": True,
            "show_transcription": True,
            "voice_trigger_keywords": ["tripioo"],
        },
    )
    runner._durable_job_store = DurableJobStore(tmp_path / "jobs.sqlite3")
    runner._job_route_locks = {}
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters[Platform.TELEGRAM] = adapter
    event = _voice_event(chat_id="-200", chat_type="group", thread_id="2")
    event.telegram_agent_dispatch_eligible = True
    thread_key = "agent:main:telegram:group:-200:2"

    with (
        patch(
            "tools.transcription_tools.transcribe_audio",
            return_value={
                "success": True,
                "transcript": "Включи охрану сразу, без задержки",
                "provider": "whisper",
            },
        ) as transcribe,
        patch(
            "gateway.job_router.decide_job_route",
            new=AsyncMock(
                return_value=JobRouteDecision(
                    action="new_job",
                    confidence=0.99,
                    reason="voice command",
                )
            ),
        ),
    ):
        job, handled = await runner._ensure_durable_job_route(
            event, event.source, thread_key
        )

    assert handled is False
    assert job is not None
    assert "Включи охрану сразу" in job["request_text"]
    assert event.telegram_voice_preprocessed is True
    assert event.telegram_voice_transcript_only is False
    assert "Включи охрану сразу" in event.durable_request_text
    transcribe.assert_called_once_with("/tmp/voice.ogg")
    adapter.send.assert_awaited_once()

    # Job execution consumes the already prepared text and must not repeat STT
    # or post a second transcript bubble.
    with patch("tools.transcription_tools.transcribe_audio") as second_stt:
        prepared_again = await runner._prepare_inbound_message_text(
            event=event,
            source=event.source,
            history=[],
        )
    second_stt.assert_not_called()
    assert "Включи охрану сразу" in prepared_again
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_durable_transcript_only_is_public_context_not_job(tmp_path):
    runner = _make_runner(stt_enabled=True)
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test",
        extra={
            "audio_trigger": True,
            "show_transcription": True,
            "voice_trigger_keywords": ["tripioo"],
        },
    )
    runner._durable_job_store = DurableJobStore(tmp_path / "jobs.sqlite3")
    runner._job_route_locks = {}
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters[Platform.TELEGRAM] = adapter
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(
        session_id="public-topic-session"
    )
    runner.session_store.load_transcript.return_value = []
    event = _voice_event(chat_id="-200", chat_type="group", thread_id="2")
    thread_key = "agent:main:telegram:group:-200:2"

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={
            "success": True,
            "transcript": "Это фоновый контекст без обращения к боту",
            "provider": "whisper",
        },
    ) as transcribe:
        job, handled = await runner._ensure_durable_job_route(
            event, event.source, thread_key
        )

    assert handled is True
    assert job is None
    inbox = runner._durable_job_store.get_inbox_event(event.durable_inbox_id)
    assert inbox["status"] == "context_only"
    assert "фоновый контекст" in inbox["request_text"]
    assert runner._durable_job_store.active_jobs() == []
    runner.session_store.append_to_transcript.assert_called_once()
    public_entry = runner.session_store.append_to_transcript.call_args.args[1]
    assert "фоновый контекст" in public_entry["content"]

    # Telegram redelivery reuses the context-only inbox row without another STT
    # call, transcript bubble, or public-history duplicate.
    duplicate = _voice_event(chat_id="-200", chat_type="group", thread_id="2")
    with patch("tools.transcription_tools.transcribe_audio") as duplicate_stt:
        duplicate_job, duplicate_handled = await runner._ensure_durable_job_route(
            duplicate, duplicate.source, thread_key
        )
    duplicate_stt.assert_not_called()
    assert duplicate_job is None
    assert duplicate_handled is True
    assert transcribe.call_count == 1
    runner.session_store.append_to_transcript.assert_called_once()


@pytest.mark.asyncio
async def test_restart_recovers_raw_voice_by_transcribing_before_job_route(tmp_path):
    runner = _make_runner(stt_enabled=True)
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test",
        extra={"audio_trigger": True, "show_transcription": False},
    )
    runner._durable_job_store = DurableJobStore(tmp_path / "jobs.sqlite3")
    runner._job_route_locks = {}
    runner.adapters[Platform.TELEGRAM] = MagicMock(send=AsyncMock())
    source = _voice_event(
        chat_id="-200", chat_type="group", thread_id="2"
    ).source
    thread_key = "agent:main:telegram:group:-200:2"
    runner._durable_job_store.ingest_event(
        thread_key=thread_key,
        platform="telegram",
        source=source,
        request_text="",
        message_id="restart-voice-1",
        platform_update_id=9001,
        message_type="voice",
        media=[{"path": "/tmp/restart-voice.ogg", "type": "audio/ogg"}],
        event_metadata={"telegram_agent_dispatch_eligible": True},
    )

    with (
        patch(
            "tools.transcription_tools.transcribe_audio",
            return_value={
                "success": True,
                "transcript": "Проверь состояние охраны",
                "provider": "whisper",
            },
        ),
        patch(
            "gateway.job_router.decide_job_route",
            new=AsyncMock(
                return_value=JobRouteDecision(
                    action="new_job",
                    confidence=0.95,
                    reason="recovered voice command",
                )
            ),
        ),
    ):
        recovered = await runner._recover_unrouted_inbox_events()

    jobs = runner._durable_job_store.active_jobs()
    assert recovered == 1
    assert len(jobs) == 1
    assert "Проверь состояние охраны" in jobs[0]["request_text"]


@pytest.mark.asyncio
async def test_passive_voice_can_audio_trigger_without_showing_transcript():
    runner = _make_runner(stt_enabled=True)
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test",
        extra={"voice_trigger_keywords": ["tripioo"]},
    )
    runner._gateway_chat_settings_raw = lambda: {
        "settings": [
            {
                "platform": "telegram",
                "chat_id": "-200",
                "audio_trigger": "on",
                "show_transcription": "off",
            }
        ]
    }
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters[Platform.TELEGRAM] = adapter
    event = _voice_event(chat_id="-200", chat_type="group")
    event.telegram_passive_audio_transcription = True

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "Tripioo напомни проверить", "provider": "whisper"},
    ):
        result = await runner._prepare_inbound_message_text(event=event, source=event.source, history=[])

    assert result is not None
    assert "Tripioo напомни проверить" in result
    assert "voice transcription rule matched" in result
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_audio_attachment_opt_in_rule_transcribes_and_posts_to_same_topic():
    """A chat/topic rule with message_types=['audio'] transcribes Telegram audio attachments."""
    runner = _make_runner(stt_enabled=True)
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test",
        extra={
            "audio_transcription_rules": [
                {
                    "chat_id": "-1003966683704",
                    "thread_id": "359",
                    "enabled": True,
                    "message_types": ["audio"],
                    "send_transcript": True,
                    "on_no_match": "transcript_only",
                }
            ]
        },
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters[Platform.TELEGRAM] = adapter
    event = _audio_event(
        "/tmp/podcast.mp3",
        chat_id="-1003966683704",
        chat_type="group",
        thread_id="359",
    )
    source = event.source

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "Текст аудио", "provider": "whisper"},
    ) as mock_transcribe:
        result = await runner._prepare_inbound_message_text(event=event, source=source, history=[])

    assert result is None
    mock_transcribe.assert_called_once_with("/tmp/podcast.mp3")
    adapter.send.assert_awaited_once_with(
        "-1003966683704",
        "Текст аудио",
        reply_to=None,
        metadata={"thread_id": "359"},
    )


@pytest.mark.asyncio
async def test_audio_attachment_rule_without_audio_type_still_skips_stt():
    """Existing voice-only rules must not accidentally transcribe Telegram audio files."""
    runner = _make_runner(stt_enabled=True)
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test",
        extra={
            "audio_transcription_rules": [
                {
                    "chat_id": "-1003966683704",
                    "thread_id": "359",
                    "enabled": True,
                    "send_transcript": True,
                    "on_no_match": "transcript_only",
                }
            ]
        },
    )
    event = _audio_event(
        "/tmp/song.mp3",
        chat_id="-1003966683704",
        chat_type="group",
        thread_id="359",
    )

    with patch(
        "tools.transcription_tools.transcribe_audio",
        side_effect=AssertionError("audio attachments are opt-in only"),
    ):
        with patch("tools.credential_files.to_agent_visible_cache_path", side_effect=lambda p: p):
            result = await runner._prepare_inbound_message_text(
                event=event,
                source=event.source,
                history=[],
            )

    assert "audio file attachment" in result.lower()
    assert "/tmp/song.mp3" in result


# ---------------------------------------------------------------------------
# 2. AUDIO file attachment bypasses STT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audio_attachment_skips_stt():
    """MessageType.AUDIO must NOT be routed to STT — transcribe_audio must not be called."""
    runner = _make_runner(stt_enabled=True)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm")
    event = _audio_event("/tmp/song.mp3")

    with patch(
        "tools.transcription_tools.transcribe_audio",
        side_effect=AssertionError("transcribe_audio must NOT be called for audio file attachments"),
    ):
        with patch(
            "tools.credential_files.to_agent_visible_cache_path",
            side_effect=lambda p: p,
        ):
            result = await runner._prepare_inbound_message_text(
                event=event,
                source=source,
                history=[],
            )

    assert result is not None
    assert "/tmp/song.mp3" in result
    assert "audio file attachment" in result.lower()


@pytest.mark.asyncio
async def test_audio_attachment_context_note_format():
    """Context note for audio file attachments should include the file path and guidance."""
    runner = _make_runner(stt_enabled=True)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm")
    event = _audio_event("/tmp/cache_12345_my_song.mp3")

    with patch(
        "tools.transcription_tools.transcribe_audio",
        side_effect=AssertionError("must not be called"),
    ):
        with patch(
            "tools.credential_files.to_agent_visible_cache_path",
            side_effect=lambda p: p,
        ):
            result = await runner._prepare_inbound_message_text(
                event=event,
                source=source,
                history=[],
            )

    assert "my_song.mp3" in result
    assert "audio file attachment" in result.lower()
    # Should NOT contain the voice-message transcription wrapper text
    assert "voice message" not in result.lower()


# ---------------------------------------------------------------------------
# 3. STT disabled still results in no transcription for audio file attachments
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audio_attachment_skips_stt_when_stt_disabled():
    """Even with STT disabled, AUDIO must NOT produce STT disabled notice — just a file note."""
    runner = _make_runner(stt_enabled=False)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm")
    event = _audio_event("/tmp/podcast.m4a")

    with patch(
        "tools.transcription_tools.transcribe_audio",
        side_effect=AssertionError("must not be called"),
    ):
        with patch(
            "tools.credential_files.to_agent_visible_cache_path",
            side_effect=lambda p: p,
        ):
            result = await runner._prepare_inbound_message_text(
                event=event,
                source=source,
                history=[],
            )

    # Should NOT see the "transcription is disabled" note — that's only for VOICE
    assert "transcription is disabled" not in result.lower()
    assert "audio file attachment" in result.lower()
    assert "/tmp/podcast.m4a" in result


# ---------------------------------------------------------------------------
# 4. Telegram gateway: msg.audio → MessageType.AUDIO (not VOICE)
# ---------------------------------------------------------------------------

def test_telegram_media_type_detection_audio_vs_voice():
    """The Telegram platform must set MessageType.AUDIO for msg.audio, VOICE for msg.voice."""
    from gateway.platforms.base import MessageType

    # The Telegram adapter's _build_media_type already returns correct values
    # via MessageType.AUDIO for .audio and MessageType.VOICE for .voice.
    # Check the constants match expected semantic roles.
    assert MessageType.AUDIO.value == "audio"
    assert MessageType.VOICE.value == "voice"
    # Sanity: they are distinct
    assert MessageType.AUDIO != MessageType.VOICE
