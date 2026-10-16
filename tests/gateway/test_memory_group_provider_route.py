import base64
from dataclasses import replace
import importlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.memory_routing import resolve_memory_provider_route


@pytest.fixture
def group_setup(tmp_path):
    policy={'version':1,'default_private_zone':'personal','default_group_zone':'shared','principals':[],
        'groups':[{'id':'g_'+name,'platform':'telegram','chat_id':'-100123','thread_id':tid,'zone':zone}
            for name,tid,zone in [('plan','313','planning'),('box','6827','boxmap'),('root',None,'shared')]]}
    policy_file=tmp_path/'policy.json';policy_file.write_text(json.dumps(policy))
    key=tmp_path/'opaque.key';key.write_text(base64.urlsafe_b64encode(b'k'*32).rstrip(b'=').decode())
    credentials=tmp_path/'client.env'
    vector=json.loads((Path(__file__).parents[1]/'fixtures/memory_capability_v1.json').read_text())
    credentials.write_text('\n'.join(k+'='+v for k,v in vector['environment'].items())+'\n');credentials.chmod(0o600)
    route={'enabled':True,'destination_kind':'group','provider':'mempalace','match':{'platform':'telegram','chat_id':'-100123'},
        'topic_scopes':{'313':'planning','6827':'boxmap','':'shared'},'policy_file':str(policy_file),'opaque_id_key_file':str(key),'credential_file':str(credentials)}
    source=SessionSource(platform=Platform.TELEGRAM,chat_id='-100123',chat_type='group',user_id='unpaired-user',thread_id='313',profile_name='personal',message_id='10')
    return source,{'memory':{'provider':'mempalace','memory_enabled':True,'user_profile_enabled':True,'trusted_provider_routes':[route]}}


def resolve(source,cfg,authorized=True,verified=False):
    return resolve_memory_provider_route(source,cfg,request_authorized=authorized,principal_verified=verified)


@pytest.mark.parametrize('tid,zone',[('313','planning'),('6827','boxmap'),(None,'shared')])
def test_known_group_routes_bind_exact_destination(group_setup,tid,zone):
    source,cfg=group_setup
    route=resolve(replace(source,thread_id=tid,memory_scope='personal'),cfg)
    ctx=route.context
    assert ctx.destination_kind=='group' and ctx.principal_id is None
    assert ctx.authorization_subject.startswith('group_context:')
    assert len(ctx.read_wings)==1 and ctx.read_wings[0].endswith('.'+zone)
    assert ctx.write_wing==ctx.read_wings[0] and ctx.grants==()


def test_unknown_topic_is_empty_never_legacy(group_setup):
    from plugins.memory.mempalace.capability_client import _validate_context_boundary,MemPalaceClientError
    source,cfg=group_setup
    unknown=resolve(replace(source,thread_id='new-topic',memory_scope='personal'),cfg)
    assert unknown and unknown.context.read_wings==() and unknown.context.write_wing is None
    assert unknown.context.destination.kind=='group'
    with pytest.raises(MemPalaceClientError):_validate_context_boundary(unknown.context)


@pytest.mark.parametrize('change',[{'chat_id':'-other'},{'chat_id':'123','chat_type':'dm'},{'platform':Platform.DISCORD}])
def test_other_destinations_unchanged(group_setup,change):
    source,cfg=group_setup
    assert resolve(replace(source,**change),cfg) is None


def test_authorization_missing_or_duplicate_fails_closed(group_setup):
    source,cfg=group_setup
    with pytest.raises(ValueError):resolve(source,cfg,authorized=False)
    cfg['memory']['trusted_provider_routes']*=2
    with pytest.raises(ValueError):resolve(source,cfg)


def test_group_fingerprint_separates_topics_actor_and_write_rights(group_setup):
    source,cfg=group_setup
    route=resolve(source,cfg)
    assert route.cache_fingerprint==resolve(replace(source,message_id='11'),cfg).cache_fingerprint
    for change in [{'thread_id':'6827'},{'thread_id':None},{'user_id':'other'},{'message_id':None},{'profile_name':'health'}]:
        assert route.cache_fingerprint!=resolve(replace(source,**change),cfg).cache_fingerprint


@pytest.mark.asyncio
async def test_actual_gateway_group_prompt_and_native_dispatch(group_setup,monkeypatch,tmp_path):
    from run_agent import AIAgent
    from gateway.run import GatewayRunner
    from agent.tool_executor import execute_tool_calls_sequential,execute_tool_calls_concurrent
    source,cfg=group_setup
    cfg.update({'display':{'tool_progress':'off','interim_assistant_messages':False},'model':{'default':'unit-test-model'},'agent':{}})
    memories=tmp_path/'memories';memories.mkdir()
    for name in ['MEMORY.md','USER.md']:(memories/name).write_text('PRIVATE_PROMPT_CANARY_NEVER_SHARE')
    runner=object.__new__(GatewayRunner)
    for name,value in {'adapters':{},'_voice_mode':{},'_prefill_messages':[],'_ephemeral_system_prompt':'','_reasoning_config':None,
        '_provider_routing':{},'_fallback_model':None,'_session_db':None,'_running_agents':{},'_session_run_generation':{},'_cleanup_progress_ids_by_run':{},
        '_service_tier':None,'_agent_cache':{},'_agent_cache_lock':threading.Lock(),'hooks':SimpleNamespace(loaded_hooks=False),
        'config':SimpleNamespace(thread_sessions_per_user=False,group_sessions_per_user=False,stt_enabled=False)}.items():setattr(runner,name,value)
    monkeypatch.setattr(runner,'_get_proxy_url',lambda:None)
    monkeypatch.setattr(runner,'_profile_home_for_name',lambda name:tmp_path)
    monkeypatch.setattr(runner,'_load_profile_config_for_name',lambda name:cfg)
    monkeypatch.setattr(runner,'_profile_config_with_chat_settings',lambda config,source:config)
    monkeypatch.setattr(runner,'_is_user_authorized',lambda source:True)
    monkeypatch.setattr(runner,'_pairing_store_for_source',lambda source:SimpleNamespace(is_approved=lambda p,u:False))
    monkeypatch.setattr(runner,'_resolve_turn_agent_config',lambda message,model,runtime:{'model':'unit-test-model','runtime':{'api_key':'unit-test-key','base_url':'https://openrouter.ai/api/v1'}})
    monkeypatch.setattr(importlib.import_module('gateway.run'),'_hermes_home',tmp_path)
    monkeypatch.setattr(importlib.import_module('gateway.run'),'_resolve_runtime_agent_kwargs',lambda:{'api_key':'unit-test-key','base_url':'https://openrouter.ai/api/v1'})
    monkeypatch.setattr(importlib.import_module('gateway.run'),'_resolve_gateway_model',lambda *a,**kw:'unit-test-model')
    monkeypatch.setattr(importlib.import_module('hermes_cli.config'),'load_config',lambda:cfg)
    monkeypatch.setattr(importlib.import_module('hermes_cli.tools_config'),'_get_platform_tools',lambda *a:['memory','session_search'])
    monkeypatch.setattr(importlib.import_module('run_agent'),'OpenAI',Mock())
    blocked={'memory','recall_access','query_chat_agent'}
    schemas=[{'type':'function','function':{'name':name,'description':'unit-test','parameters':{'type':'object','properties':{}}}} for name in blocked | {"session_search"}]
    monkeypatch.setattr(importlib.import_module('run_agent'),'get_tool_definitions',lambda **kw:schemas)
    monkeypatch.setattr(importlib.import_module('run_agent'),'check_toolset_requirements',lambda *a,**kw:{})
    monkeypatch.setattr(importlib.import_module('agent.model_metadata'),'get_model_context_length',lambda *a,**kw:204800)
    observed=[]
    def conversation(agent,message,**kwargs):
        observed.append(agent)
        assert agent._memory_store is None and not agent._memory_enabled and not agent._user_profile_enabled
        assert blocked.isdisjoint(agent.valid_tool_names)
        assert 'session_search' in agent.valid_tool_names
        assert 'session_search' not in agent._trusted_memory_blocked_tools
        # Exercise the actual group-memory agent, not just config/schema resolution.
        from hermes_state import SessionDB
        from gateway.session_context import set_session_vars, clear_session_vars
        history_db = SessionDB(tmp_path / 'state.db')
        try:
            for sid, profile in [('same-profile', 'personal'), ('private-other-profile', 'other')]:
                history_db.create_session(sid, source='telegram', access_scope=json.dumps({'origin': {
                    'platform': 'telegram', 'chat_id': '-other-chat', 'thread_id': '999', 'profile_name': profile}}))
                if not history_db.get_messages(sid):
                    history_db.append_message(sid, role='user', content='profilehistorytoken')
            agent._session_db = history_db
            tokens = set_session_vars(platform='telegram', chat_id='-100123', thread_id='313', profile_name='personal')
            try:
                found = json.loads(agent._invoke_tool('session_search', {'query': 'profilehistorytoken', 'offset': 0}, 'synthetic', pre_tool_block_checked=True))
                assert found['success']
                assert {m['session_id'] for m in found['messages']} == {'same-profile'}
            finally:
                clear_session_vars(tokens)
                agent._session_db = None
        finally:
            history_db.close()
        prompt=agent._build_system_prompt()
        assert 'PRIVATE_PROMPT_CANARY_NEVER_SHARE' not in prompt
        assert 'mempalace_add_drawer' in agent.valid_tool_names
        for name in blocked:
            assert 'trusted memory boundary' in agent._invoke_tool(name,{},'synthetic',pre_tool_block_checked=True)
        agent.quiet_mode=True
        tools=[SimpleNamespace(id='unit-'+name,function=SimpleNamespace(name=name,arguments='{}')) for name in blocked]
        messages=[]
        execute_tool_calls_sequential(agent,SimpleNamespace(tool_calls=tools),messages,'synthetic')
        assert len(messages)==len(blocked)
        assert all('trusted memory boundary' in item['content'] for item in messages)
        messages=[]
        execute_tool_calls_concurrent(agent,SimpleNamespace(tool_calls=tools),messages,'synthetic')
        assert len(messages)==len(blocked)
        assert all('trusted memory boundary' in item['content'] for item in messages)
        return {'final_response':'synthetic-ok','messages':[],'api_calls':0,'completed':True}
    monkeypatch.setattr(AIAgent,'run_conversation',conversation)
    for number in ['10','11']:
        result=await runner._run_agent(message='synthetic validation',context_prompt='',history=[],source=replace(source,message_id=number),session_id='group-test',session_key='group-test')
        assert result['final_response']=='synthetic-ok'
    assert observed[0] is observed[1]
