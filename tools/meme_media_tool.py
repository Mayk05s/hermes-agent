"""Meme media helper tool.

Downloads curated meme image/video URLs into the active profile media folder and
returns Hermes MEDIA tags. This lets gateway chats attach meme media natively
without granting broad terminal/file tools to the profile.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image

from tools.registry import registry

logger = logging.getLogger(__name__)

MAX_URLS = 5
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_VIDEO_BYTES = 45 * 1024 * 1024
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}
BLOCKED_HOSTS = {"tass.ru", "rbc.ru", "rbcautonews.ru", "ria.ru", "rg.ru"}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


def _crop_solid_canvas(img: Image.Image) -> Image.Image:
    """Remove a large, uniform editor canvas around an otherwise valid image.

    Some repost sites serve vertical clips inside a wide solid-colour canvas
    (the all-blue frame from the Pukhososy delivery was one example).  That is
    a presentation artefact, not part of the meme.  Only crop when one edge
    colour clearly dominates and the non-canvas content occupies materially
    less than the whole image, so normal full-frame memes are left intact.
    """
    img = img.convert("RGB")
    width, height = img.size
    if width < 240 or height < 240:
        return img

    # Sample every few pixels along all four edges. Quantisation tolerates
    # JPEG compression without treating a photographic edge as a flat canvas.
    step = max(1, min(width, height) // 300)
    edge_pixels = []
    for x in range(0, width, step):
        edge_pixels.extend((img.getpixel((x, 0)), img.getpixel((x, height - 1))))
    for y in range(0, height, step):
        edge_pixels.extend((img.getpixel((0, y)), img.getpixel((width - 1, y))))
    if not edge_pixels:
        return img

    def quantise(pixel: tuple[int, int, int]) -> tuple[int, int, int]:
        return tuple(channel // 16 for channel in pixel)

    counts: dict[tuple[int, int, int], int] = {}
    for pixel in edge_pixels:
        key = quantise(pixel)
        counts[key] = counts.get(key, 0) + 1
    background_key, background_count = max(counts.items(), key=lambda item: item[1])
    if background_count / len(edge_pixels) < 0.60:
        return img
    background = tuple(channel * 16 + 8 for channel in background_key)

    # Work on a coarse grid first. It is fast for large photos and prevents
    # tiny compression noise in the canvas from defeating the crop.
    scan_step = max(1, min(width, height) // 600)
    threshold = 48
    xs: list[int] = []
    ys: list[int] = []
    for y in range(0, height, scan_step):
        for x in range(0, width, scan_step):
            pixel = img.getpixel((x, y))
            if max(abs(pixel[i] - background[i]) for i in range(3)) > threshold:
                xs.append(x)
                ys.append(y)
    if not xs or not ys:
        return img

    padding = max(2, scan_step * 2)
    left, right = max(0, min(xs) - padding), min(width, max(xs) + padding)
    top, bottom = max(0, min(ys) - padding), min(height, max(ys) + padding)
    cropped_width, cropped_height = right - left, bottom - top
    if cropped_width < 180 or cropped_height < 180:
        return img
    # Do not alter a normal image just because it has a narrow colour bar.
    if cropped_width * cropped_height >= width * height * 0.82:
        return img
    return img.crop((left, top, right, bottom))


def _active_profile_home() -> Path:
    try:
        from gateway.session_context import get_session_env

        profile_name = get_session_env("HERMES_SESSION_PROFILE_NAME", "").strip()
    except Exception:
        profile_name = ""

    try:
        if profile_name and profile_name != "default":
            from hermes_cli.profiles import get_profile_dir

            return get_profile_dir(profile_name)
        from hermes_cli.config import get_hermes_home

        return get_hermes_home()
    except Exception:
        return Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes")).expanduser()


def _clean_title(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "").strip())
    return value[:80]


def _safe_slug(value: str) -> str:
    value = value or "meme"
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return digest


def _allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower()
    if not host:
        return False
    return not any(host == item or host.endswith("." + item) for item in BLOCKED_HOSTS)


def _url_ext(url: str) -> str:
    path = urlparse(url).path.lower()
    ext = Path(path).suffix
    if ext in IMAGE_EXTS or ext in VIDEO_EXTS:
        return ext
    return ""


def _download(url: str, out_dir: Path, slug: str, index: int) -> Path | None:
    if not _allowed_url(url):
        return None

    try:
        resp = requests.get(url, headers=HEADERS, timeout=25, stream=True)
        resp.raise_for_status()
    except Exception as exc:
        logger.info("meme_media download failed for %s: %s", url, exc)
        return None

    content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    ext = _url_ext(url)
    is_image = content_type.startswith("image/") or ext in IMAGE_EXTS
    is_video = content_type.startswith("video/") or ext in VIDEO_EXTS
    if not is_image and not is_video:
        return None

    max_bytes = MAX_VIDEO_BYTES if is_video else MAX_IMAGE_BYTES
    raw_suffix = ext if ext else (".mp4" if is_video else ".img")
    raw_path = out_dir / f"{slug}_{index}.download{raw_suffix}"
    total = 0
    try:
        with raw_path.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raw_path.unlink(missing_ok=True)
                    return None
                fh.write(chunk)
        if total < 1024:
            raw_path.unlink(missing_ok=True)
            return None

        if is_image:
            out_path = out_dir / f"{slug}_{index}.jpg"
            with Image.open(raw_path) as img:
                img = img.convert("RGB")
                if img.width < 180 or img.height < 180:
                    raw_path.unlink(missing_ok=True)
                    return None
                img = _crop_solid_canvas(img)
                img.thumbnail((1600, 1600))
                img.save(out_path, "JPEG", quality=92, optimize=True)
            raw_path.unlink(missing_ok=True)
            return out_path

        video_ext = ext if ext in VIDEO_EXTS else ".mp4"
        out_path = out_dir / f"{slug}_{index}{video_ext}"
        raw_path.replace(out_path)
        return out_path
    except Exception as exc:
        logger.info("meme_media processing failed for %s: %s", url, exc)
        raw_path.unlink(missing_ok=True)
        return None


def meme_media_tool(args: dict, **_kw) -> str:
    urls = args.get("urls") or []
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list):
        return json.dumps({"error": "urls must be a list"}, ensure_ascii=False)

    cleaned_urls: list[str] = []
    seen: set[str] = set()
    for item in urls:
        url = str(item or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        cleaned_urls.append(url)
        if len(cleaned_urls) >= MAX_URLS:
            break

    if not cleaned_urls:
        return json.dumps({"error": "no urls provided"}, ensure_ascii=False)

    title = _clean_title(args.get("title", ""))
    caption = _clean_title(args.get("caption", ""))
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    slug = f"meme_media_{today}_{_safe_slug(title or cleaned_urls[0])}"
    out_dir = _active_profile_home() / "media" / "memes" / "interactive"
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for index, url in enumerate(cleaned_urls):
        path = _download(url, out_dir, slug, index)
        if path is not None:
            paths.append(path)

    if not paths:
        return json.dumps(
            {"error": "could not download any valid image/video media"},
            ensure_ascii=False,
        )

    lines = [f"MEDIA:{path}" for path in paths]
    if title:
        lines.append(title)
    if caption:
        lines.append(caption)
    return "\n".join(lines)


MEME_MEDIA_SCHEMA = {
    "type": "function",
    "function": {
        "name": "meme_media",
        "description": (
            "Download curated meme image/video URLs into the active profile media "
            "folder and return MEDIA:/absolute/path tags for native Telegram "
            "attachments. Use this for meme pictures/videos instead of Markdown "
            "image syntax, plain URLs, or link lists. After calling it, include "
            "the returned MEDIA lines in the final answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Direct image/video URLs to attach, already selected as real meme media.",
                    "minItems": 1,
                    "maxItems": MAX_URLS,
                },
                "title": {
                    "type": "string",
                    "description": "Optional meme title to append after MEDIA lines.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional short caption to append after the title.",
                },
            },
            "required": ["urls"],
            "additionalProperties": False,
        },
    },
}


registry.register(
    name="meme_media",
    toolset="meme_media",
    schema=MEME_MEDIA_SCHEMA,
    handler=meme_media_tool,
    description="Download meme URLs and return MEDIA tags for native attachment delivery.",
    max_result_size_chars=12000,
)
