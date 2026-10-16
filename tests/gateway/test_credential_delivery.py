import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import pytest

from gateway import credential_delivery as delivery
from gateway.config import PlatformConfig
from gateway.platforms.telegram import TelegramAdapter


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'key').write_bytes(Fernet.generate_key())
    monkeypatch.setattr('tools.credential_vault_tool.vault_root', lambda: root)
    now = [delivery.time.time()]
    monkeypatch.setattr(delivery.time, 'time', lambda: now[0])
    path = tmp_path / 'expiry.sqlite3'
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic-token'))
    adapter._credential_expiry_store = delivery.CredentialExpiryStore(path)
    adapter._bot = NS(id=123, send_message=AsyncMock(return_value=NS(message_id=42)),
                      edit_message_text=AsyncMock(), delete_message=AsyncMock())
    account = dict(label='Example <teacher>', login='demo@example.test', password='synth<>&||\n🔑secret')
    collector = delivery.CredentialReplyCollector('-12345', '7', '123')
    collector.record(account)
    metadata = dict(thread_id='7', telegram_reply_to_message_id='21', credential_deliveries=collector.snapshot())
    return NS(adapter=adapter, account=account, metadata=metadata, now=now, path=path, collector=collector)


@pytest.mark.asyncio
async def test_native_spoiler_is_persisted_before_reveal_and_never_plaintext_fallback(setup):
    item = setup
    async def edit(**kwargs):
        record = item.adapter._credential_expiry_store.get(json.loads(delivery._cipher().decrypt(item.collector.snapshot()[0]))['id'])
        assert record and record['message_id'] == '42'
        assert record['due_at'] == item.now[0] + 900
        assert kwargs['parse_mode'] == 'HTML'
        assert '<tg-spoiler>synth&lt;&gt;&amp;||\n🔑secret</tg-spoiler>' in kwargs['text']
    item.adapter._bot.edit_message_text.side_effect = edit
    result = await item.adapter.send('-12345', 'safe model receipt', metadata=item.metadata)
    assert result.success and result.message_id == '42'
    sent = item.adapter._bot.send_message.call_args.kwargs
    assert sent['message_thread_id'] == 7 and sent['reply_to_message_id'] == 21
    assert item.account['password'] not in sent['text']
    assert item.account['password'] not in json.dumps(item.metadata, ensure_ascii=False)
    assert item.account['password'].encode() not in item.path.read_bytes()
    assert item.account['login'].encode() not in item.path.read_bytes()


@pytest.mark.asyncio
async def test_delete_at_15_minutes_survives_restart_and_does_not_resend(setup):
    item = setup
    assert (await item.adapter.send('-12345', 'receipt', metadata=item.metadata)).success
    again = await item.adapter.send('-12345', 'receipt', metadata=item.metadata)
    assert again.success and again.message_id == '42'
    assert item.adapter._bot.send_message.call_count == 1
    assert item.adapter._bot.edit_message_text.call_count == 1
    item.adapter._credential_expiry_store = delivery.CredentialExpiryStore(item.path)
    item.now[0] += 899
    await delivery.expire_credentials(item.adapter)
    item.adapter._bot.delete_message.assert_not_called()
    item.now[0] += 1
    await delivery.expire_credentials(item.adapter)
    item.adapter._bot.delete_message.assert_awaited_once_with(chat_id=-12345, message_id=42)
    await delivery.expire_credentials(item.adapter)
    assert item.adapter._bot.delete_message.call_count == 1


@pytest.mark.asyncio
async def test_deletion_retries_network_failures_after_restart(setup):
    item = setup
    await item.adapter.send('-12345', 'receipt', metadata=item.metadata)
    item.now[0] += 900
    item.adapter._bot.delete_message.side_effect = [OSError('synthetic network outage'), True]
    await delivery.expire_credentials(item.adapter)
    item.adapter._credential_expiry_store = delivery.CredentialExpiryStore(item.path)
    item.now[0] += 5
    await delivery.expire_credentials(item.adapter)
    assert item.adapter._bot.delete_message.call_count == 2
    await delivery.expire_credentials(item.adapter)
    assert item.adapter._bot.delete_message.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['chat', 'thread', 'bot', 'token'])
async def test_other_destination_or_tampered_token_never_discloses(setup, change):
    item = setup
    chat = '-12345'
    if change == 'chat': chat = '-99999'
    if change == 'thread': item.metadata['thread_id'] = '8'
    if change == 'bot': item.adapter._bot.id = 999
    if change == 'token': item.metadata['credential_deliveries'] = ['tampered']
    result = await item.adapter.send(chat, 'receipt', metadata=item.metadata)
    assert not result.success
    item.adapter._bot.send_message.assert_not_called()
    item.adapter._bot.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_storage_or_format_errors_cannot_reveal_untracked_plaintext(setup, monkeypatch):
    item = setup
    monkeypatch.setattr(item.adapter._credential_expiry_store, 'register', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    result = await item.adapter.send('-12345', 'receipt', metadata=item.metadata)
    assert not result.success
    item.adapter._bot.edit_message_text.assert_not_called()
    assert item.account['password'] not in item.adapter._bot.send_message.call_args.kwargs['text']


@pytest.mark.asyncio
async def test_parse_failure_never_sends_password_as_plain_text(setup):
    item = setup
    item.adapter._bot.edit_message_text.side_effect = ValueError('synthetic parse failure')
    result = await item.adapter.send('-12345', 'receipt', metadata=item.metadata)
    assert not result.success
    assert item.adapter._bot.send_message.call_count == 1
    assert item.account['password'] not in item.adapter._bot.send_message.call_args.kwargs['text']
    item.now[0] += 900
    await delivery.expire_credentials(item.adapter)
    item.adapter._bot.delete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_envelope_never_reposts_a_secret(setup):
    item = setup
    item.now[0] += 901
    result = await item.adapter.send('-12345', 'receipt', metadata=item.metadata)
    assert result.success
    assert item.account['password'] not in item.adapter._bot.send_message.call_args.kwargs['text']
    item.adapter._bot.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_encrypted_metadata_survives_durable_redelivery(setup, tmp_path):
    from gateway.durable_jobs import DurableJobStore
    from gateway.platforms.base import MessageEvent, SendResult, SessionSource, Platform
    from tests.gateway.test_durable_jobs import _new_job
    from tests.gateway.restart_test_helpers import make_restart_runner
    store = DurableJobStore(tmp_path / 'jobs.sqlite3')
    job, _ = _new_job(store)
    runner, adapter = make_restart_runner()
    runner._durable_job_store = store
    event = MessageEvent(text='Show account', source=SessionSource(platform=Platform.TELEGRAM, chat_id='job-chat'))
    event.durable_job_id = job['job_id']
    event.outbound_metadata.update(setup.metadata)
    assert runner._complete_durable_job(event, 'Credentials ready')
    runner._durable_job_store = DurableJobStore(store.path)
    adapter._send_with_retry = AsyncMock(return_value=SendResult(success=True, message_id='42'))
    assert await runner._redeliver_durable_job(runner._durable_job_store.get(job['job_id']))
    assert adapter._send_with_retry.call_args.kwargs['metadata']['credential_deliveries'] == setup.collector.snapshot()
    assert setup.account['password'].encode() not in store.path.read_bytes()
