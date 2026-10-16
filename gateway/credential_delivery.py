"""Encrypted Telegram credential handoff and restart-safe message expiry.

Account values reach only the final native spoiler renderer. Model history,
durable job metadata and the deletion queue never receive plaintext accounts.
"""
import asyncio
from contextlib import contextmanager
import html
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from cryptography.fernet import Fernet, InvalidToken

TTL_SECONDS = 15 * 60
logger = logging.getLogger(__name__)


def _cipher():
    from tools.credential_vault_tool import vault_root
    return Fernet((vault_root() / 'key').read_bytes())


class CredentialReplyCollector:
    def __init__(self, chat_id, thread_id, bot_id):
        self.chat_id, self.thread_id, self.bot_id = str(chat_id), str(thread_id or ''), str(bot_id or '')
        self._lock = threading.Lock()
        self._tokens = []

    def record(self, account):
        if not self.bot_id or not all(isinstance(account.get(k), str) for k in ('label', 'login', 'password')):
            raise ValueError('secure_delivery_unavailable')
        payload = dict(account=account, id=uuid.uuid4().hex, chat_id=self.chat_id,
                       thread_id=self.thread_id, bot_id=self.bot_id)
        token = _cipher().encrypt(json.dumps(payload).encode()).decode()
        with self._lock:
            self._tokens.append(token)

    def snapshot(self):
        with self._lock:
            return list(self._tokens)


class CredentialExpiryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS credential_expiry (
                id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                message_id TEXT NOT NULL, due_at REAL NOT NULL, shown INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0)''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, delivery_id):
        with self._connect() as db:
            row = db.execute('SELECT * FROM credential_expiry WHERE id=?', (delivery_id,)).fetchone()
            return dict(row) if row else None

    def register(self, payload, message_id, *, due_at=None):
        with self._connect() as db:
            db.execute('INSERT INTO credential_expiry(id,bot_id,chat_id,message_id,due_at) VALUES (?,?,?,?,?)',
                (payload['id'], payload['bot_id'], payload['chat_id'], str(message_id),
                 time.time() + TTL_SECONDS if due_at is None else due_at))

    def mark_shown(self, delivery_id):
        with self._connect() as db:
            db.execute('UPDATE credential_expiry SET shown=1 WHERE id=?', (delivery_id,))

    def due(self, bot_id):
        now = time.time()
        with self._connect() as db:
            return [dict(row) for row in db.execute('''SELECT * FROM credential_expiry
                WHERE bot_id=? AND deleted=0 AND due_at<=? AND retry_at<=? LIMIT 100''', (str(bot_id), now, now))]

    def deleted(self, delivery_id):
        with self._connect() as db:
            db.execute('UPDATE credential_expiry SET deleted=1 WHERE id=?', (delivery_id,))

    def retry(self, delivery_id, seconds=5):
        with self._connect() as db:
            db.execute('UPDATE credential_expiry SET retry_at=? WHERE id=?', (time.time() + seconds, delivery_id))


def expiry_store(adapter):
    store = getattr(adapter, '_credential_expiry_store', None)
    if store is None:
        from hermes_constants import get_hermes_home
        store = CredentialExpiryStore(get_hermes_home() / 'credential-message-expiry.sqlite3')
        adapter._credential_expiry_store = store
    return store


def render_account(account):
    return ('🔐 <b>' + html.escape(account['label']) + '</b>\n'
            + 'Логин: ' + html.escape(account['login']) + '\n'
            + 'Пароль: <tg-spoiler>' + html.escape(account['password']) + '</tg-spoiler>\n\n'
            + 'Сообщение будет удалено через 15 минут.')


async def deliver_credentials(adapter, chat_id, tokens, metadata=None, reply_to=None):
    from gateway.platforms.base import SendResult
    lock = getattr(adapter, '_credential_send_lock', None)
    if lock is None:
        lock = adapter._credential_send_lock = asyncio.Lock()
    async with lock:
        ids = []
        try:
            if not isinstance(tokens, list) or not tokens:
                raise ValueError('invalid_credential_delivery')
            # Validate the entire batch before showing any credentials.
            cipher = _cipher()
            payloads = [json.loads(cipher.decrypt(token.encode())) for token in tokens]
            thread = str((metadata or {}).get('thread_id') or '')
            bot_id = str(getattr(adapter._bot, 'id', '') or '')
            for payload in payloads:
                if (payload['chat_id'] != str(chat_id) or payload['thread_id'] != thread
                        or payload['bot_id'] != bot_id or not bot_id):
                    raise ValueError('credential_destination_mismatch')
                text = render_account(payload['account'])
                if len(text.encode('utf-16-le')) // 2 > 4096:
                    raise ValueError('credential_message_too_long')
            try:
                for token in tokens:
                    cipher.decrypt(token.encode(), ttl=TTL_SECONDS)
            except InvalidToken:
                # End delayed delivery cleanly; never retry an expired secret.
                clean_metadata = {k: v for k, v in (metadata or {}).items() if k != 'credential_deliveries'}
                return await adapter.send(chat_id, 'Срок показа доступа истёк. Запросите его ещё раз.',
                                          reply_to=reply_to, metadata=clean_metadata)
            store = expiry_store(adapter)
            for payload in payloads:
                record = store.get(payload['id'])
                if record and (record['deleted'] or record['due_at'] <= time.time()):
                    continue
                if record is None:
                    kwargs = dict(chat_id=int(chat_id), text='🔐 Подготовка доступа…')
                    requested_thread = adapter._message_thread_id_for_send(thread)
                    if requested_thread:
                        kwargs['message_thread_id'] = int(requested_thread)
                    anchor = reply_to or (metadata or {}).get('telegram_reply_to_message_id')
                    if anchor:
                        kwargs.update(reply_to_message_id=int(anchor), allow_sending_without_reply=True)
                    message = await adapter._bot.send_message(**kwargs)
                    # Persist the deletion deadline BEFORE revealing a secret.
                    store.register(payload, message.message_id)
                    record = store.get(payload['id'])
                if not record['shown']:
                    try:
                        await adapter._bot.edit_message_text(chat_id=int(chat_id),
                            message_id=int(record['message_id']), text=render_account(payload['account']), parse_mode='HTML')
                    except Exception as exc:
                        if type(exc).__name__ != 'BadRequest' or 'message is not modified' not in str(exc).lower():
                            raise
                    store.mark_shown(payload['id'])
                ids.append(record['message_id'])
            if not ids:
                return SendResult(success=True, raw_response={'credential_delivery_expired': True})
            return SendResult(success=True, message_id=ids[-1], continuation_message_ids=tuple(ids[:-1]), raw_response={'message_ids': ids})
        except InvalidToken:
            # A delayed or replayed result cannot resurrect an expired secret.
            return SendResult(success=False, error='credential_delivery_expired')
        except Exception as exc:
            logger.warning('Credential message delivery failed (%s)', type(exc).__name__)
            retryable = isinstance(exc, (OSError, TimeoutError)) or type(exc).__name__ in {'NetworkError', 'TimedOut', 'RetryAfter'}
            return SendResult(success=False, error='credential_delivery_failed', retryable=retryable)


async def expire_credentials(adapter):
    if not adapter._bot:
        return
    store = expiry_store(adapter)
    for row in store.due(adapter._bot.id):
        try:
            await adapter._bot.delete_message(chat_id=int(row['chat_id']), message_id=int(row['message_id']))
            store.deleted(row['id'])
            logger.info('Credential message expired: message=%s', row['message_id'])
        except Exception as exc:
            if type(exc).__name__ == 'BadRequest' and 'message to delete not found' in str(exc).lower():
                store.deleted(row['id'])
            else:
                store.retry(row['id'])
                logger.warning('Credential message deletion will retry (%s)', type(exc).__name__)


async def credential_expiry_loop(adapter):
    logger.info('Credential message expiry worker started')
    while adapter._bot:
        try:
            await expire_credentials(adapter)
        except Exception as exc:
            logger.warning('Credential expiry queue unavailable (%s)', type(exc).__name__)
        await asyncio.sleep(1)
