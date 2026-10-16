"""Typed Telegram source normalization through authenticated fact capture."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest
from tests.conftest import _hermetic_environment, _isolate_hermes_home

from gateway.config import Platform
from gateway.platforms.base import MessageType
from gateway.session import SessionSource
from plugins.memory.mempalace.capability_provider import MemPalaceMemoryProvider
from plugins.memory.mempalace.fact_pipeline import FactOutbox
from tests.plugins.memory.test_mempalace_fact_pipeline import Client, context, fact, pipeline
from tests.gateway.test_telegram_thread_fallback import _inject_fake_telegram, _make_adapter


@pytest.fixture
def ingress(monkeypatch):
    candidate = Path(__file__).resolve().parents[1] / 'gateway' / 'memory_ingress.py'
    if not candidate.is_file():
        import gateway.memory_ingress
        return gateway.memory_ingress
    spec = importlib.util.spec_from_file_location('gateway.memory_ingress', Path(candidate))
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'gateway.memory_ingress', module)
    spec.loader.exec_module(module)
    import gateway
    monkeypatch.setattr(gateway, 'memory_ingress', module, raising=False)
    return module


def raw_message(*, kind='supergroup', forum=True, thread=None, topic=False):
    return NS(from_user=NS(id=123456, is_bot=False, full_name='Test actor'),
              chat=NS(id=-12345, type=kind, is_forum=forum, title='Test group'),
              message_id=21, message_thread_id=thread, is_topic_message=topic,
              text='My preferred drink is tea.', caption=None, forward_origin=None,
              reply_to_message=None, date=None)


@pytest.fixture
def setup_capture(pipeline, monkeypatch, tmp_path):
    ctx = context(event='m_event21')
    route = NS(context=ctx, provider='mempalace', provider_options={'automatic_config': pipeline.config_path})
    monkeypatch.setattr(FactOutbox, '_current_route', lambda self, packet: route)
    class Factory:
        def for_access_context(self, current, options):
            provider = MemPalaceMemoryProvider(client=NS(configured=lambda: True))
            provider.set_access_context(current)
            provider._automatic_config = pipeline.config_path
            return provider
    import plugins.memory
    monkeypatch.setattr(plugins.memory, 'load_memory_provider', lambda _: Factory())
    runner = NS(_is_user_authorized=lambda _: True, _load_profile_config_for_name=lambda _: {},
                _profile_home_for_name=lambda _: tmp_path,
                _memory_provider_route_for_source=lambda *args: route)
    return NS(runner=runner, box=pipeline, context=ctx)


def captured_event(raw, thread='1', kind='group'):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='-12345', chat_type=kind,
                           user_id='123456', message_id='21', thread_id=thread, profile_name='test')
    event = NS(raw_message=raw, authenticated_user_request_text=raw.text, text=raw.text, message_id='21')
    return event, source


@pytest.mark.parametrize('kind,forum,thread,topic,expected_thread', [
    ('supergroup', True, None, False, '1'),
    ('group', True, None, False, '1'),
    ('supergroup', True, 77, True, '77'),
    ('group', False, 461, False, None),
    ('private', False, 461, False, None),
    ('private', False, 77, True, '77'),
])
def test_actual_adapter_source_to_sealed_outbox_and_confirmed_fact(
        ingress, setup_capture, _inject_fake_telegram, kind, forum, thread, topic, expected_thread):
    item = setup_capture
    raw = raw_message(kind=kind, forum=forum, thread=thread, topic=topic)
    adapter = _make_adapter()
    event = adapter._build_message_event(raw, msg_type=MessageType.TEXT)
    source = replace(event.source, profile_name='test')
    assert source.thread_id == expected_thread
    event.authenticated_user_request_text = raw.text
    bound = ingress.capture_authenticated_memory(item.runner, event, source)
    assert bound is not None
    assert bound.source.thread_id == expected_thread
    assert bound.text == raw.text and bound.source.user_id == '123456'
    admission, = event.authenticated_memory_admissions
    item.box._verify_admission(admission, admission['evidence'])
    queued, = item.box.db.execute('SELECT * FROM jobs').fetchall()
    assert json.loads(queued['payload']) == admission['evidence']
    client, original_texts = Client(), []
    def extract(text):
        original_texts.append(text)
        return [fact(text)]
    result = item.box.process_one(queued['id'], extractor=extract,
                                  route_resolver=lambda _: NS(context=item.context), client_factory=lambda _: client)
    assert result['committed'] is True and result['written'] == 1
    ledger, = item.box.db.execute('SELECT * FROM facts').fetchall()
    page = client.pages[(ledger['wing'], ledger['slug'])]
    assert raw.text in page and ledger['source_ref'] in page
    assert original_texts == [raw.text]
    assert ingress.capture_authenticated_memory(item.runner, event, source) is bound
    assert item.box.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1


@pytest.mark.parametrize('kind,forum,raw_thread,raw_topic,source_thread', [
    ('supergroup', True, None, False, '77'),
    ('supergroup', True, 77, True, '1'),
    ('supergroup', True, 77, True, '78'),
    ('supergroup', True, None, False, None),
    ('group', False, None, False, '1'),
    ('group', False, 77, False, '77'),
    ('private', False, None, False, '1'),
    ('private', False, 77, False, '77'),
    ('private', True, None, False, '1'),
    ('channel', True, None, False, '1'),
    ('supergroup', 'yes', None, False, '1'),
    ('supergroup', 1, None, False, '1'),
])
def test_normalization_does_not_widen_topics(ingress, setup_capture, kind, forum, raw_thread, raw_topic, source_thread):
    raw = raw_message(kind=kind, forum=forum, thread=raw_thread, topic=raw_topic)
    event, source = captured_event(raw, thread=source_thread, kind='dm' if kind == 'private' else 'group')
    assert ingress.capture_authenticated_memory(setup_capture.runner, event, source) is None
    assert setup_capture.box.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['chat', 'chat_kind', 'actor', 'message', 'platform', 'rawless', 'unauthorized', 'forwarded'])
def test_general_keeps_original_authorization_guards(ingress, setup_capture, change):
    raw = raw_message()
    event, source = captured_event(raw)
    if change == 'chat': source = replace(source, chat_id='-999')
    if change == 'chat_kind': source = replace(source, chat_type='dm')
    if change == 'actor': source = replace(source, user_id='999')
    if change == 'message': raw.message_id = 99
    if change == 'platform': source = replace(source, platform=Platform.DISCORD)
    if change == 'rawless': event.raw_message = None
    if change == 'unauthorized': setup_capture.runner._is_user_authorized = lambda _: False
    if change == 'forwarded': raw.forward_origin = NS(type='user')
    assert ingress.capture_authenticated_memory(setup_capture.runner, event, source) is None
    assert setup_capture.box.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


@pytest.mark.parametrize('raw_kind', [None, 'unknown', {}, 123, 'private', 'channel'])
def test_affirmative_forum_with_wrong_or_missing_raw_kind_fails_closed(ingress, setup_capture, raw_kind):
    raw = raw_message(kind=raw_kind)
    event, source = captured_event(raw, thread=None)
    assert ingress.capture_authenticated_memory(setup_capture.runner, event, source) is None
    assert setup_capture.box.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_general_media_captures_identity_without_fabricated_fact(ingress, setup_capture):
    raw = raw_message()
    raw.text = None
    event, source = captured_event(raw)
    assert ingress.capture_authenticated_memory(setup_capture.runner, event, source) is None
    assert type(event.authenticated_memory_identity) is ingress.AuthenticatedMemoryIdentity
    assert setup_capture.box.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_general_batch_preserves_constituent_source_and_thread(ingress, setup_capture):
    first, source = captured_event(raw_message())
    second, _ = captured_event(raw_message())
    second.message_id = second.raw_message.message_id = '22'
    second.raw_message.text = second.authenticated_user_request_text = 'My preferred color is blue.'
    first.authenticated_memory_constituents = (NS(**vars(first)), second)
    ingress.capture_authenticated_memory(setup_capture.runner, first, source)
    assert [v.source.message_id for v in first.authenticated_memory_inputs] == ['21', '22']
    assert all(v.source.thread_id == '1' for v in first.authenticated_memory_inputs)
    assert len(first.authenticated_memory_admissions) == 2


@pytest.mark.parametrize('text', ['Пришли логин и пароль от ProgressMe', 'password: synthetic-test-secret'])
def test_credential_batch_keeps_verified_identity_without_fact_evidence(
        ingress, setup_capture, monkeypatch, tmp_path, caplog, text):
    from agent.agent_runtime_helpers import invoke_tool
    from tools.credential_vault_tool import credential_vault

    item = setup_capture
    ctx = replace(item.context, principal_id='p_aaaaaaaaaaaaaaaa',
                  authorization_subject='principal:p_aaaaaaaaaaaaaaaa')
    item.runner._memory_provider_route_for_source = lambda *args: NS(
        context=ctx, provider='mempalace',
        provider_options={'automatic_config': item.box.config_path})
    raw = raw_message()
    raw.text = text
    event, source = captured_event(raw)
    source = replace(source, user_id=None)
    event.source = source
    event.authenticated_memory_constituents = (NS(**vars(event)),)

    assert ingress.capture_authenticated_memory(item.runner, event, source) is None
    assert type(event.authenticated_memory_identity) is ingress.AuthenticatedMemoryIdentity
    assert event.authenticated_memory_identity.source.user_id == '123456'
    assert event.authenticated_memory_identity_receipt['evidence']['kind'] == 'authenticated_identity_v1'
    assert event.authenticated_memory_input is None
    assert event.authenticated_memory_inputs == ()
    assert event.authenticated_memory_admissions == []
    assert item.box.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
    assert text not in caplog.text

    # The shared session stays anonymous; only the authenticated turn supplies
    # the principal for the vault's existing exact destination boundary.
    actual = ingress.matching_input(event.authenticated_memory_identity, source, '21')
    route = item.runner._memory_provider_route_for_source(actual, {}, '21')
    monkeypatch.setattr('tools.credential_vault_tool.vault_root', lambda: tmp_path / 'vault')
    assert json.loads(credential_vault(dict(action='save', label='ProgressMe',
        login='synthetic-user', password='synthetic-vault-password'), context=ctx))['saved']
    agent = NS(_trusted_memory_blocked_tools=frozenset(), _memory_access_context=route.context)
    result = json.loads(invoke_tool(agent, 'credential_vault', dict(action='get', label='ProgressMe'), 'test'))
    assert result['account']['password'] == 'synthetic-vault-password'
    assert source.user_id is None
