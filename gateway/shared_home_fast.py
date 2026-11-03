"""Optional Telegram entrance to the SAME Hermes rule store as voice.

Admission and homeassistant toolset scoping run before this hook. Every HA call
also passes the existing pre-tool guard; this hook never changes permissions.
"""
import json
import re


def original_command(text,requester_id):
    text=str(text or '')
    if text.startswith('[Replying to:'):
        return None  # Context-dependent replies remain model-owned.
    m=re.match(r'^\[[^\]\n|]+\|(\d+)\]\s*',text)
    if m:
        if not requester_id or m[1]!=str(requester_id):return None
        text=text[m.end():]
    return text.strip() or None


async def try_route(text,request_id,settings,guard,call,shared,still_current=lambda:True):
    if not settings.get('enabled') or not request_id or not still_current():return None
    async def checked(name,data):
        if not still_current():raise RuntimeError('superseded')
        args={'domain':'home_tools','service':name,'data':json.dumps(data,ensure_ascii=False)}
        if guard('ha_call_service',args):raise PermissionError('home_tool_denied')
        return await call(name,data)
    try:
        inventory=await checked('inventory',{})
        if not still_current():return None
        result=await shared.try_fast(text,request_id,settings.get('source','telegram'),inventory,checked)
        if not still_current():
            return {**result,'handled':False,'reason':'superseded_input','model_instruction':'A newer input arrived. Preserve this receipt; do not repeat confirmed effects. Let the existing durable input mechanism resume the complete task.'}
        if result.get('reason') in ('unknown','partial','ambiguous'):
            return {**result,'lookup_already_attempted':True,'model_instruction':'Fast lookup already checked the complete original utterance. Do not repeat it. Understand the full request and discover an authorized HA path; no partial execution.'}
        return result
    except PermissionError:return None
    except Exception as exc:
        return {'handled':False,'request_id':request_id,'reason':'fast_route_unavailable','error_class':type(exc).__name__,
                'model_instruction':'Find an authorized solution. Inspect HA delivery for this original request before sending anything; no blind retries or new identity.'}
