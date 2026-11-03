"""Native Antigravity image generation via the local agy-image command.

The helper isolates credentials and references per request. Results live in
this Hermes profile's image cache; no conversation history is shared.
"""
from __future__ import annotations

import base64
import datetime
import logging
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
from typing import Any

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO, ImageGenProvider, error_response,
    resolve_aspect_ratio, success_response,
)

logger = logging.getLogger(__name__)
MAX_REFERENCE_BYTES = 50 * 1024 * 1024


def _config() -> dict:
    from hermes_cli.config import load_config_readonly
    section = load_config_readonly().get('image_gen', {})
    value = section.get('antigravity', {}) if isinstance(section, dict) else {}
    return value if isinstance(value, dict) else {}


def _helper(config: dict) -> str | None:
    value = config.get('command', 'agy-image')
    if not isinstance(value, str) or not value.strip():
        return None
    return shutil.which(value)


def _references(values: Any, directory: Path) -> list[Path]:
    """Materialize only the supplied ordered references, with bounded reads."""
    from PIL import Image
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)) or len(values) > 10:
        raise ValueError('reference_images must contain at most 10 image paths or URLs')
    paths = []
    for index, reference in enumerate(values, 1):
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError(f'Reference image {index} must be a non-empty string')
        reference = reference.strip()
        if reference.startswith('data:image/'):
            header, encoded = reference.split(',', 1)
            if ';base64' not in header or len(encoded) > MAX_REFERENCE_BYTES * 4 // 3 + 4:
                raise ValueError('Invalid or oversized base64 image')
            data = base64.b64decode(encoded, validate=True)
        elif reference.startswith(('https://', 'http://')):
            import requests
            with requests.get(reference, stream=True, timeout=(15, 45)) as response:
                response.raise_for_status()
                chunks, length = [], 0
                for chunk in response.iter_content(65536):
                    length += len(chunk)
                    if length > MAX_REFERENCE_BYTES:
                        raise ValueError('Reference image exceeds 50 MB')
                    chunks.append(chunk)
                data = b''.join(chunks)
        else:
            source = Path(reference).expanduser()
            if not source.is_absolute() or not source.is_file():
                raise ValueError(f'Reference image {index} must be a readable absolute file path')
            if source.stat().st_size > MAX_REFERENCE_BYTES:
                raise ValueError('Reference image exceeds 50 MB')
            data = source.read_bytes()
        if not data or len(data) > MAX_REFERENCE_BYTES:
            raise ValueError('Reference image is empty or exceeds 50 MB')
        path = directory / f'reference-{index}.image'
        path.write_bytes(data)
        with Image.open(path) as image:
            extension = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp', 'GIF': '.gif'}.get(image.format)
            if extension is None:
                raise ValueError('Unsupported reference image format')
            image.verify()
        renamed = path.with_suffix(extension)
        path.rename(renamed)
        paths.append(renamed)
    return paths


class AntigravityImageGenProvider(ImageGenProvider):
    @property
    def name(self):
        return 'antigravity'

    @property
    def display_name(self):
        return 'Antigravity (local CLI)'

    def is_available(self):
        try:
            return bool(_helper(_config()))
        except Exception:
            return False

    def generate(self, prompt, aspect_ratio=DEFAULT_ASPECT_RATIO, **kwargs):
        prompt = (prompt or '').strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        config = _config()
        model = config.get('model', 'gemini-3.8-flash-low')

        def failure(message, kind):
            return error_response(error=message, error_type=kind, provider=self.name,
                                  model=model, prompt=prompt, aspect_ratio=aspect)

        if not prompt:
            return failure('Prompt is required', 'invalid_argument')
        helper = _helper(config)
        if not helper:
            return failure('Authenticated agy-image command is not installed', 'missing_dependency')
        try:
            timeout = max(30, min(600, int(config.get('timeout', 240))))
            from hermes_constants import get_hermes_home
            cache = get_hermes_home() / 'cache' / 'images'
            cache.mkdir(parents=True, exist_ok=True)
            output = cache / f'antigravity_{datetime.datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.jpg'
            with tempfile.TemporaryDirectory(prefix='hermes-image-references-') as temporary:
                try:
                    references = _references(kwargs.get('reference_images'), Path(temporary))
                except Exception as exc:
                    return failure(f'Could not read reference image: {exc}', 'invalid_reference_image')
                command = [helper, '--prompt', prompt, '--output', str(output),
                           '--model', model, '--aspect-ratio', aspect, '--timeout', str(timeout)]
                for reference in references:
                    command.extend(['--reference-image', str(reference)])
                result = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 60)
                if result.returncode:
                    logger.warning('Antigravity image helper failed (exit=%s)', result.returncode)
                    return failure('Antigravity did not produce a completed image', 'api_error')
                from PIL import Image
                with Image.open(output) as image:
                    if image.format != 'JPEG':
                        raise ValueError('Antigravity output is not a JPEG image')
                    width, height = image.size
                    image.verify()
                return success_response(
                    image=str(output), model=model, prompt=prompt, aspect_ratio=aspect,
                    provider=self.name, extra=dict(width=width, height=height,
                    mime_type='image/jpeg', reference_images_count=len(references)),
                )
        except subprocess.TimeoutExpired:
            return failure('Antigravity image generation timed out', 'timeout')
        except Exception:
            logger.warning('Antigravity image generation failed', exc_info=True)
            return failure('Antigravity image generation failed or returned an invalid image', 'api_error')


def register(ctx):
    ctx.register_image_gen_provider(AntigravityImageGenProvider())
