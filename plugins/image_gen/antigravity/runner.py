#!/usr/bin/env python3
"""Generate a verified JPEG using the local authenticated Antigravity CLI."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--output', required=True, help='New .jpg or .jpeg output path')
    parser.add_argument('--model', default='gemini-3.8-flash-low')
    parser.add_argument('--timeout', type=int, default=240)
    parser.add_argument('--reference-image', action='append', default=[])
    parser.add_argument('--aspect-ratio', choices=['landscape', 'square', 'portrait'], default='square')
    args = parser.parse_args()
    output = Path(args.output).expanduser().resolve()
    if output.suffix.lower() not in {'.jpg', '.jpeg'}:
        parser.error('Native image output is JPEG; use .jpg or .jpeg')
    if output.exists():
        parser.error('Output already exists; choose a new path')
    source = Path(os.environ.get('ANTIGRAVITY_RUNTIME_DIR', str(Path.home() / '.local/libexec/antigravity')))
    spec = importlib.util.spec_from_file_location('bridge', source / 'hermes_openai_bridge.py')
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='agy-image-') as temporary:
        workspace = Path(temporary)
        home = workspace / 'home'
        home.mkdir(mode=0o700)
        # Copy only authentication/settings, never unrelated conversation history.
        state = home / '.gemini'
        for relative in ('oauth_creds.json', 'google_accounts.json',
                         'antigravity-cli/installation_id',
                         'antigravity-cli/antigravity-oauth-token'):
            origin = bridge.SOURCE_STATE / relative
            if origin.is_file():
                destination = state / relative
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(origin, destination)
        if not (state / 'oauth_creds.json').is_file():
            raise RuntimeError('Antigravity OAuth state is missing')
        references = []
        from PIL import Image
        if len(args.reference_image) > 10:
            raise ValueError('At most 10 reference images are supported')
        for index, reference in enumerate(args.reference_image, 1):
            origin = Path(reference).expanduser().resolve(strict=True)
            if not origin.is_file() or origin.stat().st_size > 50 * 1024 * 1024:
                raise ValueError('Reference image is missing or exceeds 50 MB')
            with Image.open(origin) as image:
                image.verify()
            destination = workspace / f'reference-{index}{origin.suffix}'
            shutil.copy2(origin, destination)
            references.append(str(destination))
        target = workspace / 'generated.jpg'
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TZ', 'TERM', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY') if key in os.environ}
        env.update(HOME=str(home), AGY_CLI_DISABLE_AUTO_UPDATE='true', NO_COLOR='1')
        prompt = (
            'Generate an actual raster image using your native image generator. '
            'Do not draw using code, SVG, HTML, or fetch a substitute image. '
            'Do not inspect unrelated files. Wait for generation to finish. '
            'You may copy the generated image artifact to the destination using a file command. '
            'Native JPEG output is expected. Save the actual generated JPEG exactly at: '
            + str(target) + '\nAspect ratio: ' + args.aspect_ratio
            + '\nOrdered reference images (use these as actual visual inputs to image generation): '
            + json.dumps(references) + '\nUser image request: ' + args.prompt
            + '\nOnly report success after the destination file exists.'
        )
        process = subprocess.Popen(
            [str(bridge.QEMU), '-cpu', 'max', str(bridge.AGY_NATIVE),
             '--dangerously-skip-permissions', '--model', args.model,
             '--output-format', 'json', '--print-timeout', f'{args.timeout}s',
             '--print', prompt],
            cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=args.timeout + 30)
        except subprocess.TimeoutExpired:
            # Tear down tool subprocesses too, before deleting their HOME.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise RuntimeError('Antigravity image generation timed out')
        if process.returncode or not target.is_file():
            raise RuntimeError('Antigravity did not produce an image: ' + (stdout + stderr)[-4000:])
        from PIL import Image
        with Image.open(target) as image:
            if image.format != 'JPEG':
                raise RuntimeError(f'Expected JPEG, received {image.format}')
            width, height = image.size
            image.verify()
        output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents accidental replacement during a concurrent run.
        try:
            with output.open('xb') as destination, target.open('rb') as generated:
                shutil.copyfileobj(generated, destination)
        except FileExistsError:
            raise RuntimeError('Output appeared during generation; refusing to replace it')
        print(json.dumps(dict(path=str(output), mime='image/jpeg', width=width,
                              height=height, bytes=output.stat().st_size,
                              seconds=round(time.monotonic()-started, 2))))


if __name__ == '__main__':
    main()
