"""Configured fallback must preserve references and primary failure diagnostics."""
import importlib
from types import SimpleNamespace

import pytest

codex = importlib.import_module('plugins.image_gen.openai-codex')


@pytest.fixture
def setup(monkeypatch):
    provider = codex.OpenAICodexImageGenProvider()
    monkeypatch.setattr(codex, '_load_image_gen_config', lambda: {'fallback_providers': ['antigravity']})
    return provider


@pytest.mark.parametrize('kind', ['api_error', 'rate_limit', 'auth_required', 'empty_response', 'provider_exception'])
def test_primary_failure_routes_same_request_to_fallback(setup, monkeypatch, kind):
    monkeypatch.setattr(setup, '_generate_primary', lambda *a, **k: {
        'success': False, 'image': None, 'error_type': kind, 'error': 'primary failed', 'provider': 'openai-codex'})
    calls = []
    def generate(prompt, aspect_ratio, **kwargs):
        calls.append((prompt, aspect_ratio, kwargs))
        return {'success': True, 'image': '/image.jpg', 'provider': 'antigravity'}
    monkeypatch.setattr('agent.image_gen_registry.get_provider', lambda name: SimpleNamespace(generate=generate))
    result = setup.generate('exact cake prompt', 'portrait', reference_images=['/cake.jpg'], model='gpt-tier')
    assert calls == [('exact cake prompt', 'portrait', {'reference_images': ['/cake.jpg']})]
    assert result['provider'] == 'antigravity'
    assert result['fallback_used'] is True
    assert result['primary_error_type'] == kind
    assert result['fallback_from'] == 'openai-codex'


@pytest.mark.parametrize('primary', [
    {'success': True, 'image': '/gpt.png'},
    {'success': False, 'error_type': 'invalid_argument'},
    {'success': False, 'error_type': 'invalid_reference_image'},
])
def test_success_or_invalid_input_does_not_invoke_fallback(setup, monkeypatch, primary):
    monkeypatch.setattr(setup, '_generate_primary', lambda *a, **k: primary)
    monkeypatch.setattr('agent.image_gen_registry.get_provider', lambda name: pytest.fail('unexpected fallback'))
    assert setup.generate('cake') == primary


def test_disabled_by_default(setup, monkeypatch):
    primary = {'success': False, 'error_type': 'api_error'}
    monkeypatch.setattr(setup, '_generate_primary', lambda *a, **k: primary)
    monkeypatch.setattr(codex, '_load_image_gen_config', lambda: {})
    monkeypatch.setattr('agent.image_gen_registry.get_provider', lambda name: pytest.fail('unexpected fallback'))
    assert setup.generate('cake') == primary


def test_both_fail_preserves_original_and_reports_fallback_failure(setup, monkeypatch):
    monkeypatch.setattr(setup, '_generate_primary', lambda *a, **k: {
        'success': False, 'image': None, 'error_type': 'api_error', 'error': 'original'})
    monkeypatch.setattr('agent.image_gen_registry.get_provider', lambda name: SimpleNamespace(
        generate=lambda *a, **k: {'success': False, 'error_type': 'timeout'}))
    result = setup.generate('cake')
    assert result['success'] is False
    assert result['error'] == 'original'
    assert result['fallback_attempts'] == [{'provider': 'antigravity', 'error_type': 'timeout'}]


def test_self_fallback_is_skipped(setup, monkeypatch):
    monkeypatch.setattr(setup, '_generate_primary', lambda *a, **k: {'success': False})
    monkeypatch.setattr(codex, '_load_image_gen_config', lambda: {'fallback_providers': ['openai-codex']})
    monkeypatch.setattr('agent.image_gen_registry.get_provider', lambda name: pytest.fail('recursive fallback'))
    assert setup.generate('cake')['success'] is False


def test_fallback_keeps_tool_available_without_gpt_auth(setup, monkeypatch):
    monkeypatch.setattr(codex, '_read_codex_access_token', lambda: None)
    monkeypatch.setattr('agent.image_gen_registry.get_provider', lambda name: SimpleNamespace(is_available=lambda: True))
    assert setup.is_available() is True
