import json
from dataclasses import replace
from pathlib import Path

import pytest
from gateway.memory_access import MemoryAccessContext, MemoryDestination
from tools.credential_vault_tool import credential_vault


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr('tools.credential_vault_tool.vault_root', lambda: tmp_path / 'vault')
    ctx = MemoryAccessContext(version=1, authorization_subject='principal:p_aaaaaaaaaaaaaaaa',
        principal_id='p_aaaaaaaaaaaaaaaa', actor_ref='a_verified', source_ref='m_verified',
        destination=MemoryDestination('group', 'telegram', '-123', '7'), profile_name='family',
        memory_scope='family', read_wings=('group.family',), write_wing='group.family',
        grants=(), policy_fingerprint='test')
    def call(action, context=ctx, **kw):
        return json.loads(credential_vault(dict(action=action, **kw), context=context))
    return call, ctx, tmp_path / 'vault'


def test_save_readback_and_encrypted_disk(vault):
    call, ctx, root = vault
    saved = call('save', label='Example teacher', login='demo@example.test', password='synthetic-pass-6789')
    assert saved['saved'] and 'password' not in saved
    assert call('get', label='example teacher')['account']['password'] == 'synthetic-pass-6789'
    assert call('list')['labels'] == ['Example teacher']
    for path in root.iterdir():
        assert b'synthetic-pass-6789' not in path.read_bytes()
        assert b'demo@example.test' not in path.read_bytes()
        assert path.stat().st_mode & 0o777 == 0o600
    assert root.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('kind', ['profile', 'chat', 'platform'])
def test_other_boundaries_cannot_read_or_delete(vault, kind):
    call, ctx, _ = vault
    assert call('save', label='Example', login='demo', password='synthetic')['success']
    other = (replace(ctx, profile_name='other-family') if kind == 'profile' else
             replace(ctx, destination=replace(ctx.destination, **({'chat_id':'-456'} if kind == 'chat' else {'platform':'discord'}))))
    assert call('get', context=other, label='Example')['error'] == 'not_found'
    assert call('list', context=other)['labels'] == []
    assert call('delete', context=other, label='Example')['deleted'] is False
    assert call('get', label='Example')['success']


def test_unverified_context_and_delete(vault):
    call, ctx, root = vault
    for other in [None, {}, replace(ctx, actor_ref='unknown'), replace(ctx, source_ref=None)]:
        assert call('save', context=other, label='Example', login='demo', password='synthetic')['error'] == 'verified_user_required'
    assert not root.exists()
    call('save', label='Example', login='demo', password='synthetic')
    assert call('delete', label='Example')['deleted']
    assert call('get', label='Example')['error'] == 'not_found'


@pytest.mark.parametrize('principal', ['p_bbbbbbbbbbbbbbbb', None])
def test_chat_participants_share_all_topics_even_without_fact_memory(vault, principal):
    call, ctx, _ = vault
    assert call('save', label='Example', login='demo', password='synthetic')['saved']
    other = replace(ctx, principal_id=principal, actor_ref='a_other_member',
                    authorization_subject='group_context:verified-chat',
                    source_ref='m_other_message', read_wings=(), write_wing=None,
                    destination=replace(ctx.destination, thread_id='new-topic'))
    assert call('list', context=other)['labels'] == ['Example']
    assert call('get', context=other, label='Example')['account']['password'] == 'synthetic'
    assert call('save', context=other, label='Second', login='member', password='synthetic')['saved']
    assert call('get', label='Second')['success']
    assert call('delete', context=other, label='Example')['deleted']
    assert call('get', label='Example')['error'] == 'not_found'


def test_dm_keeps_private_principal_boundary(vault):
    call, ctx, _ = vault
    dm = replace(ctx, destination=replace(ctx.destination, kind='dm'))
    assert call('save', context=dm, label='Private', login='demo', password='synthetic')['saved']
    other = replace(dm, principal_id='p_bbbbbbbbbbbbbbbb')
    assert call('get', context=other, label='Private')['error'] == 'not_found'
    assert call('get', context=replace(dm, principal_id=None), label='Private')['error'] == 'verified_user_required'
    assert call('get', label='Private')['error'] == 'not_found'


def test_telegram_get_requires_secure_transport_callback(vault):
    from types import SimpleNamespace
    from tools.credential_vault_tool import invoke_credential_vault
    call, ctx, _ = vault
    call('save', label='Example', login='demo', password='synthetic-secret')
    agent = SimpleNamespace(platform='telegram', _memory_access_context=ctx)
    result = invoke_credential_vault(agent, dict(action='get', label='Example'))
    assert json.loads(result)['error'] == 'secure_delivery_unavailable'
    assert 'synthetic-secret' not in result


def test_vault_available_in_restricted_family_toolset():
    # Family Telegram has an explicit tool list; it does not use hermes-cli.
    from model_tools import get_tool_definitions
    tools = get_tool_definitions(
        enabled_toolsets=['family-archive', 'web', 'credential-vault'],
        disabled_toolsets=['terminal', 'file', 'delegation', 'memory'], quiet_mode=True,
    )
    assert 'credential_vault' in {t['function']['name'] for t in tools}
    from model_tools import handle_function_call
    # Generic dispatch cannot supply a forged authorization context.
    result = json.loads(handle_function_call('credential_vault', {'action': 'list'}))
    assert 'must be handled by the agent loop' in result['error']


@pytest.mark.parametrize('mode', ['sequential', 'concurrent'])
@pytest.mark.parametrize('secure_telegram', [False, True])
def test_agent_execution_saves_and_reads_credentials(vault, monkeypatch, mode, secure_telegram):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    import threading
    from agent.agent_runtime_helpers import invoke_tool
    from agent.tool_executor import execute_tool_calls_sequential, execute_tool_calls_concurrent
    call, ctx, root = vault
    agent = MagicMock()
    agent.platform = 'telegram' if secure_telegram else 'cli'
    deliveries = []
    agent._credential_delivery_callback = deliveries.append
    agent._memory_access_context = ctx
    agent._trusted_memory_blocked_tools = frozenset()
    agent._interrupt_requested = False
    agent.quiet_mode = True
    agent.verbose_logging = False
    agent.session_id = 'vault-test'
    agent.tool_delay = 0
    agent._tool_guardrails.before_call.return_value.allows_execution = True
    agent._append_guardrail_observation.side_effect = lambda name, args, result, **kw: result
    agent._tool_result_content_for_active_model.side_effect = lambda name, result: result
    agent._subdirectory_hints.check_tool_call.return_value = ''
    agent._should_emit_quiet_tool_messages.return_value = False
    agent._tool_worker_threads_lock = threading.Lock()
    agent._tool_worker_threads = set()
    agent._invoke_tool.side_effect = lambda *a, **kw: invoke_tool(agent, *a, **kw)
    monkeypatch.setattr('hermes_cli.plugins.get_pre_tool_call_block_message', lambda *a, **kw: None)
    execute = execute_tool_calls_sequential if mode == 'sequential' else execute_tool_calls_concurrent
    def run(args):
        tc = SimpleNamespace(id='vault-call', function=SimpleNamespace(name='credential_vault', arguments=json.dumps(args)))
        messages = []
        execute(agent, SimpleNamespace(tool_calls=[tc]), messages, 'vault-test')
        return json.loads(messages[-1]['content'])
    result = run(dict(action='save', label='Example teacher', login='demo', password='synthetic-agent-password'))
    assert result['success'] and result['saved']
    result = run(dict(action='get', label='Example teacher'))
    if secure_telegram:
        assert result['delivery'] == 'telegram_native_spoiler'
        assert result['delete_after_seconds'] == 900
        assert 'synthetic-agent-password' not in json.dumps(result)
        assert 'account' not in result
        assert deliveries[0]['password'] == 'synthetic-agent-password'
    else:
        assert result['account']['password'] == 'synthetic-agent-password'
    assert run(dict(action='delete', label='Example teacher'))['deleted']
    assert run(dict(action='get', label='Example teacher'))['error'] == 'not_found'
