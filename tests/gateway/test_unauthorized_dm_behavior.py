from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _clear_auth_env(monkeypatch) -> None:
    for key in (
        "TELEGRAM_ALLOWED_USERS",
        "TELEGRAM_GROUP_ALLOWED_USERS",
        "DISCORD_ALLOWED_USERS",
        "WHATSAPP_ALLOWED_USERS",
        "SLACK_ALLOWED_USERS",
        "SIGNAL_ALLOWED_USERS",
        "SIGNAL_GROUP_ALLOWED_USERS",
        "TELEGRAM_GROUP_ALLOWED_CHATS",
        "EMAIL_ALLOWED_USERS",
        "SMS_ALLOWED_USERS",
        "MATTERMOST_ALLOWED_USERS",
        "MATRIX_ALLOWED_USERS",
        "DINGTALK_ALLOWED_USERS", "FEISHU_ALLOWED_USERS", "WECOM_ALLOWED_USERS",
        "QQ_ALLOWED_USERS", "QQ_GROUP_ALLOWED_USERS",
        "GATEWAY_ALLOWED_USERS",
        "TELEGRAM_ALLOW_ALL_USERS",
        "DISCORD_ALLOW_ALL_USERS",
        "WHATSAPP_ALLOW_ALL_USERS",
        "SLACK_ALLOW_ALL_USERS",
        "SIGNAL_ALLOW_ALL_USERS",
        "EMAIL_ALLOW_ALL_USERS",
        "SMS_ALLOW_ALL_USERS",
        "MATTERMOST_ALLOW_ALL_USERS",
        "MATRIX_ALLOW_ALL_USERS",
        "DINGTALK_ALLOW_ALL_USERS", "FEISHU_ALLOW_ALL_USERS", "WECOM_ALLOW_ALL_USERS",
        "QQ_ALLOW_ALL_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_event(platform: Platform, user_id: str, chat_id: str) -> MessageEvent:
    return MessageEvent(
        text="hello",
        message_id="m1",
        source=SessionSource(
            platform=platform,
            user_id=user_id,
            chat_id=chat_id,
            user_name="tester",
            chat_type="dm",
        ),
    )


def _make_runner(platform: Platform, config: GatewayConfig):
    from gateway.run import GatewayRunner
    from gateway.profile_routing import normalize_profile_routes_config

    runner = object.__new__(GatewayRunner)
    runner.config = config
    adapter = SimpleNamespace(
        send=AsyncMock(),
        send_chat_pairing_request=AsyncMock(return_value=1),
        send_dm_pairing_request=AsyncMock(return_value=1),
    )
    runner.adapters = {platform: adapter}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_store.is_chat_approved.return_value = False
    runner.pairing_store._is_rate_limited.return_value = False
    runner.pairing_store.get_pending_entry.return_value = {
        "subject_type": "chat",
    }
    runner._profile_route_config = lambda: normalize_profile_routes_config(None)
    # Attributes required by _handle_message for the authorized-user path
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._update_prompts = {}
    runner.session_store = SimpleNamespace()
    runner.hooks = SimpleNamespace(dispatch=AsyncMock(return_value=None))
    runner._sessions = {}
    return runner, adapter


def test_whatsapp_lid_user_matches_phone_allowlist_via_session_mapping(monkeypatch, tmp_path):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "15550000001")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    session_dir = tmp_path / "whatsapp" / "session"
    session_dir.mkdir(parents=True)
    (session_dir / "lid-mapping-15550000001.json").write_text('"900000000000001"', encoding="utf-8")
    (session_dir / "lid-mapping-900000000000001_reverse.json").write_text('"15550000001"', encoding="utf-8")

    runner, _adapter = _make_runner(
        Platform.WHATSAPP,
        GatewayConfig(platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)}),
    )

    source = SessionSource(
        platform=Platform.WHATSAPP,
        user_id="900000000000001@lid",
        chat_id="900000000000001@lid",
        user_name="tester",
        chat_type="dm",
    )

    assert runner._is_user_authorized(source) is True


def test_star_wildcard_in_allowlist_authorizes_any_user(monkeypatch):
    """WHATSAPP_ALLOWED_USERS=* should act as allow-all wildcard."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")

    runner, _adapter = _make_runner(
        Platform.WHATSAPP,
        GatewayConfig(platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)}),
    )

    source = SessionSource(
        platform=Platform.WHATSAPP,
        user_id="99998887776@s.whatsapp.net",
        chat_id="99998887776@s.whatsapp.net",
        user_name="stranger",
        chat_type="dm",
    )
    assert runner._is_user_authorized(source) is True


def test_star_wildcard_works_for_any_platform(monkeypatch):
    """The * wildcard should work generically, not just for WhatsApp."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "*")

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="123456789",
        chat_id="123456789",
        user_name="stranger",
        chat_type="dm",
    )
    assert runner._is_user_authorized(source) is True


def test_qq_group_allowlist_authorizes_group_chat_without_user_allowlist(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("QQ_GROUP_ALLOWED_USERS", "group-openid-1")

    runner, _adapter = _make_runner(
        Platform.QQBOT,
        GatewayConfig(platforms={Platform.QQBOT: PlatformConfig(enabled=True)}),
    )

    source = SessionSource(
        platform=Platform.QQBOT,
        user_id="member-openid-999",
        chat_id="group-openid-1",
        user_name="tester",
        chat_type="group",
    )

    assert runner._is_user_authorized(source) is True


def test_qq_group_allowlist_does_not_authorize_other_groups(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("QQ_GROUP_ALLOWED_USERS", "group-openid-1")

    runner, _adapter = _make_runner(
        Platform.QQBOT,
        GatewayConfig(platforms={Platform.QQBOT: PlatformConfig(enabled=True)}),
    )

    source = SessionSource(
        platform=Platform.QQBOT,
        user_id="member-openid-999",
        chat_id="group-openid-2",
        user_name="tester",
        chat_type="group",
    )

    assert runner._is_user_authorized(source) is False


def test_telegram_group_user_allowlist_authorizes_forum_sender_without_dm_allowlist(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_USERS", "999")

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="999",
        chat_id="-1001878443972",
        user_name="tester",
        chat_type="forum",
    )

    assert runner._is_user_authorized(source) is True


def test_telegram_group_user_allowlist_rejects_other_senders(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_USERS", "999")

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="123",
        chat_id="-1001878443972",
        user_name="tester",
        chat_type="group",
    )

    assert runner._is_user_authorized(source) is False


def test_telegram_group_user_allowlist_wildcard_authorizes_any_sender(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_USERS", "*")

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="123",
        chat_id="-1001878443972",
        user_name="tester",
        chat_type="group",
    )

    assert runner._is_user_authorized(source) is True


def test_telegram_group_user_allowlist_does_not_authorize_dms(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_USERS", "999")

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="999",
        chat_id="999",
        user_name="tester",
        chat_type="dm",
    )

    assert runner._is_user_authorized(source) is False


def test_telegram_group_chat_allowlist_authorizes_group_chat_without_user_allowlist(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-1001878443972")

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="999",
        chat_id="-1001878443972",
        user_name="tester",
        chat_type="forum",
    )

    assert runner._is_user_authorized(source) is True


def test_telegram_group_chat_allowlist_authorizes_anonymous_sender(monkeypatch):
    """TELEGRAM_GROUP_ALLOWED_CHATS must authorize chat traffic with no
    sender user_id (Telegram anonymous-admin posts, sender_chat). The
    docs state the chat allowlist authorizes "every member of that chat,
    regardless of sender" — anonymous senders had been silently dropped
    despite an explicit chat opt-in.
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-1001878443972")

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=None,
        chat_id="-1001878443972",
        user_name=None,
        chat_type="group",
    )

    assert runner._is_user_authorized(source) is True


def test_telegram_group_chat_allowlist_rejects_anonymous_sender_in_other_chat(monkeypatch):
    """Anonymous senders in a chat *not* on the allowlist must still be
    rejected — the early no-user-id path must not become an open gate.
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-1001878443972")

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=None,
        chat_id="-1009999999999",
        user_name=None,
        chat_type="group",
    )

    assert runner._is_user_authorized(source) is False


def test_telegram_observed_group_source_respects_config_chat_allowlist(monkeypatch):
    _clear_auth_env(monkeypatch)

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    enabled=True,
                    token="t",
                    extra={
                        "group_allowed_chats": "-1001878443972",
                        "observe_unmentioned_group_messages": True,
                    },
                )
            }
        ),
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=None,
        chat_id="-5274164515",
        user_name=None,
        chat_type="group",
    )

    assert runner._is_user_authorized(source) is False


def test_telegram_dm_pairing_does_not_authorize_unapproved_group(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-1001878443972")

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )
    runner.pairing_store.is_approved.return_value = True

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="367599252",
        chat_id="-5274164515",
        user_name="tester",
        chat_type="group",
    )

    assert runner._is_user_authorized(source) is False


def test_telegram_explicit_profile_route_authorizes_group(monkeypatch):
    from gateway.profile_routing import normalize_profile_routes_config

    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-1001878443972")

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )
    runner._profile_route_config = lambda: normalize_profile_routes_config(
        {
            "routes": [
                {
                    "id": "telegram-sila-treh",
                    "enabled": True,
                    "platform": "telegram",
                    "chat_id": "-4534774626",
                    "profile": "sila-treh",
                }
            ]
        }
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=None,
        chat_id="-4534774626",
        user_name=None,
        chat_type="group",
    )

    assert runner._is_user_authorized(source) is True


def test_telegram_group_pairing_request_detects_attributed_bot_start(monkeypatch):
    _clear_auth_env(monkeypatch)

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )
    event = MessageEvent(
        text="[Наталия|367599252] @TripiooBot /start",
        message_id="5763",
        raw_message=SimpleNamespace(
            text="@TripiooBot /start",
            caption=None,
            from_user=SimpleNamespace(id=367599252, full_name="Наталия"),
        ),
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id=None,
            chat_id="-5274164515",
            chat_name="Only me b2+",
            user_name=None,
            chat_type="group",
        ),
    )

    assert runner._is_group_pairing_start_event(event) is True
    assert runner._group_pairing_requester(event) == ("367599252", "Наталия")


@pytest.mark.asyncio
async def test_approved_telegram_group_start_reopens_existing_setup_request(monkeypatch):
    _clear_auth_env(monkeypatch)
    runner, adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )
    runner.pairing_store.is_chat_approved.return_value = True
    runner.pairing_store.generate_chat_request.return_value = "retry-entry"
    runner.pairing_store.get_pending_entry.return_value = {
        "subject_type": "chat",
        "owner_notified_at": 123.0,
    }
    event = MessageEvent(
        text="/start",
        message_id="5765",
        raw_message=SimpleNamespace(
            text="/start",
            caption=None,
            from_user=SimpleNamespace(id=179555559, full_name="Mikhail"),
        ),
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="179555559",
            chat_id="-5526305849",
            chat_name="Худеем с Трипио",
            user_name="Mikhail",
            chat_type="group",
        ),
    )

    result = await runner._handle_group_pairing_request(
        event,
        allow_reconfigure=True,
    )

    assert result is None
    runner.pairing_store.generate_chat_request.assert_called_once_with(
        "telegram",
        "-5526305849",
        "Худеем с Трипио",
        chat_type="group",
        thread_id="",
        requester_user_id="179555559",
        requester_user_name="Mikhail",
    )
    adapter.send_chat_pairing_request.assert_not_awaited()
    adapter.send.assert_awaited_once()
    assert "уже отправлен" in adapter.send.await_args.args[1]


@pytest.mark.asyncio
async def test_unauthorized_telegram_group_mention_creates_chat_pairing_request(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-1001878443972")

    runner, adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )
    adapter._bot = SimpleNamespace(username="TripiooBot")
    runner.pairing_store.generate_chat_request.return_value = "pairing-telegram--5274164515-chat"

    event = MessageEvent(
        text="[Наталия|367599252] @TripiooBot",
        message_id="5764",
        raw_message=SimpleNamespace(
            text="@TripiooBot",
            caption=None,
            from_user=SimpleNamespace(id=367599252, full_name="Наталия"),
        ),
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id=None,
            chat_id="-5274164515",
            chat_name="Only me b2+",
            user_name=None,
            chat_type="group",
        ),
    )

    result = await runner._handle_message(event)

    assert result is None
    runner.pairing_store.generate_chat_request.assert_called_once_with(
        "telegram",
        "-5274164515",
        "Only me b2+",
        chat_type="group",
        thread_id="",
        requester_user_id="367599252",
        requester_user_name="Наталия",
    )
    adapter.send.assert_awaited_once()
    assert "личный чат" in adapter.send.await_args.args[1]
    adapter.send_chat_pairing_request.assert_awaited_once_with(
        entry_id="pairing-telegram--5274164515-chat",
        chat_id="-5274164515",
        chat_name="Only me b2+",
        chat_type="group",
        thread_id="",
        requester_user_id="367599252",
        requester_user_name="Наталия",
    )
    runner.pairing_store.mark_owner_notified.assert_called_once_with(
        "telegram",
        "pairing-telegram--5274164515-chat",
    )


@pytest.mark.asyncio
async def test_handle_message_does_not_drop_anonymous_sender_in_allowlisted_chat(monkeypatch):
    """End-to-end: a group message with from_user=None in an allowlisted
    chat must reach the dispatch path — not get silently dropped by the
    no-user-id guard, and not trigger pairing (anonymous senders can't
    be paired anyway).
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-1001878443972")

    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")},
    )
    runner, adapter = _make_runner(Platform.TELEGRAM, config)

    # Force _handle_message to bail with a sentinel right after the
    # auth gate, so a successful "auth passed" call can be distinguished
    # from the buggy "silently dropped" case (which would return None
    # before this hook ever runs).
    reached_dispatch = MagicMock(side_effect=RuntimeError("reached dispatch"))
    runner._session_key_for_source = reached_dispatch

    event = MessageEvent(
        text="hi",
        message_id="m1",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id=None,
            chat_id="-1001878443972",
            user_name=None,
            chat_type="group",
        ),
    )

    with pytest.raises(RuntimeError, match="reached dispatch"):
        await runner._handle_message(event)

    reached_dispatch.assert_called_once()
    runner.pairing_store.generate_code.assert_not_called()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_message_drops_anonymous_sender_outside_allowlist(monkeypatch):
    """Anonymous senders in a chat *not* on the allowlist remain silently
    dropped — the fix must not become a backdoor for unauthorized chats.
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-1001878443972")

    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")},
    )
    runner, adapter = _make_runner(Platform.TELEGRAM, config)

    must_not_run = MagicMock(side_effect=AssertionError("auth gate did not drop"))
    runner._session_key_for_source = must_not_run

    event = MessageEvent(
        text="hi",
        message_id="m1",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id=None,
            chat_id="-1009999999999",
            user_name=None,
            chat_type="group",
        ),
    )

    result = await runner._handle_message(event)

    assert result is None
    must_not_run.assert_not_called()
    runner.pairing_store.generate_code.assert_not_called()
    adapter.send.assert_not_awaited()


def test_telegram_group_users_legacy_chat_ids_still_authorize(monkeypatch):
    """Backward-compat: PR #15027 shipped TELEGRAM_GROUP_ALLOWED_USERS as a
    chat-ID allowlist. PR #17686 renamed it to sender IDs and added
    TELEGRAM_GROUP_ALLOWED_CHATS. Users on the old guidance must keep working:
    chat-ID-shaped values (starting with "-") in the _USERS var are honored as
    chat IDs with a deprecation warning.
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_USERS", "-1001878443972")

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="999",
        chat_id="-1001878443972",
        user_name="tester",
        chat_type="forum",
    )

    assert runner._is_user_authorized(source) is True


def test_telegram_group_users_legacy_does_not_cross_chats(monkeypatch):
    """Legacy chat-ID value only authorizes the listed chat, not any group."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_USERS", "-1001878443972")

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="999",
        chat_id="-1009999999999",
        user_name="tester",
        chat_type="group",
    )

    assert runner._is_user_authorized(source) is False


def test_telegram_group_users_mixed_sender_and_legacy_chat(monkeypatch):
    """Mixed values: positive user ID gates senders; negative chat ID gates chat."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_USERS", "999,-1001878443972")

    runner, _adapter = _make_runner(
        Platform.TELEGRAM,
        GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")}),
    )

    # Legacy chat ID path: any sender in the listed chat is authorized
    legacy_chat_source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="123",
        chat_id="-1001878443972",
        user_name="tester",
        chat_type="group",
    )
    assert runner._is_user_authorized(legacy_chat_source) is True

    # Sender path: listed sender user ID authorized in any group
    sender_source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="999",
        chat_id="-1009999999999",
        user_name="tester",
        chat_type="group",
    )
    assert runner._is_user_authorized(sender_source) is True


@pytest.mark.asyncio
async def test_unauthorized_dm_pairs_by_default(monkeypatch):
    _clear_auth_env(monkeypatch)
    config = GatewayConfig(
        platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)},
    )
    runner, adapter = _make_runner(Platform.WHATSAPP, config)
    runner.pairing_store.generate_code.return_value = "ABC12DEF"

    result = await runner._handle_message(
        _make_event(
            Platform.WHATSAPP,
            "15551234567@s.whatsapp.net",
            "15551234567@s.whatsapp.net",
        )
    )

    assert result is None
    runner.pairing_store.generate_code.assert_called_once_with(
        "whatsapp",
        "15551234567@s.whatsapp.net",
        "tester",
    )
    adapter.send.assert_awaited_once()
    assert "ABC12DEF" in adapter.send.await_args.args[1]
    adapter.send_dm_pairing_request.assert_awaited_once_with(
        user_id="15551234567@s.whatsapp.net",
        user_name="tester",
        code="ABC12DEF",
    )


@pytest.mark.asyncio
async def test_telegram_dm_pairing_notifies_configured_admin(monkeypatch):
    from gateway.platforms.telegram import TelegramAdapter

    _clear_auth_env(monkeypatch)
    config = PlatformConfig(
        enabled=True,
        token="test-token",
        extra={"allow_admin_from": ["179555559"]},
    )
    adapter = TelegramAdapter(config)
    adapter._bot = SimpleNamespace(send_message=AsyncMock())

    delivered = await adapter.send_dm_pairing_request(
        user_id="5103932194",
        user_name="Akito",
        code="TESTCODE",
    )

    assert delivered == 1
    sent = adapter._bot.send_message.await_args.kwargs
    assert sent["chat_id"] == 179555559
    assert "Akito" in sent["text"]
    assert "5103932194" in sent["text"]
    assert "TESTCODE" in sent["text"]
    assert "не одобрен автоматически" in sent["text"]


@pytest.mark.asyncio
async def test_group_pairing_is_profile_owned_but_notified_by_router_adapter(
    monkeypatch, tmp_path
):
    """One Telegram poller must not force routed pairing into default home."""
    from gateway.pairing import PairingStore
    from gateway.profile_routing import normalize_profile_routes_config
    from gateway.run import GatewayRunner

    root = tmp_path / ".hermes"
    profile_home = root / "profiles" / "hudeem-tripio"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr("gateway.run.get_default_hermes_root", lambda: root)

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.pairing_store = PairingStore(hermes_home=root)
    runner._profile_pairing_stores = {"default": runner.pairing_store}
    runner._profile_route_config = lambda: normalize_profile_routes_config({
        "routes": [{
            "platform": "telegram",
            "chat_id": "-5526305849",
            "profile": "hudeem-tripio",
        }]
    })
    adapter = SimpleNamespace(
        send=AsyncMock(),
        send_chat_pairing_request=AsyncMock(return_value=1),
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    event = MessageEvent(
        text="/start",
        message_id="m-profile-pairing",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="5103932194",
            chat_id="-5526305849",
            user_name="Akito",
            chat_name="Худеем с Трипио",
            chat_type="group",
        ),
    )

    await runner._handle_group_pairing_request(event, allow_reconfigure=True)

    assert runner.pairing_store.list_pending("telegram") == []
    profile_store = PairingStore(hermes_home=profile_home)
    pending = profile_store.list_pending("telegram")
    assert len(pending) == 1
    assert pending[0]["chat_id"] == "-5526305849"
    assert pending[0]["requester_user_id"] == "5103932194"
    adapter.send_chat_pairing_request.assert_awaited_once()
    assert profile_store.get_pending_entry(
        "telegram", pending[0]["entry_id"]
    )["owner_notified_at"] > 0


@pytest.mark.asyncio
async def test_unauthorized_whatsapp_dm_can_be_ignored(monkeypatch):
    _clear_auth_env(monkeypatch)
    config = GatewayConfig(
        platforms={
            Platform.WHATSAPP: PlatformConfig(
                enabled=True,
                extra={"unauthorized_dm_behavior": "ignore"},
            ),
        },
    )
    runner, adapter = _make_runner(Platform.WHATSAPP, config)

    result = await runner._handle_message(
        _make_event(
            Platform.WHATSAPP,
            "15551234567@s.whatsapp.net",
            "15551234567@s.whatsapp.net",
        )
    )

    assert result is None
    runner.pairing_store.generate_code.assert_not_called()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_rate_limited_user_gets_no_response(monkeypatch):
    """When a user is already rate-limited, pairing messages are silently ignored."""
    _clear_auth_env(monkeypatch)
    config = GatewayConfig(
        platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)},
    )
    runner, adapter = _make_runner(Platform.WHATSAPP, config)
    runner.pairing_store._is_rate_limited.return_value = True

    result = await runner._handle_message(
        _make_event(
            Platform.WHATSAPP,
            "15551234567@s.whatsapp.net",
            "15551234567@s.whatsapp.net",
        )
    )

    assert result is None
    runner.pairing_store.generate_code.assert_not_called()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejection_message_records_rate_limit(monkeypatch):
    """After sending a 'too many requests' rejection, rate limit is recorded
    so subsequent messages are silently ignored."""
    _clear_auth_env(monkeypatch)
    config = GatewayConfig(
        platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)},
    )
    runner, adapter = _make_runner(Platform.WHATSAPP, config)
    runner.pairing_store.generate_code.return_value = None  # triggers rejection

    result = await runner._handle_message(
        _make_event(
            Platform.WHATSAPP,
            "15551234567@s.whatsapp.net",
            "15551234567@s.whatsapp.net",
        )
    )

    assert result is None
    adapter.send.assert_awaited_once()
    assert "Too many" in adapter.send.await_args.args[1]
    runner.pairing_store._record_rate_limit.assert_called_once_with(
        "whatsapp", "15551234567@s.whatsapp.net"
    )


@pytest.mark.asyncio
async def test_global_ignore_suppresses_pairing_reply(monkeypatch):
    _clear_auth_env(monkeypatch)
    config = GatewayConfig(
        unauthorized_dm_behavior="ignore",
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
    )
    runner, adapter = _make_runner(Platform.TELEGRAM, config)

    result = await runner._handle_message(
        _make_event(
            Platform.TELEGRAM,
            "12345",
            "12345",
        )
    )

    assert result is None
    runner.pairing_store.generate_code.assert_not_called()
    adapter.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# Allowlist-configured platforms default to "ignore" for unauthorized users
# (#9337: Signal gateway sends pairing spam when allowlist is configured)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signal_with_allowlist_ignores_unauthorized_dm(monkeypatch):
    """When SIGNAL_ALLOWED_USERS is set, unauthorized DMs are silently dropped.

    This is the primary regression test for #9337: before the fix, Signal
    would send pairing codes to ANY sender even when a strict allowlist was
    configured, spamming personal contacts with cryptic bot messages.
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("SIGNAL_ALLOWED_USERS", "+15550000001")  # allowlist set

    config = GatewayConfig(
        platforms={Platform.SIGNAL: PlatformConfig(enabled=True)},
    )
    runner, adapter = _make_runner(Platform.SIGNAL, config)

    result = await runner._handle_message(
        _make_event(Platform.SIGNAL, "+15559999999", "+15559999999")  # not in allowlist
    )

    assert result is None
    runner.pairing_store.generate_code.assert_not_called()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_with_allowlist_ignores_unauthorized_dm(monkeypatch):
    """Same behavior for Telegram: allowlist ⟹ ignore unauthorized DMs."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111111111")

    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True)},
    )
    runner, adapter = _make_runner(Platform.TELEGRAM, config)

    result = await runner._handle_message(
        _make_event(Platform.TELEGRAM, "999999999", "999999999")
    )

    assert result is None
    runner.pairing_store.generate_code.assert_not_called()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_allowlist_ignores_unauthorized_dm(monkeypatch):
    """GATEWAY_ALLOWED_USERS also triggers the 'ignore' behavior."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "111111111")

    config = GatewayConfig(
        platforms={Platform.SIGNAL: PlatformConfig(enabled=True)},
    )
    runner, adapter = _make_runner(Platform.SIGNAL, config)

    result = await runner._handle_message(
        _make_event(Platform.SIGNAL, "+15559999999", "+15559999999")
    )

    assert result is None
    runner.pairing_store.generate_code.assert_not_called()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_allowlist_still_pairs_by_default(monkeypatch):
    """Without any allowlist, pairing behavior is preserved (open gateway)."""
    _clear_auth_env(monkeypatch)
    # No SIGNAL_ALLOWED_USERS, no GATEWAY_ALLOWED_USERS

    config = GatewayConfig(
        platforms={Platform.SIGNAL: PlatformConfig(enabled=True)},
    )
    runner, adapter = _make_runner(Platform.SIGNAL, config)
    runner.pairing_store.generate_code.return_value = "PAIR1234"

    result = await runner._handle_message(
        _make_event(Platform.SIGNAL, "+15559999999", "+15559999999")
    )

    assert result is None
    runner.pairing_store.generate_code.assert_called_once()
    adapter.send.assert_awaited_once()
    assert "PAIR1234" in adapter.send.await_args.args[1]


def test_explicit_pair_config_overrides_allowlist_default(monkeypatch):
    """Explicit unauthorized_dm_behavior='pair' overrides the allowlist default.

    Operators can opt back in to pairing even with an allowlist by setting
    unauthorized_dm_behavior: pair in their platform config.  We test the
    _get_unauthorized_dm_behavior resolver directly to avoid the full
    _handle_message pipeline which requires extensive runner state.
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("SIGNAL_ALLOWED_USERS", "+15550000001")

    config = GatewayConfig(
        platforms={
            Platform.SIGNAL: PlatformConfig(
                enabled=True,
                extra={"unauthorized_dm_behavior": "pair"},  # explicit override
            ),
        },
    )
    runner, _adapter = _make_runner(Platform.SIGNAL, config)

    # The per-platform explicit config should beat the allowlist-derived default
    behavior = runner._get_unauthorized_dm_behavior(Platform.SIGNAL)
    assert behavior == "pair"


def test_allowlist_authorized_user_returns_ignore_for_unauthorized(monkeypatch):
    """_get_unauthorized_dm_behavior returns 'ignore' when allowlist is set.

    We test the resolver directly.  The full _handle_message path for
    authorized users is covered by the integration tests in this module.
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("SIGNAL_ALLOWED_USERS", "+15550000001")

    config = GatewayConfig(
        platforms={Platform.SIGNAL: PlatformConfig(enabled=True)},
    )
    runner, _adapter = _make_runner(Platform.SIGNAL, config)

    behavior = runner._get_unauthorized_dm_behavior(Platform.SIGNAL)
    assert behavior == "ignore"


def test_get_unauthorized_dm_behavior_no_allowlist_returns_pair(monkeypatch):
    """Without any allowlist, 'pair' is still the default."""
    _clear_auth_env(monkeypatch)

    config = GatewayConfig(
        platforms={Platform.SIGNAL: PlatformConfig(enabled=True)},
    )
    runner, _adapter = _make_runner(Platform.SIGNAL, config)

    behavior = runner._get_unauthorized_dm_behavior(Platform.SIGNAL)
    assert behavior == "pair"


def test_qqbot_with_allowlist_ignores_unauthorized_dm(monkeypatch):
    """QQBOT is included in the allowlist-aware default (QQ_ALLOWED_USERS).

    Regression guard: the initial #9337 fix omitted QQBOT from the env map
    inside _get_unauthorized_dm_behavior, even though _is_user_authorized
    mapped it to QQ_ALLOWED_USERS.  Without QQBOT here, a QQ operator with a
    strict user allowlist would still get pairing codes sent to strangers.
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("QQ_ALLOWED_USERS", "allowed-openid-1")

    config = GatewayConfig(
        platforms={Platform.QQBOT: PlatformConfig(enabled=True)},
    )
    runner, _adapter = _make_runner(Platform.QQBOT, config)

    behavior = runner._get_unauthorized_dm_behavior(Platform.QQBOT)
    assert behavior == "ignore"
