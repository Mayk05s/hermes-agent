"""Unregistered shared destinations cannot fall back to profile-global memory."""
from dataclasses import replace

import pytest

from gateway.config import Platform
from gateway.memory_routing import resolve_memory_provider_route, resolve_private_disclosure_route
from tests.gateway.test_memory_group_provider_route import group_setup


def enable_denial(config):
    route = config['memory']['trusted_provider_routes'][0]
    config['memory']['trusted_shared_deny_route'] = {
        **route, 'platform': 'telegram', 'automatic_config': 'must-not-be-inherited',
    }
    return config['memory']['trusted_shared_deny_route']


def resolve(source, config, authorized=True):
    return resolve_memory_provider_route(source, config, request_authorized=authorized,
                                         principal_verified=True)


@pytest.mark.parametrize('profile', ['default', 'personal'])
def test_unregistered_group_has_exact_empty_boundary(group_setup, profile):
    source, config = group_setup
    enable_denial(config)
    if profile == 'default':
        config['memory']['trusted_provider_routes'] = []
    source = replace(source, chat_id='-100999999', thread_id='313', profile_name=profile)
    route = resolve(source, config)
    assert route.context.destination_kind == 'group'
    assert route.context.read_wings == ()
    assert route.context.write_wing is None and route.context.grants == ()
    assert route.automatic_config == ''
    assert route.cache_fingerprint == resolve(replace(source, message_id='11'), config).cache_fingerprint
    for change in ({'chat_id': '-100999998'}, {'thread_id': '314'}, {'user_id': 'someone-else'}):
        assert route.cache_fingerprint != resolve(replace(source, **change), config).cache_fingerprint
    assert resolve_private_disclosure_route(source, config, request_authorized=True, principal_verified=True) is None


def test_registered_scope_and_unknown_topic_keep_existing_boundaries(group_setup):
    source, config = group_setup
    expected = resolve(source, config)
    enable_denial(config)
    assert resolve(source, config) == expected
    unknown = resolve(replace(source, thread_id='new-topic'), config)
    assert unknown.context.read_wings == () and unknown.context.write_wing is None


@pytest.mark.parametrize('change', [
    {'chat_id': '123', 'chat_type': 'dm', 'thread_id': None},
    {'platform': Platform.DISCORD, 'chat_id': '-10099999'},
])
def test_default_dm_and_other_platform_compatibility(group_setup, change):
    source, config = group_setup
    enable_denial(config)
    assert resolve(replace(source, **change), config) is None


def test_unmigrated_absent_optin_remains_unchanged(group_setup):
    source, config = group_setup
    config['memory']['trusted_provider_routes'] = []
    assert resolve(source, config) is None


@pytest.mark.parametrize('mutation', ['wrong_type', 'wrong_platform', 'missing_key', 'missing_policy', 'unauthorized', 'missing_enabled', 'string_enabled', 'numeric_enabled'])
def test_malformed_enabled_denial_never_returns_legacy(group_setup, mutation):
    source, config = group_setup
    template = enable_denial(config)
    source = replace(source, chat_id='-100999999')
    if mutation == 'wrong_type':
        config['memory']['trusted_shared_deny_route'] = 'bad'
    elif mutation == 'wrong_platform':
        template['platform'] = 'discord'
    elif mutation == 'missing_key':
        template.pop('opaque_id_key_file')
    elif mutation == 'missing_policy':
        template['policy_file'] = '/does-not-exist'
    elif mutation == 'missing_enabled':
        template.pop('enabled')
    elif mutation == 'string_enabled':
        template['enabled'] = 'true'
    elif mutation == 'numeric_enabled':
        template['enabled'] = 0
    with pytest.raises((ValueError, KeyError, FileNotFoundError)):
        resolve(source, config, authorized=mutation != 'unauthorized')
