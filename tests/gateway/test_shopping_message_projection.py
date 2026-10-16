from types import SimpleNamespace as NS
from unittest.mock import patch
import pytest
from gateway.shopping_message_projection import projection_row_ids, authenticated_projection_context, project_shopping_message


def event(**changes):
    source=NS(chat_id='-100',thread_id='42')
    raw=NS(from_user=NS(id=7),chat=NS(id=-100),message_thread_id=42)
    values=dict(source=source,raw_message=raw,message_id='9')
    values.update(changes)
    return NS(**values)


def test_only_successful_executed_shopping_mutations_can_project():
    write=dict(section='shopping',success=True,mutation=True,row_ids=[2,1])
    assert projection_row_ids([write]) == [1,2]
    for extra in [dict(section='shopping',mutation=True),dict(section='planning',success=True),dict(section='shopping',success=True,mutation=True,row_ids=[])]:
        assert projection_row_ids([write,extra]) == []
    assert projection_row_ids([dict(section='shopping',success=True,mutation=False,row_ids=[1])]) == []


def test_real_telegram_author_and_bot_own_transport_context():
    adapter=NS(_bot=NS(id=123))
    assert authenticated_projection_context(event(),adapter) == dict(actorId='7',chatId='-100',threadId='42',botId='123')
    assert authenticated_projection_context(event(raw_message=NS(from_user=NS(id=7),chat=NS(id=-200),message_thread_id=42)),adapter) is None
    assert authenticated_projection_context(event(raw_message=NS(from_user=NS(id=7),chat=NS(id=-100),message_thread_id=43)),adapter) is None
    assert authenticated_projection_context(event(),NS(_bot=None)) is None


@pytest.mark.asyncio
async def test_projection_uses_configured_internal_url_and_actual_author_only(monkeypatch):
    monkeypatch.setenv('HEALTH_ACTIONS_API_URL','https://mini.example/api/admin/health-actions')
    monkeypatch.setenv('HEALTH_ACTIONS_API_TOKEN','test-secret')
    records=[dict(section='shopping',success=True,mutation=True,row_ids=[2])]
    requests=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): return b'{"ok":true,"queued":true}'
    def send(request,timeout):
        import json
        requests.append((request.full_url,json.loads(request.data),timeout))
        return Response()
    with patch('urllib.request.urlopen',send):
        assert await project_shopping_message(event(),NS(_bot=NS(id=123)),records)
    assert requests == [('https://mini.example/api/admin/shopping-message',dict(actorId='7',chatId='-100',threadId='42',botId='123',rowIds=[2]),25)]


@pytest.mark.asyncio
async def test_non_https_or_model_like_url_never_receives_token(monkeypatch):
    monkeypatch.setenv('HEALTH_ACTIONS_API_URL','https://evil.example/other')
    monkeypatch.setenv('HEALTH_ACTIONS_API_TOKEN','test-secret')
    with patch('urllib.request.urlopen',side_effect=AssertionError('must not send')):
        assert not await project_shopping_message(event(),NS(_bot=NS(id=123)),[dict(section='shopping',success=True,mutation=True,row_ids=[2]),
          dict(section='shopping',success=True,mutation=False,exact_read=True,row_ids=[99],list_ids=['22222222-2222-2222-2222-222222222222']),
          dict(section='shopping',display=True,success=False,mutation=False),
          dict(section='planning',success=True,mutation=False)])


def test_signed_durable_author_survives_without_memory_and_rejects_all_context_substitutions(monkeypatch):
    import copy
    from gateway.shopping_message_projection import capture_projection_identity
    monkeypatch.setenv('HEALTH_ACTIONS_API_TOKEN','projection-secret')
    adapter=NS(_bot=NS(id=123))
    original=event()
    receipt=capture_projection_identity(original,adapter)
    recovered=event(raw_message=None)
    recovered.shopping_projection_identity=receipt
    assert authenticated_projection_context(recovered,adapter)['actorId']=='7'
    for field in ['actorId','chatId','threadId','botId','messageId']:
        altered=copy.deepcopy(receipt);altered['context'][field]='999'
        recovered.shopping_projection_identity=altered
        assert authenticated_projection_context(recovered,adapter) is None
    recovered.shopping_projection_identity=receipt
    recovered.source.thread_id='43'
    assert authenticated_projection_context(recovered,adapter) is None
    recovered.source.thread_id='42';recovered.message_id='10'
    assert authenticated_projection_context(recovered,adapter) is None
    recovered.message_id='9'
    assert authenticated_projection_context(recovered,NS(_bot=NS(id=124))) is None
    monkeypatch.setenv('HEALTH_ACTIONS_API_TOKEN','other-secret')
    assert authenticated_projection_context(recovered,adapter) is None


def test_durable_restoration_uses_inbox_metadata_not_persisted_sender_labels(monkeypatch):
    import json
    from gateway.shopping_message_projection import capture_projection_identity,restore_projection_identity
    monkeypatch.setenv('HEALTH_ACTIONS_API_TOKEN','projection-secret')
    receipt=capture_projection_identity(event(),NS(_bot=NS(id=123)))
    recovered=event(raw_message=None);recovered.durable_inbox_id='inbox-1'
    store=NS(get_inbox_event=lambda key:{'event_json':json.dumps({'shopping_projection_identity':receipt}),'request_text':'[fake|999]'})
    restore_projection_identity(NS(_durable_store=lambda:store),recovered)
    assert authenticated_projection_context(recovered,NS(_bot=NS(id=123)))['actorId']=='7'


def test_ingress_receipt_seals_identity_without_lexical_intent(monkeypatch):
 from gateway.shopping_message_projection import capture_projection_identity
 monkeypatch.setenv('HEALTH_ACTIONS_API_TOKEN','secret')
 ev=event();ev.raw_message.text='покажи список';ev.raw_message.forward_origin=NS(type='user')
 assert 'showIntent' not in capture_projection_identity(ev,NS(_bot=NS(id=123)))['context']


@pytest.mark.asyncio
async def test_explicit_show_uses_exact_read_and_signed_original_event_not_model_prose(monkeypatch):
 import json
 monkeypatch.setenv('HEALTH_ACTIONS_API_URL','https://mini.example/api/admin/health-actions')
 monkeypatch.setenv('HEALTH_ACTIONS_API_TOKEN','secret')
 ev=event();ev.raw_message.text='добавь хлеб и покажи список'
 records=[dict(section='shopping',display=True,success=True,mutation=False,exact_read=True,row_ids=[1],list_ids=['11111111-1111-1111-1111-111111111111']),
          dict(section='shopping',success=True,mutation=True,row_ids=[2]),
          dict(section='shopping',success=True,mutation=False,exact_read=True,row_ids=[99],list_ids=['22222222-2222-2222-2222-222222222222']),
          dict(section='shopping',display=True,success=False,mutation=False),
          dict(section='planning',success=True,mutation=False)]
 requests=[]
 class Response:
  def __enter__(self):return self
  def __exit__(self,*args):pass
  def read(self):return b'{"ok":true,"queued":true,"messageId":"901","partial":false}'
 def send(request,timeout):requests.append(json.loads(request.data));return Response()
 with patch('urllib.request.urlopen',send):
  assert await project_shopping_message(ev,NS(_bot=NS(id=123)),records)
 assert requests[0]['mode']=='show';assert requests[0]['eventId']=='9';assert requests[0]['rowIds']==[1]
 assert requests[0]['intentReceipt']['context']['action']=='show-shopping-list'
 ev.raw_message.text='добавь воду'
 with patch('urllib.request.urlopen',send):assert await project_shopping_message(ev,NS(_bot=NS(id=123)),records)
 assert requests[1]['mode']=='show'  # semantic tool action, independent of words
 ev.raw_message.text='покажи список'
 with patch('urllib.request.urlopen',side_effect=AssertionError('no ambiguous show')):
  assert not await project_shopping_message(ev,NS(_bot=NS(id=123)),[dict(section='shopping',success=True,mutation=False,exact_read=False,row_ids=[1])])


def test_queued_followup_uses_its_own_signed_event_for_show_and_keeps_original_unchanged(monkeypatch):
 from gateway.shopping_message_projection import capture_projection_identity,projection_event_for_result
 from gateway.session import SessionSource
 from gateway.config import Platform
 monkeypatch.setenv('HEALTH_ACTIONS_API_TOKEN','secret')
 original=event();original.source=SessionSource(platform=Platform.TELEGRAM,chat_id='-100',thread_id='42',user_id='7',message_id='9')
 followup=event(message_id='10');followup.source=SessionSource(platform=Platform.TELEGRAM,chat_id='-100',thread_id='42',user_id='8',message_id='10')
 followup.raw_message.from_user.id=8;followup.raw_message.text='покажи список'
 receipt=capture_projection_identity(followup,NS(_bot=NS(id=123)))
 current=projection_event_for_result(original,dict(shopping_projection_event_id='10',shopping_projection_identity=receipt))
 assert original.message_id=='9';assert current.message_id=='10'
 assert authenticated_projection_context(current,NS(_bot=NS(id=123)))['actorId']=='8'
 assert 'showIntent' not in current.shopping_projection_identity['context']
 assert projection_event_for_result(original,dict(shopping_projection_event_id='10')) is None


def test_exact_read_keeps_only_server_contract_generation():
 import json
 from gateway.miniapp_reply_contract import MiniappReplyCollector
 collector=MiniappReplyCollector();generation='11111111-1111-1111-1111-111111111111'
 collector.record('mcp_health_actions_list_shopping_items',json.dumps({'ok':True,'action':'list-shopping-items',
   '_miniapp':{'start_param':'shopping_buy_g'+generation.replace('-','')+'_c100'},
   'items':[{'shoppingId':1,'liveListId':generation},{'shoppingId':2,'liveListId':'22222222-2222-2222-2222-222222222222'}]}))
 record=collector.snapshot()[0]
 assert record['row_ids']==[1];assert record['exact_read'] is True


@pytest.mark.asyncio
async def test_internal_read_never_rotates_even_with_show_words(monkeypatch):
 monkeypatch.setenv('HEALTH_ACTIONS_API_TOKEN','secret')
 ev=event();ev.raw_message.text='покажи список'
 records=[dict(section='shopping',success=True,mutation=False,exact_read=True,row_ids=[1],list_ids=['11111111-1111-1111-1111-111111111111'])]
 with patch('urllib.request.urlopen',side_effect=AssertionError('internal read must not rotate')):
  assert not await project_shopping_message(ev,NS(_bot=NS(id=123)),records)


def test_display_collector_marks_only_semantic_action():
 import json
 from gateway.miniapp_reply_contract import MiniappReplyCollector
 collector=MiniappReplyCollector();generation='11111111-1111-1111-1111-111111111111'
 collector.record('mcp_health_actions_show_shopping_list',json.dumps({'ok':True,'action':'show-shopping-list',
   '_miniapp':{'start_param':'shopping_buy_g'+generation.replace('-','')+'_c100'},
   'items':[{'shoppingId':1,'liveListId':generation}]}))
 record=collector.snapshot()[0]
 assert record['display'] is True and record['mutation'] is False and record['exact_read'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('row_ids', [[], [101, 102]])
async def test_semantic_rollover_handoff_works_for_empty_or_carried_new_generation(monkeypatch,row_ids):
 import json
 monkeypatch.setenv('HEALTH_ACTIONS_API_URL','https://mini.example/api/admin/health-actions')
 monkeypatch.setenv('HEALTH_ACTIONS_API_TOKEN','test-secret')
 target={'oldListId':'11111111-1111-1111-1111-111111111111','liveListId':'22222222-2222-2222-2222-222222222222'}
 records=[dict(section='shopping',success=True,mutation=True,row_ids=row_ids,rollover=target)]
 requests=[]
 class Response:
  def __enter__(self):return self
  def __exit__(self,*args):pass
  def read(self):return b'{"ok":true,"queued":true}'
 def send(request,timeout):requests.append(json.loads(request.data));return Response()
 with patch('urllib.request.urlopen',send):assert await project_shopping_message(event(),NS(_bot=NS(id=123)),records)
 assert requests[0]['mode']=='rollover' and requests[0]['expectedListId']==target['liveListId']
 assert requests[0]['rolloverOldListId']==target['oldListId'] and requests[0]['rowIds']==row_ids

def test_reply_target_signed_only_from_actual_bot_message(monkeypatch):
 from types import SimpleNamespace as NS
 from gateway.shopping_message_projection import capture_projection_identity
 monkeypatch.setenv('HEALTH_ACTIONS_API_TOKEN','test-secret')
 source=NS(chat_id='-100',thread_id='7')
 raw=NS(from_user=NS(id=42),chat=NS(id=-100),message_thread_id=7,reply_to_message=NS(from_user=NS(id=765),chat=NS(id=-100),message_thread_id=7,message_id=99))
 event=NS(raw_message=raw,source=source,message_id='100')
 assert capture_projection_identity(event,NS(_bot=NS(id=765)))['context']['replyMessageId']=='99'
 raw.reply_to_message.from_user.id=123
 assert 'replyMessageId' not in capture_projection_identity(event,NS(_bot=NS(id=765)))['context']
 raw.reply_to_message.from_user.id=765;raw.reply_to_message.chat.id=-200
 assert 'replyMessageId' not in capture_projection_identity(event,NS(_bot=NS(id=765)))['context']
