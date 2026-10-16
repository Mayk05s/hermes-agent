import base64
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.memory_routing import resolve_memory_provider_route, verified_memory_identity


@pytest.fixture
def route_setup(tmp_path):
    policy = {'version': 1, 'default_private_zone': 'personal', 'default_group_zone': 'shared', 'groups': [], 'principals': [{
        'id': 'p_aaaaaaaaaaaaaaaa', 'identities': [{'platform': 'telegram', 'user_id': '12345'}],
        'dm_destinations': [{'platform': 'telegram', 'chat_id': '12345'}],
        'private_zones': ['personal'], 'group_memberships': [], 'dm_rules': []}]}
    policy_file = tmp_path/'policy.json'; policy_file.write_text(json.dumps(policy))
    key = tmp_path/'opaque.key'; key.write_text(base64.urlsafe_b64encode(b'k'*32).rstrip(b'=').decode())
    raw = {'enabled': True, 'provider': 'mempalace', 'match': {'platform': 'telegram', 'user_id': '12345', 'chat_id': '12345', 'chat_type': 'dm', 'profile_name': 'personal', 'thread_id': ''},
           'policy_file': str(policy_file), 'opaque_id_key_file': str(key), 'credential_file': str(tmp_path/'client.env'), 'memory_scope': 'personal'}
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='12345', user_id='12345', chat_type='dm', profile_name='personal', message_id='5')
    return source, {'memory': {'provider': 'legacy', 'trusted_provider_route': raw}}


def resolve(source, config, **kwargs):
    return resolve_memory_provider_route(source, config, request_authorized=kwargs.get('authorized',True), principal_verified=kwargs.get('verified',True))


def test_exact_verified_dm_uses_own_private_wing(route_setup):
    source, cfg = route_setup; route = resolve(source,cfg)
    assert route.context.read_wings == ('private.p_aaaaaaaaaaaaaaaa.personal',)
    assert route.context.write_wing == route.context.read_wings[0]
    assert route.context.grants == ()


@pytest.mark.parametrize('changed', [{'chat_id':'-1003735932411','chat_type':'group'}, {'chat_type':'private'}, {'user_id':'54321'}, {'profile_name':'health'}, {'thread_id':'3'}, {'platform':Platform.DISCORD}, {'chat_id':'54321'}])
def test_nonmatching_routes_keep_existing_provider(route_setup,changed):
    source,cfg = route_setup
    assert resolve(replace(source,**changed),cfg) is None
    assert cfg['memory']['provider'] == 'legacy'


@pytest.mark.parametrize('auth', [(False,True),(True,False),(False,False)])
def test_match_cannot_downgrade_when_unverified(route_setup,auth):
    source,cfg = route_setup
    with pytest.raises(ValueError): resolve(source,cfg,authorized=auth[0],verified=auth[1])


def test_identity_never_uses_allow_all_or_chat_allowlist(route_setup):
    source,_ = route_setup
    broad = {'TELEGRAM_ALLOW_ALL_USERS':'true','TELEGRAM_GROUP_ALLOWED_CHATS':'12345','TELEGRAM_ALLOWED_USERS':'*','GATEWAY_ALLOW_ALL_USERS':'true'}
    assert not verified_memory_identity(source,environ=broad)
    assert verified_memory_identity(source,environ={'TELEGRAM_ALLOWED_USERS':'12345'})
    assert not verified_memory_identity(replace(source,is_bot=True),environ={'TELEGRAM_ALLOWED_USERS':'12345'})


def test_disabled_route_and_missing_policy_are_distinct(route_setup):
    source,cfg = route_setup
    cfg['memory']['trusted_provider_route']['policy_file'] = '/does/not/exist'
    with pytest.raises(FileNotFoundError): resolve(source,cfg)
    cfg['memory']['trusted_provider_route']['enabled'] = False
    assert resolve(source,cfg) is None


def test_cache_stable_per_message_but_new_provenance(route_setup):
    source,cfg = route_setup; first = resolve(source,cfg); second = resolve(replace(source,message_id='6'),cfg)
    assert first.cache_fingerprint == second.cache_fingerprint
    assert first.context.source_ref != second.context.source_ref
    missing = resolve(replace(source,message_id=None),cfg)
    assert missing.context.write_wing is None
    assert missing.cache_fingerprint != first.cache_fingerprint


def test_real_gateway_method_uses_auth_and_message_fallback(route_setup,monkeypatch):
    from gateway.run import GatewayRunner
    source,cfg = route_setup
    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = Mock(return_value=True)
    runner._pairing_store_for_source = Mock(return_value=SimpleNamespace(is_approved=lambda p,u: True))
    monkeypatch.delenv('TELEGRAM_ALLOWED_USERS',raising=False)
    route = runner._memory_provider_route_for_source(replace(source,message_id=None),cfg,'987')
    assert route.context.source_ref and route.context.write_wing
    runner._is_user_authorized.assert_called_once()


def test_unconfigured_transport_does_not_enter_memory_auth(route_setup):
    from gateway.run import GatewayRunner
    source, cfg = route_setup
    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = Mock(side_effect=AssertionError('Unrouted transport authenticated'))
    runner._pairing_store_for_source = Mock(side_effect=AssertionError('Unrouted pairing store opened'))
    cfg['memory']['trusted_shared_deny_route'] = dict(enabled=True, platform='telegram')
    assert runner._memory_provider_route_for_source(replace(source, platform=Platform.LOCAL), cfg, '987') is None
    runner._is_user_authorized.assert_not_called()


@pytest.mark.parametrize('memory_config', [
    {'trusted_shared_deny_route': {'platform': 'telegram', 'enabled': 'true'}},
    {'trusted_provider_routes': 'invalid'},
])
def test_gateway_rejects_malformed_memory_routes_before_fallback(route_setup, memory_config):
    from gateway.run import GatewayRunner
    source, _ = route_setup
    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = Mock(return_value=True)
    runner._pairing_store_for_source = Mock(return_value=SimpleNamespace(is_approved=lambda *a: True))
    with pytest.raises(ValueError):
        runner._memory_provider_route_for_source(replace(source, chat_type='group'), {'memory': memory_config}, '987')


def test_refresh_context_rejects_new_rights(route_setup):
    from run_agent import AIAgent
    source,cfg = route_setup; route = resolve(source,cfg)
    agent = object.__new__(AIAgent); provider=Mock()
    agent._memory_access_context=route.context; agent._memory_manager=SimpleNamespace(providers=[provider])
    refreshed=replace(route.context,source_ref='m_newsource')
    agent.set_memory_access_context(refreshed)
    provider.set_access_context.assert_called_once_with(refreshed)
    with pytest.raises(ValueError): agent.set_memory_access_context(replace(refreshed,read_wings=()))


@pytest.mark.asyncio
@pytest.mark.parametrize('secure_credentials', [False, True])
async def test_actual_gateway_constructs_scoped_agent_and_reuses_with_fresh_provenance(route_setup, monkeypatch, tmp_path, secure_credentials):
    import importlib
    import threading
    from pathlib import Path
    from run_agent import AIAgent
    from gateway.run import GatewayRunner
    source,cfg=route_setup
    vector=json.loads((Path(__file__).parents[1]/'fixtures/memory_capability_v1.json').read_text())
    credentials=Path(cfg['memory']['trusted_provider_route']['credential_file'])
    credentials.write_text('\n'.join(k+'='+v for k,v in vector['environment'].items())+'\n');credentials.chmod(0o600)
    cfg.update({'display':{'tool_progress':'off','interim_assistant_messages':False},'model':{'default':'unit-test-model'},'agent':{}})
    runner=object.__new__(GatewayRunner)
    for name,value in {'adapters':{},'_voice_mode':{},'_prefill_messages':[],'_ephemeral_system_prompt':'','_reasoning_config':None,
        '_provider_routing':{},'_fallback_model':None,'_session_db':None,'_running_agents':{},'_session_run_generation':{},'_cleanup_progress_ids_by_run':{},
        '_service_tier':None,'_agent_cache':{},'_agent_cache_lock':threading.Lock(),'hooks':SimpleNamespace(loaded_hooks=False),
        'config':SimpleNamespace(thread_sessions_per_user=False,group_sessions_per_user=False,stt_enabled=False)}.items(): setattr(runner,name,value)
    if secure_credentials:
        from gateway.config import PlatformConfig
        from gateway.platforms.telegram import TelegramAdapter
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic-token'))
        adapter._bot = SimpleNamespace(id=123)
        runner.adapters[Platform.TELEGRAM] = adapter
        monkeypatch.setattr('tools.credential_vault_tool.vault_root', lambda: tmp_path / 'vault')
    monkeypatch.setattr(runner,'_get_proxy_url',lambda:None)
    monkeypatch.setattr(runner,'_profile_home_for_name',lambda name:tmp_path)
    monkeypatch.setattr(runner,'_load_profile_config_for_name',lambda name:cfg)
    monkeypatch.setattr(runner,'_profile_config_with_chat_settings',lambda config,source:config)
    monkeypatch.setattr(runner,'_is_user_authorized',lambda source:True)
    monkeypatch.setattr(runner,'_pairing_store_for_source',lambda source:SimpleNamespace(is_approved=lambda p,u:True))
    monkeypatch.setattr(runner,'_resolve_turn_agent_config',lambda message,model,runtime:{'model':'unit-test-model','runtime':{'api_key':'unit-test-key','base_url':'https://openrouter.ai/api/v1'}})
    monkeypatch.setattr(importlib.import_module('gateway.run'),'_hermes_home',tmp_path)
    monkeypatch.setattr(importlib.import_module('gateway.run'),'_resolve_runtime_agent_kwargs',lambda:{'api_key':'unit-test-key','base_url':'https://openrouter.ai/api/v1'})
    monkeypatch.setattr(importlib.import_module('gateway.run'),'_resolve_gateway_model',lambda *a,**kw:'unit-test-model')
    monkeypatch.setattr(importlib.import_module('hermes_cli.config'),'load_config',lambda:cfg)
    monkeypatch.setattr(importlib.import_module('hermes_cli.tools_config'),'_get_platform_tools',lambda *a:['memory'])
    monkeypatch.setattr(importlib.import_module('run_agent'),'OpenAI',Mock())
    monkeypatch.setattr(importlib.import_module('run_agent'),'get_tool_definitions',lambda **kw:[])
    monkeypatch.setattr(importlib.import_module('run_agent'),'check_toolset_requirements',lambda *a,**kw:{})
    monkeypatch.setattr(importlib.import_module('agent.model_metadata'),'get_model_context_length',lambda *a,**kw:204800)
    observed=[]
    def conversation(agent,message,**kwargs):
        observed.append((agent,agent._memory_access_context.source_ref))
        assert agent._memory_store is None
        assert 'mempalace_add_drawer' in agent.valid_tool_names
        if secure_credentials:
            from tools.credential_vault_tool import credential_vault
            from agent.agent_runtime_helpers import invoke_tool
            assert json.loads(credential_vault(dict(action='save', label='Example',
                login='synthetic-login', password='synthetic-private-password'), context=agent._memory_access_context))['saved']
            receipt = invoke_tool(agent, 'credential_vault', dict(action='get', label='Example'), 'test')
            assert 'synthetic-private-password' not in receipt
            assert json.loads(receipt)['delivery'] == 'telegram_native_spoiler'
        return {'final_response':'synthetic-ok','messages':[],'api_calls':0,'completed':True}
    monkeypatch.setattr(AIAgent,'run_conversation',conversation)
    for number in ('10','11'):
        result=await runner._run_agent(message='synthetic validation',context_prompt='',history=[],source=replace(source,message_id=number),session_id='canary-test',session_key='canary-test')
        assert result['final_response']=='synthetic-ok'
        if secure_credentials:
            from cryptography.fernet import Fernet
            assert 'synthetic-private-password' not in json.dumps(result)
            token, = result['credential_deliveries']
            payload = json.loads(Fernet((tmp_path / 'vault' / 'key').read_bytes()).decrypt(token))
            assert payload['account']['password'] == 'synthetic-private-password'
            assert payload['chat_id'] == source.chat_id
    assert observed[0][0] is observed[1][0]
    assert observed[0][1] != observed[1][1]
