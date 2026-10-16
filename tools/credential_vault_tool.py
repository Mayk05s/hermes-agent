"""Encrypted, request-only account storage outside prompt-injected memory."""
import hashlib
import fcntl
import json
import os
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet
from gateway.memory_access import MemoryAccessContext
from tools.registry import registry


def vault_root():
    # Shared across profiles; ownership comes from verified identity, not profile.
    return Path.home() / '.config' / 'hermes' / 'credential-vault'


def invoke_credential_vault(agent, args):
    """Keep Telegram plaintext out of model input, progress and transcripts."""
    result = credential_vault(args, context=getattr(agent, '_memory_access_context', None))
    if getattr(agent, 'platform', None) != 'telegram' or args.get('action') != 'get':
        return result
    data = json.loads(result)
    if data.get('success') is not True or 'account' not in data:
        return result
    callback = getattr(agent, '_credential_delivery_callback', None)
    try:
        if not callable(callback):
            raise ValueError('secure_delivery_unavailable')
        callback(data['account'])
    except Exception:
        return json.dumps({'success': False, 'error': 'secure_delivery_unavailable'})
    return json.dumps({'success': True, 'label': data['account']['label'],
        'delivery': 'telegram_native_spoiler', 'delete_after_seconds': 900,
        'message': 'Credentials are queued for secure Telegram delivery. Do not print or speak account values. The transport shows the password under a spoiler and deletes the message after 15 minutes.'}, ensure_ascii=False)


def credential_vault(args, *, context=None):
    if (type(context) is not MemoryAccessContext or not context.source_ref
            or not context.actor_ref or context.actor_ref == 'unknown'
            or not context.profile_name or not context.destination.chat_id
            or context.destination.kind not in {'group', 'dm'}
            or (context.destination.kind == 'dm' and
                (not context.principal_id or not context.write_wing))):
        return json.dumps({'success': False, 'error': 'verified_user_required'})
    action = args.get('action')
    label = args.get('label', '').strip()
    if action not in {'save', 'get', 'list', 'delete'} or (action != 'list' and not label):
        return json.dumps({'success': False, 'error': 'invalid_request'})
    if len(label) > 200:
        return json.dumps({'success': False, 'error': 'label_too_long'})
    dest = context.destination
    # Group secrets belong to the profile's chat, shared by its authenticated
    # participants across all topics. DMs retain the private principal boundary.
    boundary_parts = (['credential-vault-group-v2', context.profile_name, dest.platform, dest.chat_id]
                      if dest.kind == 'group' else
                      [context.principal_id, dest.kind, dest.platform, dest.chat_id, dest.thread_id])
    boundary = json.dumps(boundary_parts, separators=(',', ':'))
    owner = hashlib.sha256(boundary.encode()).hexdigest()
    try:
        root = vault_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        key_path = root / 'key'
        # Serialize initial key creation so concurrent readers never see an empty key.
        lock_fd = os.open(root / 'key.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, 'rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not key_path.exists():
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as key_file:
                    key_file.write(Fernet.generate_key())
                    key_file.flush()
                    os.fsync(key_file.fileno())
            key_path.chmod(0o600)
            cipher = Fernet(key_path.read_bytes())
        db_path = root / 'vault.sqlite3'
        fd = os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        db_path.chmod(0o600)
        db = sqlite3.connect(db_path, timeout=15)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS accounts (owner TEXT, name TEXT, payload BLOB, PRIMARY KEY(owner,name))')
            name = hashlib.sha256(label.casefold().encode()).hexdigest()
            if action == 'save':
                login, password = args.get('login'), args.get('password')
                if not isinstance(login, str) or not isinstance(password, str) or not login or not password:
                    return json.dumps({'success': False, 'error': 'login_and_password_required'})
                payload = {'label': label, 'login': login, 'password': password}
                db.execute('INSERT OR REPLACE INTO accounts VALUES (?,?,?)',
                           (owner, name, cipher.encrypt(json.dumps(payload).encode())))
                db.commit()
                # Read back encrypted data before confirming success. Never echo password.
                row = db.execute('SELECT payload FROM accounts WHERE owner=? AND name=?', (owner, name)).fetchone()
                assert json.loads(cipher.decrypt(row[0])) == payload
                result = {'success': True, 'saved': True, 'label': label}
            elif action == 'list':
                rows = db.execute('SELECT payload FROM accounts WHERE owner=?', (owner,)).fetchall()
                result = {'success': True, 'labels': [json.loads(cipher.decrypt(r[0]))['label'] for r in rows]}
            elif action == 'delete':
                cursor = db.execute('DELETE FROM accounts WHERE owner=? AND name=?', (owner, name))
                db.commit()
                result = {'success': True, 'deleted': cursor.rowcount == 1}
            else:
                row = db.execute('SELECT payload FROM accounts WHERE owner=? AND name=?', (owner, name)).fetchone()
                result = ({'success': True, 'account': json.loads(cipher.decrypt(row[0]))}
                          if row else {'success': False, 'error': 'not_found'})
            return json.dumps(result, ensure_ascii=False)
        finally:
            db.close()
    except Exception:
        # Errors must not include account contents or encryption material.
        return json.dumps({'success': False, 'error': 'vault_unavailable'})


SCHEMA = {
    'name': 'credential_vault',
    'description': 'Separate encrypted login/password storage. When the user explicitly asks to remember account credentials, save them here and confirm only after success. Do not refuse solely because they contain a password. Never copy credentials into ordinary memory. get returns credentials only for an explicit user request for that account; never fetch speculatively. Group records are shared by authenticated participants of the current profile and chat across all topics. DM records remain private to the verified requester. Saving confirms the label without echoing the password. list returns labels only; use it to find the exact saved label before get when needed.',
    'parameters': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['save', 'get', 'list', 'delete']},
        'label': {'type': 'string', 'description': 'Stable service/account name, e.g. Edvibe — teacher'},
        'login': {'type': 'string'}, 'password': {'type': 'string'}},
        'required': ['action']}}

registry.register(name='credential_vault', toolset='credential-vault', schema=SCHEMA,
                  handler=lambda args, **kw: credential_vault(args),
                  check_fn=lambda: True, emoji='🔐')
