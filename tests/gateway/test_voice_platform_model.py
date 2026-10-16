from types import SimpleNamespace
import pytest
import gateway.run as run


def source(name):
    return SimpleNamespace(platform=SimpleNamespace(value=name), chat_id='family', thread_id=None)


def test_voice_model_does_not_change_telegram_or_profile(monkeypatch):
    runner = object.__new__(run.GatewayRunner)
    runner._session_model_overrides = {}
    cfg = {'model': {'default': 'gpt-6.1-sol', 'provider': 'openai-codex',
                     'platforms': {'alice': {'model': 'gpt-6-luna', 'reasoning_effort': 'none'}}}}
    monkeypatch.setattr(run, '_resolve_runtime_agent_kwargs', lambda **kw: {'provider': 'openai-codex'})
    assert runner._resolve_session_agent_runtime(source=source('alice'), session_key='alice', user_config=cfg)[0] == 'gpt-6-luna'
    assert runner._resolve_session_agent_runtime(source=source('telegram'), session_key='telegram', user_config=cfg)[0] == 'gpt-6.1-sol'
    assert cfg['model']['default'] == 'gpt-6.1-sol'


def test_voice_reasoning_and_session_precedence(monkeypatch):
    runner = object.__new__(run.GatewayRunner)
    runner._session_reasoning_overrides = {}
    monkeypatch.setattr(run, '_load_gateway_runtime_config', lambda: {
        'model': {'platforms': {'alice': {'reasoning_effort': 'none'}}},
        'agent': {'reasoning_effort': 'medium'}})
    assert runner._resolve_session_reasoning_config(source=source('alice'), session_key='a') == {'enabled': False}
    assert runner._resolve_session_reasoning_config(source=source('telegram'), session_key='t')['effort'] == 'medium'
    runner._session_reasoning_overrides['a'] = {'enabled': True, 'effort': 'high'}
    assert runner._resolve_session_reasoning_config(source=source('alice'), session_key='a')['effort'] == 'high'
