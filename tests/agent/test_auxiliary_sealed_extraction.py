from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest

from agent.auxiliary_client import call_llm


@pytest.mark.parametrize('error', [ConnectionError('Synthetic connection failure'), RuntimeError('Payment Required: insufficient credits')])
def test_extraction_provider_never_falls_back(error):
    client = MagicMock()
    client.base_url = 'https://chatgpt.com/backend-api/codex'
    client.chat.completions.create.side_effect = error
    with patch('agent.auxiliary_client._resolve_task_provider_model', return_value=('openai-codex', 'gpt-5.6-sol', None, None, None)), \
         patch('agent.auxiliary_client._get_cached_client', return_value=(client, 'gpt-5.6-sol')), \
         patch('agent.auxiliary_client._get_task_extra_body', return_value={}), \
         patch('agent.auxiliary_client._recoverable_pool_provider', return_value=None), \
         patch('agent.auxiliary_client._try_payment_fallback') as alternate:
        with pytest.raises(type(error)):
            call_llm(provider='openai-codex', model='gpt-5.6-sol', messages=[{'role': 'user', 'content': 'Fictional color is blue.'}],
                     tools=[], no_tools=True, allow_fallback=False)
        alternate.assert_not_called()


def test_no_tools_survives_config_and_caller_extra_body():
    client = MagicMock()
    client.base_url = 'https://chatgpt.com/backend-api/codex'
    client.chat.completions.create.return_value = NS(choices=[NS(message=NS(content='{"facts":[]}', tool_calls=[]))])
    injected = {'tools': [{'type': 'function', 'function': {'name': 'forbidden'}}], 'tool_choice': 'required', 'parallel_tool_calls': True,
                'functions': [{'name': 'forbidden'}], 'function_call': 'auto'}
    with patch('agent.auxiliary_client._resolve_task_provider_model', return_value=('openai-codex', 'gpt-5.6-sol', None, None, None)), \
         patch('agent.auxiliary_client._get_cached_client', return_value=(client, 'gpt-5.6-sol')), \
         patch('agent.auxiliary_client._get_task_extra_body', return_value=dict(injected)):
        call_llm(provider='openai-codex', model='gpt-5.6-sol', messages=[{'role': 'user', 'content': 'Fictional color is blue.'}],
                 tools=injected['tools'], extra_body=injected, no_tools=True, allow_fallback=False)
    wire = client.chat.completions.create.call_args.kwargs
    assert not wire.get('tools')
    assert not any(k in wire.get('extra_body', {}) for k in injected)
