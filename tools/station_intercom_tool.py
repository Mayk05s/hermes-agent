"""Source-bound family intercom tool; gateway fast path and agent share one executor."""
import asyncio
import hashlib
import json
import logging
import re
from types import SimpleNamespace
from gateway.session_context import get_session_env
from tools.registry import registry

LOG=logging.getLogger(__name__)
SCHEMA={
    'name':'station_intercom',
    'description':('Ask people near Alice a question explicitly requested by the current Telegram sender. '
        'Alice quotes the question with the authenticated sender name, opens the microphone, and returns the answer to the same originating Telegram chat and topic. '
        'Use this tool for requests to ask Alice, ask through a Station, relay a question to people at home, and follow-up questions. '
        'This is an available working tool; do not replace the requested action with a draft question or claim there is no speaker tool. '
        'Only the configured family chat, owner private chat, and Tripio Telegram chat are permitted. Never use it from voice, for troubleshooting or descriptions of past requests, '
        'or to resend an old question unless the current user explicitly asks to resend. '
        'The speaker reply is attributed to Alice because the person speaking is unknown. '
        'Confirm speech only when spoken=true. Otherwise report message honestly; accepted alone does not confirm speech.'),
    'parameters':{'type':'object','properties':{
        'question':{'type':'string','description':'The actual question to quote verbatim, without the ask-Alice prefix or sender name. Preserve all clauses.','maxLength':900},
        'station':{'type':'string','description':'Optional configured station name such as Кухня or Детская; omit for default.'}},
        'required':['question'],'additionalProperties':False}}


async def invoke_event(event,cfg=None,speak=None,origin='gateway'):
    from gateway import station_intercom as service
    cfg=cfg if cfg is not None else service.settings()
    if not cfg:return None
    message=await service._handle_telegram(event,cfg,speak or service.ask)
    if message is None:return None
    source=event.source
    ident=hashlib.sha256(f'{source.chat_id}:{source.thread_id or ""}:{event.message_id}'.encode()).hexdigest()[:32]
    with service.Store(cfg['database']).db() as db:
        row=db.execute('SELECT state,speech_confirmed FROM intercom WHERE id=?',(ident,)).fetchone()
    state=row['state'] if row else 'rejected'
    result={'success':bool(row and row['speech_confirmed']) or state=='answered','accepted':row is not None,'spoken':bool(row and row['speech_confirmed']),
            'status':state,'message':message,'request_id':ident}
    LOG.info('station_intercom_tool origin=%s request=%s chat=%s thread=%s status=%s spoken=%s',
        origin,ident,source.chat_id,source.thread_id or '',state,result['spoken'])
    return result


def station_intercom(args,**kwargs):
    from gateway import station_intercom as service
    cfg=service.settings()
    get=lambda key:str(get_session_env('HERMES_SESSION_'+key,'') or '').strip()
    # Destination/identity/message IDs are runtime data, never model arguments.
    user=get('REQUESTER_USER_ID') or get('USER_ID')
    if not cfg or get('PLATFORM')!='telegram' or not get('PROFILE_NAME') or not service.source_allowed(cfg,get('CHAT_ID'),get('PROFILE_NAME'),user):
        return json.dumps({'success':False,'spoken':False,'error':'allowed_telegram_source_required'})
    if set(args)-{'question','station'}:return json.dumps({'success':False,'spoken':False,'error':'invalid_fields'})
    request=get('USER_REQUEST');user=get('REQUESTER_USER_ID') or get('USER_ID');anchor=get('MESSAGE_ID')
    if not request or not user.isdecimal() or not anchor.isdecimal():
        return json.dumps({'success':False,'spoken':False,'error':'authenticated_current_message_required'})
    # A current explicit prohibition cannot be overridden by an old transcript.
    if re.search(r'не\s+(?:отправляй|досылай|озвучивай|передавай)|не\s+(?:надо|нужно|стоит).{0,65}(?:досыл|отправ|передав|озвуч)|(?:досылать|отправлять|озвучивать|передавать).{0,40}не\s+(?:надо|нужно)',request,re.I|re.S):
        return json.dumps({'success':False,'spoken':False,'error':'current_request_prohibits_sending'})
    question=args.get('question')
    if not isinstance(question,str) or not 1<=len(question.strip())<=900:
        return json.dumps({'success':False,'spoken':False,'error':'question_required_up_to_900_characters'})
    station=args.get('station','')
    if not isinstance(station,str):return json.dumps({'success':False,'spoken':False,'error':'invalid_station'})
    prefix=''
    if station:
        selected=next((v for k,v in cfg['stations'].items() if station.casefold() in (k.casefold(),v['name'].casefold())),None)
        if not selected or not selected.get('aliases'):return json.dumps({'success':False,'spoken':False,'error':'unknown_station'})
        prefix=selected['aliases'][0]+': '
    event=SimpleNamespace(source=SimpleNamespace(platform='telegram',profile_name=get('PROFILE_NAME'),chat_id=get('CHAT_ID'),
        thread_id=get('THREAD_ID'),user_id=user,user_name=get('USER_NAME')),message_id=anchor,
        text='спроси Алису '+prefix+question.strip(),reply_to_message_id=None)
    try:
        result=asyncio.run(invoke_event(event,cfg,origin='agent'))
        return json.dumps(result or {'success':False,'spoken':False,'error':'request_not_accepted'},ensure_ascii=False)
    except Exception:
        LOG.exception('station_intercom_tool failed; no automatic effect retry')
        return json.dumps({'success':False,'spoken':False,'error':'delivery_not_confirmed','message':'Не удалось подтвердить доставку вопроса. Повторно его не отправляю.'},ensure_ascii=False)


registry.register(name='station_intercom',toolset='station-intercom',schema=SCHEMA,
                  handler=station_intercom,check_fn=lambda:True,emoji='📣')
