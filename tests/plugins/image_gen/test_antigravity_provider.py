"""Reference preservation, profile isolation and verified output for Antigravity."""
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

plugin = importlib.import_module('plugins.image_gen.antigravity')


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'profile'))
    monkeypatch.setattr(plugin, '_config', lambda: {'model': 'agy-model', 'timeout': 30})
    monkeypatch.setattr(plugin, '_helper', lambda config: '/fake/agy-image')
    return plugin.AntigravityImageGenProvider()


def test_reference_edit_uses_actual_image_and_profile_cache(setup, monkeypatch, tmp_path):
    reference = tmp_path / 'source.png'
    Image.new('RGB', (4, 6), 'blue').save(reference)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        supplied = Path(command[command.index('--reference-image') + 1])
        assert supplied.read_bytes() == reference.read_bytes()
        assert supplied != reference
        destination = Path(command[command.index('--output') + 1])
        assert destination.parent == tmp_path / 'profile/cache/images'
        assert command[command.index('--model') + 1] == 'agy-model'
        assert command[command.index('--aspect-ratio') + 1] == 'portrait'
        Image.new('RGB', (8, 12), 'red').save(destination, format='JPEG')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(plugin.subprocess, 'run', run)
    result = setup.generate('edit this cake', 'portrait', reference_images=[str(reference)], model='gpt-tier')
    assert result['success'] is True
    assert result['reference_images_count'] == 1
    assert (result['width'], result['height']) == (8, 12)
    assert result['mime_type'] == 'image/jpeg'
    assert not Path(calls[0][calls[0].index('--reference-image') + 1]).exists()


@pytest.mark.parametrize('output', [None, b'not an image'])
def test_exit_zero_without_valid_image_is_not_success(setup, monkeypatch, output):
    def run(command, **kwargs):
        if output is not None:
            Path(command[command.index('--output') + 1]).write_bytes(output)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(plugin.subprocess, 'run', run)
    assert setup.generate('cake')['success'] is False


def test_missing_reference_does_not_generate_without_it(setup, monkeypatch):
    monkeypatch.setattr(plugin.subprocess, 'run', lambda *a, **k: pytest.fail('Must not drop missing reference'))
    result = setup.generate('cake', reference_images=['/does-not-exist.png'])
    assert result['error_type'] == 'invalid_reference_image'


def test_timeout_reported(setup, monkeypatch):
    def run(*args, **kwargs):
        raise plugin.subprocess.TimeoutExpired('agy-image', 30)
    monkeypatch.setattr(plugin.subprocess, 'run', run)
    assert setup.generate('cake')['error_type'] == 'timeout'
