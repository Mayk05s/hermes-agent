from types import SimpleNamespace

from agent.agent_runtime_helpers import copy_reasoning_content_for_api


def _agent(needs_reasoning_echo: bool):
    return SimpleNamespace(
        _needs_thinking_reasoning_pad=lambda: needs_reasoning_echo,
    )


def test_reasoning_content_not_replayed_to_plain_openai_compatible_provider():
    source = {
        "role": "assistant",
        "content": "done",
        "reasoning": "provider-private reasoning",
    }
    api_msg = dict(source)

    copy_reasoning_content_for_api(_agent(False), source, api_msg)

    assert "reasoning_content" not in api_msg


def test_existing_reasoning_content_stripped_for_plain_openai_compatible_provider():
    source = {
        "role": "assistant",
        "content": "done",
        "reasoning_content": "provider-private reasoning",
    }
    api_msg = dict(source)

    copy_reasoning_content_for_api(_agent(False), source, api_msg)

    assert "reasoning_content" not in api_msg


def test_reasoning_content_replayed_for_provider_that_requires_echo():
    source = {
        "role": "assistant",
        "content": "done",
        "reasoning": "provider-required reasoning",
    }
    api_msg = dict(source)

    copy_reasoning_content_for_api(_agent(True), source, api_msg)

    assert api_msg["reasoning_content"] == "provider-required reasoning"
