"""Family-only Telegram/Station intercom. Persist effects before sending; never replay uncertain writes."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

CONFIG = Path('/home/hermes/.hermes/quasar-bridge/intercom.json')
QUESTION = re.compile(r'^(?:(?:трипио|трипео|tripio|@\w+)[, :]+)?(?:спроси(?:\s+(?:его|её|ее|их))?\s+(?:у\s+алисы|алису|на\s+алисе|через\s+алису)|передай\s+(?:вопрос\s+)?(?:алисе|через\s+алису))\s*[:,—-]?\s*', re.I)
ANSWER = re.compile(r'^(?:алиса[, :]+)?(?:ответь|ответить|передай\s+ответ)(?:\s+(?:в\s+телеграм|в\s+чат|на\s+сообщение))?\s*[:,—-]?\s*', re.I)
CANCEL = {'отмена', 'отмени', 'не отвечать', 'закрой связь', 'закончи связь'}


def is_filler(text):
    """Bare address of the speaker or a request to repeat — never a reply."""
    from gateway.station_lesson import is_repeat
    return is_repeat(text)


def settings(path=None):
    p=Path(path or os.environ.get('HERMES_STATION_INTERCOM_CONFIG', str(CONFIG)))
    if not p.is_file():return None
    cfg=json.loads(p.read_text())
    return cfg if cfg.get('enabled') else None


def source_allowed(cfg,chat,profile=None,user=None):
    rules=cfg.get('telegram_sources')
    if rules is None:rules=[{'chat_id':str(cfg['family_chat_id']),'profile':'family-chat'}]
    for rule in rules:
        if str(chat)!=str(rule['chat_id']):continue
        if profile and profile!=rule.get('profile'):continue
        if rule.get('user_ids') and str(user) not in {str(v) for v in rule['user_ids']}:continue
        return True
    return False


class Store:
    def __init__(self, path):
        self.path=path
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("CREATE TABLE IF NOT EXISTS intercom (id TEXT PRIMARY KEY, station TEXT, chat TEXT, thread TEXT, anchor TEXT, question TEXT, created REAL, expires REAL, auto_until REAL, state TEXT, answer TEXT, voice_request TEXT UNIQUE, telegram_id TEXT)")
            columns={r[1] for r in db.execute('PRAGMA table_info(intercom)')}
            if 'speech_confirmed' not in columns:db.execute('ALTER TABLE intercom ADD COLUMN speech_confirmed INTEGER NOT NULL DEFAULT 0')
            for name in ('sender_id','sender_name','speech_kind'):
                if name not in columns:db.execute('ALTER TABLE intercom ADD COLUMN '+name+" TEXT NOT NULL DEFAULT ''")
    def db(self):
        db=sqlite3.connect(self.path, timeout=10);db.row_factory=sqlite3.Row
        return db
    def reserve(self, station, chat, thread, anchor, question, ttl=600, *, sender_id="", sender_name="", speech_kind="спрашивает"):
        ident=hashlib.sha256(f'{chat}:{thread}:{anchor}'.encode()).hexdigest()[:32]
        now=time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM intercom WHERE id=?',(ident,)).fetchone()
            if old:return dict(old),False
            busy=db.execute("SELECT 1 FROM intercom WHERE station=? AND expires>? AND state IN ('dispatching','waiting','sending','unconfirmed')",(station,now)).fetchone()
            if busy:return None,False
            db.execute("INSERT INTO intercom (id,station,chat,thread,anchor,question,created,expires,auto_until,state,sender_id,sender_name,speech_kind) VALUES (?,?,?,?,?,?,?,?,?,'dispatching',?,?,?)",(ident,station,chat,thread,anchor,question,now,now+ttl,now+90,sender_id,sender_name,speech_kind))
            return dict(db.execute('SELECT * FROM intercom WHERE id=?',(ident,)).fetchone()),True
    def pending(self,station):
        with self.db() as db:
            row=db.execute("SELECT * FROM intercom WHERE station=? AND expires>? AND state IN ('dispatching','waiting','sending','unconfirmed') ORDER BY created DESC LIMIT 1",(station,time.time())).fetchone()
            return dict(row) if row else None
    def update(self,ident,**fields):
        assert set(fields)<={'state','auto_until','answer','voice_request','telegram_id'}
        with self.db() as db:db.execute('UPDATE intercom SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),ident))
    def claim(self,ident,text,request):
        with self.db() as db:
            cur=db.execute("UPDATE intercom SET state='sending',answer=?,voice_request=? WHERE id=? AND state IN ('dispatching','waiting')",(text,request,ident))
            return bool(cur.rowcount)
    def receipt(self,request):
        with self.db() as db:
            row=db.execute('SELECT * FROM intercom WHERE voice_request=?',(request,)).fetchone()
            return dict(row) if row else None
    def parent(self,chat,thread,anchor):
        with self.db() as db:
            row=db.execute("SELECT * FROM intercom WHERE chat=? AND thread=? AND telegram_id=? AND state='answered' AND expires>?",(chat,thread,str(anchor),time.time())).fetchone()
            return dict(row) if row else None


def event_text(event):
    raw=getattr(event,'raw_message',None)
    # Family observation prepends [name|id] to authenticated_user_request_text.
    # Parse the actual Telegram message before that presentation wrapper.
    text=(getattr(raw,'text',None) or getattr(raw,'caption',None) or getattr(event,'authenticated_user_request_text',None) or getattr(event,'durable_request_text',None) or event.text or '').strip()
    sender=getattr(raw,'from_user',None)
    ident=str(getattr(sender,'id',None) or getattr(event.source,'user_id',None) or '')
    header=re.match(r'^\[[^\]\n|]+\|([0-9]+)\]\s*\n',text)
    if header and header.group(1)==ident:text=text[header.end():].strip()
    return text


def sender_identity(event,cfg):
    # Shared family sessions deliberately clear source.user_id; use the real
    # authenticated Telegram envelope, never a quoted/forwarded author or text.
    raw=getattr(event,'raw_message',None)
    user=getattr(raw,'from_user',None)
    if getattr(raw,'sender_chat',None) is not None or getattr(user,'is_bot',False):return None
    ident=str(getattr(user,'id',None) or getattr(event.source,'user_id',None) or '')
    if not ident.isdecimal():return None
    name=(cfg.get('sender_names',{}).get(ident) or getattr(user,'first_name',None)
          or getattr(event.source,'user_name',None) or '')
    import unicodedata
    name=''.join(c for c in str(name) if unicodedata.category(c)[0] in ('L','M') or c in " '-’")
    name=' '.join(name.split())[:80].strip()
    return (ident,name) if name else None


def spoken_question(row):
    if not row.get('sender_id') or not row.get('sender_name'):raise ValueError('Question author is required')
    kind=row.get('speech_kind') or 'спрашивает'
    if kind not in ('спрашивает','уточняет','интересуется'):raise ValueError('Invalid attribution')
    return row['sender_name']+' '+kind+': «'+row['question']+'»'


async def ha_request(cfg, method, path, payload=None):
    from dotenv import dotenv_values
    import aiohttp
    env=dotenv_values(cfg['ha_environment'])
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.request(method,env['HASS_URL'].rstrip('/')+path,json=payload,headers={'Authorization':'Bearer '+env['HASS_TOKEN']}) as response:
            response.raise_for_status()
            return await response.json()


class StationBusy(RuntimeError):
    pass


async def ask(cfg,row):
    target=cfg['stations'][row['station']]
    previous=await ha_request(cfg,'GET','/api/states/'+target['sensor'])
    attrs=previous.get('attributes',{})
    if attrs.get('active_request_id') or attrs.get('queue_size',0):raise StationBusy()
    payload={'station':target['event_station'],'station_id':row['station'],'request_id':'intercom-'+row['id'],
             'provider':'yandex','voice':'Алиса','text':spoken_question(row), 'await_reply':True}
    # Exactly one POST. A lost acknowledgement never causes a repeated announcement.
    await ha_request(cfg,'POST','/api/events/tripio_station_say',payload)
    deadline=time.monotonic()+75
    while time.monotonic()<deadline:
        status=await ha_request(cfg,'GET','/api/states/'+target['sensor'])
        attrs=status.get('attributes',{})
        if attrs.get('last_request_id')==payload['request_id']:
            if attrs.get('last_success') is True and attrs.get('awaiting_reply') is True:return True
            if attrs.get('last_error') or attrs.get('last_cancelled'):return False
        await asyncio.sleep(.5)
    return False


async def handle_telegram(event,cfg=None,speak=ask):
    from tools.station_intercom_tool import invoke_event
    result=await invoke_event(event,cfg,speak)
    return result['message'] if result is not None else None


async def _handle_telegram(event,cfg=None,speak=ask):
    cfg=cfg if cfg is not None else settings()
    source=event.source
    if not cfg or str(getattr(source.platform,'value',source.platform))!='telegram':return None
    identity=sender_identity(event,cfg)
    if not source_allowed(cfg,source.chat_id,getattr(source,'profile_name',None),identity[0] if identity else None):return None
    text=event_text(event);match=QUESTION.match(text)
    # Only an explicit prefix or reply to this intercom's transcript opens another question.
    store=Store(cfg['database'])
    parent=store.parent(str(source.chat_id),str(source.thread_id or ''),getattr(event,'reply_to_message_id',''))
    if not match and not parent:return None
    question=text[match.end():].strip() if match else text
    if not question:return 'Какой вопрос задать через Алису?'
    if len(question)>900:return 'Вопрос слишком длинный: сократите его до 900 символов.'
    station=parent['station'] if parent else cfg['default_station']
    for key,target in cfg['stations'].items():
        for alias in target.get('aliases',[]):
            prefix=re.match(r'^'+re.escape(alias)+r'\s*[:,—-]?\s+',question,re.I)
            if prefix:station=key;question=question[prefix.end():].strip();break
    if not question:return 'Какой вопрос задать через Алису?'
    if not getattr(event,'message_id',None):return 'Не удалось определить исходное сообщение.'
    identity=sender_identity(event,cfg)
    if identity is None:return 'Не удалось определить автора Telegram-сообщения. Вопрос не отправлен.'
    row,fresh=store.reserve(station,str(source.chat_id),str(source.thread_id or ''),str(event.message_id),question,
                            sender_id=identity[0],sender_name=identity[1],speech_kind='уточняет' if parent else 'спрашивает')
    if row is None:return 'На этой Алисе уже ждёт ответа другой вопрос. Сначала ответьте на него или скажите рядом с колонкой «Алиса, ответь», затем «отмена».'
    if not fresh:return 'Этот вопрос уже принят. Повторно его не отправляю.'
    try:confirmed=await speak(cfg,row)
    except StationBusy:
        store.update(row['id'],state='not_sent',auto_until=0)
        return 'Станция занята. Вопрос не отправлен; попробуйте после её освобождения.'
    except Exception:confirmed=False
    if confirmed:
        with store.db() as db:db.execute('UPDATE intercom SET speech_confirmed=1 WHERE id=?',(row['id'],))
        with store.db() as db:db.execute("UPDATE intercom SET state='waiting',auto_until=? WHERE id=? AND state='dispatching'",(time.time()+30,row['id']))
        return 'Алиса задала вопрос и открыла микрофон. Ответ придёт сюда. Позже можно сказать «Алиса, ответь».'
    with store.db() as db:
        current=db.execute('SELECT state FROM intercom WHERE id=?',(row['id'],)).fetchone()['state']
        if current=='answered':return 'Ответ с Алисы уже пришёл в чат.'
        db.execute("UPDATE intercom SET state='unconfirmed',auto_until=0 WHERE id=? AND state='dispatching'",(row['id'],))
    return 'Не удалось подтвердить озвучивание вопроса. Повторно его не отправляю. Если вопрос прозвучал, скажите «Алиса, ответь».'


async def send_answer(cfg,row,text):
    from dotenv import dotenv_values
    from gateway.platforms.telegram_network import TelegramFallbackTransport
    import httpx
    token=dotenv_values(cfg['telegram_environment'])['TELEGRAM_BOT_TOKEN']
    payload={'chat_id':row['chat'],'text':'Алиса ответила ('+cfg['stations'][row['station']]['name']+'):\n'+text,
             'reply_parameters':{'message_id':int(row['anchor']),'allow_sending_without_reply':True}}
    if row['thread'] and row['thread']!='1':payload['message_thread_id']=int(row['thread'])
    async with httpx.AsyncClient(transport=TelegramFallbackTransport(cfg.get('fallback_ips',[])),timeout=10) as client:
        response=await client.post('https://api.telegram.org/bot'+token+'/sendMessage',json=payload)
        response.raise_for_status();result=response.json()
        if not result.get('ok'):raise RuntimeError('Telegram did not acknowledge answer')
        return str(result['result']['message_id'])


async def handle_voice(station,text,request,classifier=False,cfg=None,send=send_answer):
    cfg=cfg if cfg is not None else settings()
    if not cfg or station not in cfg['stations']:return None
    # An active study lesson owns this station's dialogue (multi-turn tutor).
    try:
        from gateway.station_lesson import handle_voice as lesson_voice
        lesson=await lesson_voice(station,text,request,classifier=classifier,cfg=cfg)
    except Exception:
        import logging;logging.getLogger(__name__).warning('station lesson failed',exc_info=True);lesson=None
    if lesson is not None:return lesson
    store=Store(cfg['database']);old=store.receipt(request)
    if old and classifier:return ('{"route":"llm"}',False)
    if old:return ('Ответ отправлен в чат.',False) if old['state']=='answered' else ('Не удалось подтвердить отправку. Ответ сохранён, повторно его не отправляю.',False)
    row=store.pending(station);match=ANSWER.match(text.strip())
    if row is not None and not source_allowed(cfg,row['chat'],user=row['sender_id']):row=None
    if row is None:return ('{"route":"llm"}' if classifier else 'Сейчас нет вопроса из Telegram.',False) if match else None
    if not match and time.time()>row['auto_until']:return None
    if classifier:return ('{"route":"llm"}',False)
    answer=text.strip()[match.end():].strip() if match else text.strip()
    if answer and is_filler(answer):
        # A bare wake word ("Алис") or "повтори" is not an answer: say the question again and keep waiting.
        store.update(row['id'],auto_until=time.time()+30)
        try:question=spoken_question(row)
        except ValueError:question=row['question']
        return ('Повторяю вопрос. '+question,True)
    if not answer:
        store.update(row['id'],auto_until=time.time()+30,**({'state':'waiting'} if row['state']=='unconfirmed' and not row['answer'] else {}))
        return ('Что передать в чат?',True)
    if answer.lower().rstrip('.!') in CANCEL:
        store.update(row['id'],state='cancelled');return ('Связь с чатом закрыта.',False)
    if len(answer)>3000:return ('Ответ слишком длинный. Скажите короче.',True)
    if row['state'] in ('sending','unconfirmed'):
        # An unconfirmed speech dispatch may still be explicitly answered; an
        # unconfirmed Telegram effect must never be sent a second time.
        if row['answer']:return ('Отправка ответа не подтверждена. Ответ сохранён, повторно его не отправляю.',False)
        if not match:return None
        store.update(row['id'],state='waiting')
    if not store.claim(row['id'],answer,request):return ('Этот ответ уже принят.',False)
    try:
        message=await send(cfg,row,answer)
        store.update(row['id'],state='answered',telegram_id=message)
        return ('Ответ отправлен в чат.',False)
    except Exception:
        store.update(row['id'],state='unconfirmed',auto_until=0)
        return ('Не удалось подтвердить отправку. Ответ сохранён, повторно его не отправляю.',False)
