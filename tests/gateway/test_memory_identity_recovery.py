"""Identity-only media/restart access is not authenticated fact evidence."""
import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from gateway.config import Platform
from gateway.durable_jobs import DurableJobStore
from gateway.memory_ingress import (AuthenticatedMemoryIdentity, capture_authenticated_memory,
                                   restore_authenticated_memory_identity)
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from plugins.memory.mempalace.capability_provider import MemPalaceMemoryProvider
from tests.plugins.memory.test_mempalace_fact_pipeline import context, pipeline


@pytest.fixture
def identity_gateway(pipeline, tmp_path, monkeypatch):
    store = DurableJobStore(tmp_path / "inbox.sqlite3")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-12345", chat_type="group",
                           user_id="123456", profile_name="test", message_id="21")
    def route(value, *args):
        return NS(context=context(event="m_" + value.message_id), provider="mempalace",
                  provider_options={"automatic_config": pipeline.config_path})
    class Factory:
        def for_access_context(self, ctx, options):
            provider = MemPalaceMemoryProvider(client=NS(configured=lambda: True))
            provider.set_access_context(ctx)
            provider._automatic_config = pipeline.config_path
            return provider
    import plugins.memory
    monkeypatch.setattr(plugins.memory, "load_memory_provider", lambda _: Factory())
    runner = NS(_is_user_authorized=lambda _: True, _profile_home_for_name=lambda _: tmp_path,
                _load_profile_config_for_name=lambda _: {}, _memory_provider_route_for_source=route,
                _durable_store=lambda: store)
    yield NS(runner=runner, store=store, source=source, box=pipeline)
    store.close()


def media_event(item, text=None):
    raw = NS(from_user=NS(id=123456, is_bot=False), chat=NS(id=-12345), message_id="21",
             message_thread_id=None, text=None, caption=None)
    return MessageEvent(source=item.source, text=text or "[Photo]", raw_message=raw,
                        message_id="21", authenticated_user_request_text=text)


@pytest.mark.parametrize("generated_text", [None, "Generated speech-to-text nutrition note"])
def test_media_identity_is_captured_without_fact_queue(identity_gateway, generated_text):
    item = identity_gateway
    event = media_event(item, generated_text)
    assert capture_authenticated_memory(item.runner, event, item.source) is None
    assert type(event.authenticated_memory_identity) is AuthenticatedMemoryIdentity
    receipt = event.authenticated_memory_identity_receipt
    assert receipt["evidence"]["kind"] == "authenticated_identity_v1"
    assert "text" not in receipt["evidence"]
    assert event.authenticated_memory_input is None
    assert item.box.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def persist_media(item):
    event = media_event(item)
    capture_authenticated_memory(item.runner, event, item.source)
    inbox, _ = item.store.ingest_event(thread_key="synthetic", platform="telegram", source=item.source,
        request_text="[Photo]", message_id="21", event_metadata={
            "authenticated_memory_identity_receipt": event.authenticated_memory_identity_receipt})
    return inbox


def test_rawless_inbox_restores_only_verified_identity(identity_gateway):
    item = identity_gateway
    inbox = persist_media(item)
    recovered = MessageEvent(source=item.source, text="Untrusted attributed restart text", message_id="21",
                             durable_inbox_id=inbox["event_id"], durable_recovery=True)
    identity = restore_authenticated_memory_identity(item.runner, recovered, item.source)
    assert type(identity) is AuthenticatedMemoryIdentity
    assert identity.source.user_id == item.source.user_id
    assert recovered.raw_message is None and recovered.authenticated_memory_input is None
    assert item.box.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["revoked", "actor", "topic", "event", "mac", "route"])
def test_recovered_identity_rechecks_seal_and_current_binding(identity_gateway, change):
    item = identity_gateway
    event = media_event(item)
    capture_authenticated_memory(item.runner, event, item.source)
    receipt = event.authenticated_memory_identity_receipt
    source = item.source
    if change == "revoked": item.runner._is_user_authorized = lambda _: False
    if change == "actor": source = replace(source, user_id="999999")
    if change == "topic": source = replace(source, thread_id="313")
    if change == "event": source = replace(source, message_id="22")
    if change == "mac": receipt["mac"] = "0" * 64
    if change == "route": item.runner._memory_provider_route_for_source = lambda *a: None
    recovered = MessageEvent(source=source, text="", message_id=source.message_id)
    assert restore_authenticated_memory_identity(item.runner, recovered, source,
        {"authenticated_memory_identity_receipt": receipt}) is None


def test_unsigned_attribution_is_not_identity_proof(identity_gateway):
    item = identity_gateway
    event = MessageEvent(source=item.source, text="[User 123456] generated statement", message_id="21")
    assert restore_authenticated_memory_identity(item.runner, event, item.source,
        {"authenticated_user_request_text": "I prefer tea."}) is None


def test_credential_batch_identity_survives_durable_handoff(identity_gateway):
    item = identity_gateway
    event = media_event(item, 'Пришли логин и пароль от ProgressMe')
    event.raw_message.text = event.authenticated_user_request_text
    event.authenticated_memory_constituents = (replace(event),)
    assert capture_authenticated_memory(item.runner, event, item.source) is None
    assert event.authenticated_memory_input is None
    assert event.authenticated_memory_admissions == []

    receipt = event.authenticated_memory_identity_receipt
    assert receipt['evidence']['kind'] == 'authenticated_identity_v1'
    assert 'text' not in receipt['evidence']
    recovered = MessageEvent(source=replace(item.source, user_id=None), text='', message_id='21')
    identity = restore_authenticated_memory_identity(item.runner, recovered, recovered.source,
        {'authenticated_memory_identity_receipt': receipt})
    assert type(identity) is AuthenticatedMemoryIdentity
    assert identity.source.user_id == item.source.user_id
    assert recovered.authenticated_memory_input is None
    assert item.box.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_new_topic_seals_identity_without_memory_rights(identity_gateway):
    item = identity_gateway
    original_route = item.runner._memory_provider_route_for_source
    def closed_route(*args):
        route = original_route(*args)
        route.context = replace(route.context, read_wings=(), write_wing=None)
        return route
    item.runner._memory_provider_route_for_source = closed_route
    event = media_event(item)
    assert capture_authenticated_memory(item.runner, event, item.source) is None
    receipt = event.authenticated_memory_identity_receipt
    assert receipt['evidence']['context']['read_wings'] == []
    assert receipt['evidence']['context']['write_wing'] is None
    recovered = MessageEvent(source=item.source, text='', message_id='21')
    assert restore_authenticated_memory_identity(item.runner, recovered, item.source,
        {'authenticated_memory_identity_receipt': receipt}) is not None
    assert recovered.authenticated_memory_input is None
    assert item.box.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
