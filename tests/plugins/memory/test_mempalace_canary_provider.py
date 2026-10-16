from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.memory_access import MemoryAccessContext, MemoryDestination
from plugins.memory import load_memory_provider
from plugins.memory.mempalace.capability_provider import MemPalaceMemoryProvider as ScopedProvider


@pytest.fixture
def scoped_setup(tmp_path):
    vector = json.loads((Path(__file__).parents[2]/'fixtures/memory_capability_v1.json').read_text())
    claims = vector['claims']
    context = MemoryAccessContext(version=1,authorization_subject=claims['sub'],principal_id=claims['principal_id'],
        actor_ref=claims['actor_ref'],source_ref='m_source0000000001',destination=MemoryDestination(**claims['destination']),
        profile_name='personal',memory_scope='personal',read_wings=tuple(claims['read_wings']),
        write_wing='private.p_aaaaaaaaaaaaaaaa.personal',grants=(),policy_fingerprint=claims['policy_fingerprint'])
    env = tmp_path/'client.env'
    env.write_text('\n'.join(k+'='+v for k,v in vector['environment'].items())+'\n'); env.chmod(0o600)
    return context, {'credential_file':str(env)}


def test_existing_plugin_switches_only_when_explicit_context(scoped_setup,monkeypatch):
    ctx,options=scoped_setup
    legacy=load_memory_provider('mempalace')
    assert not isinstance(legacy,ScopedProvider)
    scoped=legacy.for_access_context(ctx,options=options)
    assert isinstance(scoped,ScopedProvider) and scoped.is_available()
    schemas=scoped.get_tool_schemas()
    assert 'mempalace_add_drawer' in {x['name'] for x in schemas}
    assert all('palace' not in x['parameters']['properties'] for x in schemas)
    assert not isinstance(load_memory_provider('mempalace'),ScopedProvider)


def test_invalid_or_insecure_credentials_never_fallback(scoped_setup):
    ctx,options=scoped_setup; path=Path(options['credential_file'])
    path.chmod(0o644)
    with pytest.raises(ValueError): load_memory_provider('mempalace').for_access_context(ctx,options=options)
    path.chmod(0o600); path.write_text('MEMPALACE_BEARER_TOKEN=invalid\n')
    with pytest.raises(ValueError): load_memory_provider('mempalace').for_access_context(ctx,options=options)


def construct_agent(context,options):
    from run_agent import AIAgent
    cfg={'memory':{'provider':'mempalace','memory_enabled':True,'user_profile_enabled':True},'agent':{}}
    tools=[{'type':'function','function':{'name':'memory','parameters':{'type':'object'}}},
           {'type':'function','function':{'name':'query_chat_agent','parameters':{'type':'object'}}}]
    with patch('hermes_cli.config.load_config',return_value=cfg), patch('agent.model_metadata.get_model_context_length',return_value=204800), \
         patch('run_agent.get_tool_definitions',return_value=tools), patch('run_agent.check_toolset_requirements',return_value={}), patch('run_agent.OpenAI'):
        return AIAgent(api_key='unit-test-not-secret',base_url='https://openrouter.ai/api/v1',quiet_mode=True,
             skip_context_files=True,platform='telegram',user_id='12345',chat_id='12345',chat_type='dm',
             enabled_toolsets=['memory'],memory_access_context=context,memory_provider_override='mempalace',memory_provider_options=options)


def test_real_agent_constructor_uses_remote_schema_no_builtin_files(scoped_setup):
    ctx,options=scoped_setup
    with patch('tools.memory_tool.MemoryStore.load_from_disk') as old_load:
        agent=construct_agent(ctx,options)
    assert isinstance(agent._memory_manager.providers[0],ScopedProvider)
    assert agent._memory_store is None and not agent._memory_enabled and not agent._user_profile_enabled
    old_load.assert_not_called()
    assert 'memory' not in agent.valid_tool_names and 'query_chat_agent' not in agent.valid_tool_names
    assert 'mempalace_add_drawer' in agent.valid_tool_names
    assert agent._memory_manager.providers[0]._context() == ctx


def test_real_agent_constructor_failure_is_not_legacy(scoped_setup):
    ctx,options=scoped_setup
    Path(options['credential_file']).write_text('invalid=1\n')
    with pytest.raises(RuntimeError,match='Trusted memory provider initialization failed'):
        construct_agent(ctx,options)


def test_sync_turn_is_explicitly_disabled(scoped_setup):
    ctx,options=scoped_setup; provider=load_memory_provider('mempalace').for_access_context(ctx,options=options)
    with patch.object(provider._client,'ingest') as ingest:
        provider.sync_turn('test user','test assistant')
    ingest.assert_not_called()
