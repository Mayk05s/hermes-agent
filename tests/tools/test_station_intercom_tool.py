import json
import sqlite3
from unittest.mock import AsyncMock
import pytest
from gateway import station_intercom as service
from gateway.session_context import set_session_vars,clear_session_vars
from tools import station_intercom_tool as tool

@pytest.fixture
def setup(monkeypatch,tmp_path):
    cfg={'enabled':True,'database':str(tmp_path/'state.db'),'family_chat_id':'-100123','default_station':'kitchen',
         'sender_names':{'179555559':'Михаил'},'stations':{'kitchen':{'name':'Кухня','aliases':['на кухне'],'sensor':'sensor.test','event_station':'mini2'},'children':{'name':'Детская','aliases':['в детской'],'sensor':'sensor.test2','event_station':'station2'}}}
    monkeypatch.setattr(service,'settings',lambda:cfg)
    speak=AsyncMock(return_value=True);monkeypatch.setattr(service,'ask',speak)
    tokens=set_session_vars(platform='telegram',profile_name='family-chat',chat_id='-100123',thread_id='359',
        user_id='',requester_user_id='179555559',user_name='',message_id='5079',user_request='Узнай через колонку, когда ужин')
    try:yield cfg,speak
    finally:clear_session_vars(tokens)

def test_real_model_tool_discovery_and_dispatch(setup):
    from model_tools import get_tool_definitions,handle_function_call
    cfg,speak=setup
    definitions=get_tool_definitions(enabled_toolsets=['station-intercom'],quiet_mode=True)
    assert 'station_intercom' in [x['function']['name'] for x in definitions]
    result=json.loads(handle_function_call('station_intercom',{'question':'Когда ужин?','station':'Детская'},enabled_tools=['station_intercom'],enabled_toolsets=['station-intercom'],skip_pre_tool_call_hook=True))
    assert result['success'] and result['spoken']
    row=speak.call_args.args[1]
    assert (row['chat'],row['thread'],row['anchor'],row['station'],row['sender_name'])==('-100123','359','5079','children','Михаил')
    assert service.spoken_question(row)=='Михаил спрашивает: «Когда ужин?»'
    result=json.loads(tool.station_intercom({'question':'Когда ужин?','station':'Детская'}))
    assert speak.await_count==1

def test_other_chats_and_voice_cannot_initiate(setup):
    cfg,speak=setup
    for kwargs in [{'chat_id':'-999'},{'platform':'alice'},{'profile_name':'personal'}]:
        from gateway.session_context import override_session_env
        from contextlib import ExitStack
        mapping={'chat_id':'CHAT_ID','platform':'PLATFORM','profile_name':'PROFILE_NAME'}
        with ExitStack() as stack:
            for k,v in kwargs.items():stack.enter_context(override_session_env('HERMES_SESSION_'+mapping[k],v))
            assert json.loads(tool.station_intercom({'question':'Тест'}))['error']=='allowed_telegram_source_required'
    speak.assert_not_called()

@pytest.mark.parametrize('user_text',['Не отправляй старый вопрос','Не надо досылать сообщение','Сейчас уже его досылать не надо'])
def test_explicit_no_send_is_enforced(setup,user_text):
    from gateway.session_context import override_session_env
    cfg,speak=setup
    with override_session_env('HERMES_SESSION_USER_REQUEST',user_text):
        assert json.loads(tool.station_intercom({'question':'Старый вопрос'}))['error']=='current_request_prohibits_sending'
    speak.assert_not_called()

def test_model_cannot_supply_author_destination_or_message_id(setup):
    cfg,speak=setup
    assert json.loads(tool.station_intercom({'question':'Привет','chat_id':'-other'}))['error']=='invalid_fields'
    speak.assert_not_called()

def test_failure_never_becomes_spoken_success(setup):
    cfg,speak=setup;speak.side_effect=TimeoutError
    result=json.loads(tool.station_intercom({'question':'Когда ужин?'}))
    assert result['spoken'] is False and result['success'] is False
    assert result['status']=='unconfirmed'

def test_source_message_id_is_required(setup):
    from gateway.session_context import override_session_env
    cfg,speak=setup
    with override_session_env('HERMES_SESSION_MESSAGE_ID',''):
        result=json.loads(tool.station_intercom({'question':'Привет'}))
    assert result['error']=='authenticated_current_message_required'
    speak.assert_not_called()


@pytest.mark.parametrize('chat,thread',[('179555559',''),('-100456','42')])
def test_registered_tool_works_in_personal_dm_and_tripio(setup,chat,thread):
    from gateway.session_context import override_session_env
    from contextlib import ExitStack
    cfg,speak=setup
    cfg['telegram_sources']=[{'chat_id':'-100123','profile':'family-chat'},{'chat_id':'179555559','profile':'personal','user_ids':['179555559']},{'chat_id':'-100456','profile':'personal'}]
    with ExitStack() as stack:
        for key,value in {'PROFILE_NAME':'personal','CHAT_ID':chat,'THREAD_ID':thread}.items():
            stack.enter_context(override_session_env('HERMES_SESSION_'+key,value))
        from model_tools import handle_function_call
        result=json.loads(handle_function_call('station_intercom',{'question':'Ты дома?'},enabled_tools=['station_intercom'],enabled_toolsets=['station-intercom'],skip_pre_tool_call_hook=True))
    assert result['success'] and result['spoken']
    row=speak.call_args.args[1]
    assert (row['chat'],row['thread'],row['sender_name'])==(chat,thread,'Михаил')
