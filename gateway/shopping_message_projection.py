"""Gateway-only handoff from successful executed mutations to a durable card.

Transport context comes from authenticated ingress, never model arguments,
response prose, callbacks or URLs. The remote server owns list/message identity.
"""
import asyncio
import copy
import dataclasses
import json
import hashlib
import hmac
import os
import re
import urllib.request
from urllib.parse import urlsplit, urlunsplit


def projection_row_ids(records):
    if not records or any(record.get('section') != 'shopping' or record.get('success') is not True for record in records):
        return []
    writes = [record for record in records if record.get('mutation')]
    if not writes or any(not record.get('row_ids') for record in writes):
        return []
    return sorted({row_id for record in writes for row_id in record['row_ids']})


def projection_show_row_ids(records):
    reads=[record for record in records or [] if record.get('section')=='shopping' and not record.get('mutation')
           and record.get('display') is True and record.get('success') is True and record.get('exact_read') and record.get('row_ids') and len(record.get('list_ids') or [])==1]
    if not reads:return []
    return sorted(set(reads[-1]['row_ids']))


def capture_projection_identity(event, adapter=None):
    """Persist authenticated Telegram authorship independently of memory opt-in."""
    raw=getattr(event,'raw_message',None)
    if raw is None:
        return getattr(event,'shopping_projection_identity',None)
    actor=getattr(getattr(raw,'from_user',None),'id',None)
    source=event.source
    if not re.fullmatch(r'[1-9]\d*',str(actor or '')):
        return None
    if str(getattr(getattr(raw,'chat',None),'id',''))!=str(source.chat_id) or str(getattr(raw,'message_thread_id',None) or '')!=str(source.thread_id or ''):
        return None
    bot=getattr(adapter,'_bot',None)
    if bot is None:
        try:bot=raw.get_bot()
        except (AttributeError,RuntimeError):return None
    bot_id=getattr(bot,'id',None)
    secret=os.environ.get('HEALTH_ACTIONS_API_TOKEN','')
    if not bot_id or not secret:return None
    payload={'actorId':str(actor),'chatId':str(source.chat_id),'threadId':str(source.thread_id or ''),
             'botId':str(bot_id),'messageId':str(event.message_id or '')}
    reply=getattr(raw,'reply_to_message',None)
    if reply is not None and str(getattr(getattr(reply,'from_user',None),'id',''))==str(bot_id) and str(getattr(getattr(reply,'chat',None),'id',''))==str(source.chat_id) and str(getattr(reply,'message_thread_id',None) or '')==str(source.thread_id or ''):
        reply_id=str(getattr(reply,'message_id',''))
        if re.fullmatch(r'[1-9]\d*',reply_id):payload['replyMessageId']=reply_id
    encoded=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
    receipt={'context':payload,'signature':hmac.new(secret.encode(),encoded,hashlib.sha256).hexdigest()}
    event.shopping_projection_identity=receipt
    return receipt


def restore_projection_identity(runner,event):
    store=runner._durable_store()
    if store is None:return
    inbox_id=getattr(event,'durable_inbox_id',None)
    inbox=store.get_inbox_event(inbox_id) if inbox_id else None
    if inbox is None and getattr(event,'durable_job_id',None):
        for item in store.inputs_for_job(event.durable_job_id):
            candidate=store.get_inbox_event(item['event_id'])
            if candidate and str(candidate.get('request_message_id'))==str(event.message_id):
                inbox=candidate;break
    if inbox:
        event.shopping_projection_identity=json.loads(inbox.get('event_json') or '{}').get('shopping_projection_identity')


def _receipt_context(event,adapter):
    receipt=getattr(event,'shopping_projection_identity',None)
    secret=os.environ.get('HEALTH_ACTIONS_API_TOKEN','')
    if not isinstance(receipt,dict) or not secret:return None
    payload=receipt.get('context')
    if not isinstance(payload,dict) or set(payload)-{'actorId','chatId','threadId','botId','messageId','showIntent','replyMessageId'} or not {'actorId','chatId','threadId','botId','messageId'}.issubset(payload):return None
    encoded=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
    expected=hmac.new(secret.encode(),encoded,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,str(receipt.get('signature',''))):return None
    source=event.source
    if payload['chatId']!=str(source.chat_id) or payload['threadId']!=str(source.thread_id or '') or payload['messageId']!=str(event.message_id or ''):
        return None
    if payload['botId']!=str(getattr(getattr(adapter,'_bot',None),'id','')):return None
    if getattr(source,'user_id',None) and str(source.user_id)!=payload['actorId']:return None
    return {key:value for key,value in payload.items() if key not in ['messageId','showIntent','replyMessageId']}


def authenticated_projection_context(event, adapter):
    source = event.source
    raw = getattr(event, 'raw_message', None)
    actor = getattr(getattr(raw, 'from_user', None), 'id', None)
    if actor is None:
        receipt_context=_receipt_context(event,adapter)
        if receipt_context is not None:return receipt_context
        from gateway.memory_ingress import matching_input
        sealed = matching_input(getattr(event, 'authenticated_memory_input', None), source, event.message_id)
        if sealed is None:
            sealed = matching_input(getattr(event, 'authenticated_memory_identity', None), source, event.message_id)
        actor = sealed.user_id if sealed else None
    # Never parse user ids from an attribution label or trust a forwarded author.
    if not re.fullmatch(r'[1-9]\d*', str(actor or '')):
        return None
    if raw is not None:
        if str(getattr(getattr(raw, 'chat', None), 'id', '')) != str(source.chat_id):
            return None
        if str(getattr(raw, 'message_thread_id', None) or '') != str(source.thread_id or ''):
            return None
    bot_id = getattr(getattr(adapter, '_bot', None), 'id', None)
    if not bot_id:
        return None
    return {'actorId':str(actor), 'chatId':str(source.chat_id),
            'threadId':str(source.thread_id or ''), 'botId':str(bot_id)}


def projection_event_for_result(event,result):
    actual=str(result.get('shopping_projection_event_id') or '')
    if not actual or actual==str(event.message_id or ''):return event
    receipt=result.get('shopping_projection_identity')
    if not isinstance(receipt,dict):return None
    current=copy.copy(event)
    current.message_id=actual
    current.source=dataclasses.replace(event.source,user_id=None,message_id=actual)
    current.raw_message=None
    current.authenticated_memory_input=None
    current.authenticated_memory_identity=None
    current.shopping_projection_identity=receipt
    return current


async def project_shopping_message(event, adapter, records):
    receipt=capture_projection_identity(event,adapter)
    sealed=_receipt_context(event,adapter)
    show=sealed is not None and bool(projection_show_row_ids(records))
    rollover=[record for record in records or [] if record.get('section')=='shopping' and record.get('success') is True and record.get('mutation') and record.get('rollover')]
    rollover=rollover[-1] if rollover else None
    rows = rollover.get('row_ids',[]) if rollover else projection_show_row_ids(records) if show else projection_row_ids(records)
    if not rows and not rollover:
        return False
    context = authenticated_projection_context(event, adapter)
    api = os.environ.get('HEALTH_ACTIONS_API_URL', '')
    token = os.environ.get('HEALTH_ACTIONS_API_TOKEN', '')
    if not context or not api or not token:
        return False
    parts = urlsplit(api)
    # The configured action URL is an endpoint, not a base URL. Do not accept
    # a URL from tool output, the browser, or a model-authored target.
    if parts.scheme != 'https' or parts.path.rstrip('/') != '/api/admin/health-actions':
        return False
    url = urlunsplit((parts.scheme,parts.netloc,'/api/admin/shopping-message','',''))
    def send():
        body=dict(context,rowIds=rows)
        if rollover:
            body.update(mode='rollover',expectedListId=rollover['rollover']['liveListId'],rolloverOldListId=rollover['rollover']['oldListId'])
        elif show:
            target=[record for record in records if record.get('display') and record.get('success') is True and not record.get('mutation') and record.get('exact_read') and record.get('row_ids') and len(record.get('list_ids') or [])==1][-1]
            display_context={**context,'messageId':str(event.message_id),'action':'show-shopping-list','listId':target['list_ids'][0]}
            encoded=json.dumps(display_context,sort_keys=True,separators=(',',':')).encode()
            display_receipt={'context':display_context,'signature':hmac.new(token.encode(),encoded,hashlib.sha256).hexdigest()}
            body.update(mode='show',eventId=str(event.message_id),intentReceipt=display_receipt,expectedListId=target['list_ids'][0])
        request = urllib.request.Request(url, data=json.dumps(body).encode(),
            headers={'Content-Type':'application/json','X-Health-Actions-Token':token,'User-Agent':'HermesGateway/1.0'}, method='POST')
        with urllib.request.urlopen(request, timeout=25) as response:
            result=json.load(response)
        return result if result.get('ok') is True and result.get('queued') is True else False
    return await asyncio.to_thread(send)
