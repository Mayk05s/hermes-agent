from dataclasses import replace
import json

import pytest

from gateway.memory_access import MemoryDestination, MemoryGrant
from plugins.memory.mempalace.capability_client import MemPalaceCapabilityClient
from plugins.memory.mempalace.capability_provider import MemPalaceMemoryProvider
from tests.plugins.memory.test_mempalace_capability_provider import RecordingTransport, fixture_vector, vector_context


@pytest.mark.parametrize('disclosure', [False, True])
def test_long_russian_question_tail_reaches_scoped_search(disclosure):
    transport = RecordingTransport()
    provider = MemPalaceMemoryProvider(client=MemPalaceCapabilityClient(environ=fixture_vector()['environment'], transport=transport))
    context = vector_context()
    if disclosure:
        context = replace(context, destination=MemoryDestination('group', 'telegram', 'c_eeeeeeeeeeeeeeee', 'root'),
                          read_wings=('private.p_aaaaaaaaaaaaaaaa.personal',), write_wing=None,
                          grants=(MemoryGrant(id=context.source_ref, type='disclose_private_to_group'),))
    provider.set_access_context(context)
    query = 'В нашей давней беседе был небольшой вымышленный проект. ' * 5 + 'Какой цвет этикеток у вымышленного проекта Нимбус?'
    assert len(query.encode()) > 250
    result = provider.private_disclosure(query) if disclosure else provider.prefetch(query)
    assert result
    sent = json.loads(transport.calls[-1]['body'])['params']['arguments']['query']
    assert sent == query and sent.endswith('Нимбус?')
    assert len(sent.encode()) <= 2048


def test_query_has_finite_utf8_safe_bound():
    transport = RecordingTransport()
    provider = MemPalaceMemoryProvider(client=MemPalaceCapabilityClient(environ=fixture_vector()['environment'], transport=transport))
    provider.set_access_context(vector_context())
    provider.prefetch('я' * 2000)
    sent = json.loads(transport.calls[-1]['body'])['params']['arguments']['query']
    assert len(sent.encode()) == 2048 and '\ufffd' not in sent


@pytest.mark.parametrize('tool', ['mempalace_get_drawer', 'mempalace_delete_drawer', 'mempalace_update_drawer'])
def test_native_generated_drawer_id_fits_bounded_semantic_surface(tool):
    from plugins.memory.mempalace.capability_client import validate_tool_arguments, MemPalaceClientError
    identifier = 'drawer_group.telegram.c_' + 'a' * 26 + '.root.shared_page-fact-' + 'b' * 48 + '_' + 'c' * 24
    assert 128 < len(identifier.encode()) <= 512
    extra = {'content': 'Harmless synthetic fixture.'} if tool == 'mempalace_update_drawer' else {}
    assert validate_tool_arguments(tool, {'drawer_id': identifier, **extra})['drawer_id'] == identifier
    assert validate_tool_arguments(tool, {'drawer_id': 'a' * 512, **extra})
    with pytest.raises(MemPalaceClientError):
        validate_tool_arguments(tool, {'drawer_id': 'a' * 513, **extra})
