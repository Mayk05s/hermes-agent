import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.memory_ingress import capture_authenticated_memory, matching_input
from tests.plugins.memory.test_mempalace_fact_pipeline import context


def event(number='1', text='My preferred drink is tea.', forwarded=False):
    raw = NS(from_user=NS(id=123456, is_bot=False), chat=NS(id=-12345), message_id=number,
             message_thread_id=None, text=text, caption=None, forward_origin=forwarded)
    return NS(raw_message=raw, authenticated_user_request_text=text, text=text, message_id=number)


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    seen = []
    class Provider:
        def for_access_context(self, ctx, options):
            self.context = ctx
            return self
        def on_authenticated_event(self, bound):
            seen.append(bound)
            return {'test': bound.source.message_id}
    import plugins.memory
    monkeypatch.setattr(plugins.memory, 'load_memory_provider', lambda _: Provider())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='-12345', chat_type='group', profile_name='test')
    runner = NS(_is_user_authorized=lambda _: True,
                _load_profile_config_for_name=lambda _: {}, _profile_home_for_name=lambda _: tmp_path,
                _memory_provider_route_for_source=lambda src, cfg, mid: NS(context=context(event=mid), provider='mempalace', provider_options={}))
    return runner, source, seen


def test_passive_original_author_before_augmented_model_prompt(gateway):
    runner, source, seen = gateway
    original = event()
    original.text = 'Injected history and tools should not become evidence'
    captured = capture_authenticated_memory(runner, original, source)
    assert captured.text == original.raw_message.text
    assert captured.source.user_id == '123456' and len(seen) == 1
    assert original.authenticated_memory_admissions


@pytest.mark.parametrize('field,value', [('chat_id', '-99999'), ('thread_id', '88'), ('user_id', '55555')])
def test_raw_destination_and_actor_mismatch_fails_closed(gateway, field, value):
    runner, source, seen = gateway
    assert capture_authenticated_memory(runner, event(), replace(source, **{field: value})) is None
    assert seen == []


def test_batch_individual_sources_and_forward_skip(gateway):
    runner, source, seen = gateway
    first, second, forwarded = event(), event('2', 'My favorite color is blue.'), event('3', 'Forwarded fact', True)
    merged = event()
    merged.authenticated_user_request_text = first.text + '\n' + second.text
    merged.authenticated_memory_constituents = (first, second, forwarded)
    capture_authenticated_memory(runner, merged, source)
    assert [v.source.message_id for v in merged.authenticated_memory_inputs] == ['1', '2']
    assert [v.source.message_id for v in seen] == ['1', '2']
    assert len(merged.authenticated_memory_admissions) == 2


def test_bound_source_cannot_be_reused_for_other_event_or_author(gateway):
    runner, source, _ = gateway
    bound = capture_authenticated_memory(runner, event(), source)
    with pytest.raises(ValueError): matching_input(bound, replace(source, user_id='55555'), '1')
    with pytest.raises(ValueError): matching_input(bound, source, '2')
    assert matching_input(bound, source, '1').user_id == '123456'


def test_replay_uses_existing_immutable_evidence(gateway):
    runner, source, seen = gateway
    original = event()
    bound = capture_authenticated_memory(runner, original, source)
    original.text = 'untrusted later modification'
    assert capture_authenticated_memory(runner, original, source) is bound
    assert len(seen) == 1
