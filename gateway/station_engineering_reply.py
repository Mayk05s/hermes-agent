"""Route authenticated replies to registered Station engineering notices before LLM dispatch."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

class EngineeringReplyError(RuntimeError):
    """A matched engineering reply must never fall through to the general agent."""

def forward_reply(event, config_path=None):
    source = event.source
    if str(getattr(source.platform, 'value', source.platform)) != 'telegram':
        return False
    anchor = getattr(event, 'reply_to_message_id', None)
    raw = getattr(event, 'raw_message', None)
    sender = getattr(getattr(raw, 'from_user', None), 'id', None) or source.user_id
    if not anchor or not sender:
        return False
    path = Path(config_path or os.environ.get('HERMES_STATION_ENGINEERING_CONFIG', '/home/hermes/.hermes/quasar-bridge/engineering-config.json'))
    if not path.is_file():
        return False
    cfg = json.loads(path.read_text())
    notice = cfg.get('telegram') or {}
    if str(source.chat_id) != str(notice.get('chat_id')) or str(source.thread_id) != str(notice.get('thread_id')):
        return False
    with sqlite3.connect('file:' + cfg['database'] + '?mode=ro', uri=True) as db:
        rows = db.execute("SELECT job,data FROM repair_events WHERE kind='telegram_notice'").fetchall()
    matches = {job for job, raw in rows if str(json.loads(raw).get('message_id')) == str(anchor) and str(json.loads(raw).get('chat_id')) == str(source.chat_id) and str(json.loads(raw).get('thread_id')) == str(source.thread_id)}
    if not matches:
        return False
    if len(matches) != 1:
        raise EngineeringReplyError('Ambiguous engineering notice')
    text = getattr(event, 'authenticated_user_request_text', None)
    if text is None:
        raw = getattr(event, 'raw_message', None)
        text = getattr(raw, 'text', None) or getattr(raw, 'caption', None) or getattr(event, 'durable_request_text', None) or event.text
    helper = path.parent / 'src/engineering/telegram_reply.py'
    try:
        result = subprocess.run([sys.executable, str(helper), '--config', str(path), '--job', next(iter(matches)), '--text', text], capture_output=True, text=True, timeout=20)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise EngineeringReplyError('Engineering answer delivery failed') from exc
    if result.returncode:
        raise EngineeringReplyError('Engineering answer was not accepted; reply retained by Telegram')
    return True
