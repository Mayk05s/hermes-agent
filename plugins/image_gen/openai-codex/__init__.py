"""OpenAI image generation backend — ChatGPT/Codex OAuth variant.

Identical model catalog and tier semantics to the ``openai`` image-gen plugin
(``gpt-image-2`` at low/medium/high quality), but routes the request through
the Codex Responses API ``image_generation`` tool instead of the
``images.generate`` REST endpoint. This lets users who are already
authenticated with Codex/ChatGPT generate images without configuring a
separate ``OPENAI_API_KEY``.

Selection precedence for the tier (first hit wins):

1. ``OPENAI_IMAGE_MODEL`` env var (escape hatch for scripts / tests)
2. ``image_gen.openai-codex.model`` in ``config.yaml``
3. ``image_gen.model`` in ``config.yaml`` (when it's one of our tier IDs)
4. :data:`DEFAULT_MODEL` — ``gpt-image-2-medium``

Output is saved as PNG under ``$HERMES_HOME/cache/images/``.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model catalog — mirrors the ``openai`` plugin so the picker UX is identical.
# ---------------------------------------------------------------------------

API_MODEL = "gpt-image-2"

_MODELS: Dict[str, Dict[str, Any]] = {
    "gpt-image-2-low": {
        "display": "GPT Image 2 (Low)",
        "speed": "~15s",
        "strengths": "Fast iteration, lowest cost",
        "quality": "low",
    },
    "gpt-image-2-medium": {
        "display": "GPT Image 2 (Medium)",
        "speed": "~40s",
        "strengths": "Balanced — default",
        "quality": "medium",
    },
    "gpt-image-2-high": {
        "display": "GPT Image 2 (High)",
        "speed": "~2min",
        "strengths": "Highest fidelity, strongest prompt adherence",
        "quality": "high",
    },
}

DEFAULT_MODEL = "gpt-image-2-medium"

_SIZES = {
    "landscape": "1536x1024",
    "square": "1024x1024",
    "portrait": "1024x1536",
}

# Codex Responses surface used for the request. The chat model itself is only
# the host that calls the ``image_generation`` tool; the actual image work is
# done by ``API_MODEL``.
_CODEX_CHAT_MODEL = "gpt-5.4"
_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
_CODEX_INSTRUCTIONS = (
    "You are an assistant that must fulfill image generation requests by "
    "using the image_generation tool when provided."
)
_RATE_LIMIT_RETRY_DELAYS = (0.5, 1.5)
_MAX_REFERENCE_IMAGES = 10
_MAX_REFERENCE_IMAGE_BYTES = 50 * 1024 * 1024


class ReferenceImageError(ValueError):
    """A supplied reference image cannot be safely sent to GPT Image."""


class CodexImageGenerationError(RuntimeError):
    """Structured error raised from Codex image-generation SSE events."""

    def __init__(self, message: str, *, code: str = "api_error") -> None:
        self.raw_message = message
        self.code = code
        self.error_type = _classify_codex_error(code, message)
        super().__init__(_sanitize_codex_error_message(message))


def _classify_codex_error(code: Optional[str], message: str) -> str:
    text = f"{code or ''} {message or ''}".lower()
    if "rate_limit" in text or "rate limit" in text:
        return "rate_limit"
    if any(marker in text for marker in ("unauthorized", "invalid_grant", "401")):
        return "auth_error"
    return "api_error"


def _sanitize_codex_error_message(message: str) -> str:
    """Remove backend-internal identifiers before returning errors to chat."""
    text = str(message or "").strip()
    text = re.sub(r"\s+in organization\s+org-[A-Za-z0-9_-]+", "", text)
    text = re.sub(r"\s+Visit\s+https?://\S+.*$", "", text)
    return text or "Codex image backend returned an error"


# ---------------------------------------------------------------------------
# Config + auth helpers
# ---------------------------------------------------------------------------


def _load_image_gen_config() -> Dict[str, Any]:
    """Read ``image_gen`` from config.yaml (returns {} on any failure)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def _resolve_model() -> Tuple[str, Dict[str, Any]]:
    """Decide which tier to use and return ``(model_id, meta)``."""
    import os

    env_override = os.environ.get("OPENAI_IMAGE_MODEL")
    if env_override and env_override in _MODELS:
        return env_override, _MODELS[env_override]

    cfg = _load_image_gen_config()
    sub = cfg.get("openai-codex") if isinstance(cfg.get("openai-codex"), dict) else {}
    candidate: Optional[str] = None
    if isinstance(sub, dict):
        value = sub.get("model")
        if isinstance(value, str) and value in _MODELS:
            candidate = value
    if candidate is None:
        top = cfg.get("model")
        if isinstance(top, str) and top in _MODELS:
            candidate = top

    if candidate is not None:
        return candidate, _MODELS[candidate]

    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


def _read_codex_access_token() -> Optional[str]:
    """Return a usable Codex OAuth token, or None.

    Delegates to the canonical reader in ``agent.auxiliary_client`` so token
    expiry, credential pool selection, and JWT decoding stay in one place.
    """
    try:
        from agent.auxiliary_client import _read_codex_access_token as _reader

        token = _reader()
        if isinstance(token, str) and token.strip():
            return token.strip()
        return None
    except Exception as exc:
        logger.debug("Could not resolve Codex access token: %s", exc)
        return None


def _sniff_reference_image_mime(raw: bytes) -> Optional[str]:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _prepare_reference_image_parts(reference_images: Any) -> List[Dict[str, Any]]:
    """Convert ordered local paths/URLs into Responses ``input_image`` parts."""
    if isinstance(reference_images, str):
        candidates = [reference_images]
    elif isinstance(reference_images, (list, tuple)):
        candidates = list(reference_images)
    elif reference_images is None:
        candidates = []
    else:
        raise ReferenceImageError("reference_images must be an array of image paths or URLs")

    parts: List[Dict[str, Any]] = []
    seen = set()
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, str) or not candidate.strip():
            raise ReferenceImageError(f"Reference image {index} must be a non-empty string")
        reference = candidate.strip()
        if reference in seen:
            continue
        seen.add(reference)
        if len(parts) >= _MAX_REFERENCE_IMAGES:
            raise ReferenceImageError(
                f"At most {_MAX_REFERENCE_IMAGES} reference images are supported"
            )

        if reference.startswith("data:image/"):
            if ";base64," not in reference[:100]:
                raise ReferenceImageError(
                    f"Reference image {index} must be a base64 data:image URL"
                )
            image_url = reference
        elif reference.startswith(("https://", "http://")):
            image_url = reference
        else:
            path = Path(reference).expanduser()
            if not path.is_absolute():
                raise ReferenceImageError(
                    f"Reference image {index} must use an absolute local path"
                )
            try:
                stat = path.stat()
            except OSError as exc:
                raise ReferenceImageError(
                    f"Reference image {index} is not readable: {path}"
                ) from exc
            if not path.is_file():
                raise ReferenceImageError(
                    f"Reference image {index} is not a file: {path}"
                )
            if stat.st_size > _MAX_REFERENCE_IMAGE_BYTES:
                raise ReferenceImageError(
                    f"Reference image {index} exceeds the 50 MB limit"
                )
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise ReferenceImageError(
                    f"Reference image {index} is not readable: {path}"
                ) from exc
            mime = _sniff_reference_image_mime(raw)
            if mime is None:
                raise ReferenceImageError(
                    f"Reference image {index} is not a supported PNG, JPEG, GIF, or WebP file"
                )
            encoded = base64.b64encode(raw).decode("ascii")
            image_url = f"data:{mime};base64,{encoded}"

        parts.append({
            "type": "input_image",
            "image_url": image_url,
            "detail": "auto",
        })
    return parts


def _build_responses_payload(
    *,
    prompt: str,
    size: str,
    quality: str,
    reference_image_parts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the Codex Responses request body for an image_generation call."""
    content: List[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    content.extend(reference_image_parts or [])
    return {
        "model": _CODEX_CHAT_MODEL,
        "store": False,
        "instructions": _CODEX_INSTRUCTIONS,
        "input": [{
            "type": "message",
            "role": "user",
            "content": content,
        }],
        "tools": [{
            "type": "image_generation",
            "model": API_MODEL,
            "size": size,
            "quality": quality,
            "output_format": "png",
            "background": "opaque",
            "partial_images": 1,
        }],
        "tool_choice": {
            "type": "allowed_tools",
            "mode": "required",
            "tools": [{"type": "image_generation"}],
        },
        "stream": True,
    }


def _extract_image_b64(value: Any) -> Optional[str]:
    """Return the newest image b64 embedded in a Responses event payload."""
    found: Optional[str] = None
    if isinstance(value, dict):
        if value.get("type") == "image_generation_call":
            result = value.get("result")
            if isinstance(result, str) and result:
                found = result
        partial = value.get("partial_image_b64")
        if isinstance(partial, str) and partial:
            found = partial
        for child in value.values():
            nested = _extract_image_b64(child)
            if nested:
                found = nested
    elif isinstance(value, list):
        for child in value:
            nested = _extract_image_b64(child)
            if nested:
                found = nested
    return found


def _format_error_detail(value: Any) -> Optional[Tuple[str, str]]:
    """Return ``(message, code)`` from an OpenAI/Codex error-shaped dict."""
    if isinstance(value, dict):
        message = value.get("message")
        code = value.get("code") or value.get("type") or "api_error"
        if isinstance(message, str) and message.strip():
            return message.strip(), str(code)
        if isinstance(code, str) and code.strip() and code != "api_error":
            return code.strip(), code.strip()
        return None
    if isinstance(value, str) and value.strip():
        return value.strip(), "api_error"
    return None


def _extract_codex_error(value: Any) -> Optional[Tuple[str, str]]:
    """Return the first backend error carried by a streamed Responses event."""
    if isinstance(value, dict):
        event_type = value.get("type")
        if event_type == "error":
            detail = _format_error_detail(value.get("error") or value)
            if detail:
                return detail

        if event_type == "response.failed":
            response = value.get("response")
            if isinstance(response, dict):
                detail = _format_error_detail(response.get("error"))
                if detail:
                    return detail

        error = value.get("error")
        if isinstance(error, dict):
            detail = _format_error_detail(error)
            if detail:
                return detail

        for child in value.values():
            nested = _extract_codex_error(child)
            if nested:
                return nested
    elif isinstance(value, list):
        for child in value:
            nested = _extract_codex_error(child)
            if nested:
                return nested
    return None


def _iter_sse_json(response: Any):
    """Yield JSON payloads from an SSE response without OpenAI SDK parsing.

    The ChatGPT/Codex backend can emit image-generation events newer than the
    pinned Python SDK understands. Parsing raw SSE keeps this provider tolerant
    of those event-shape changes.
    """
    event_name: Optional[str] = None
    data_lines: List[str] = []

    def flush():
        nonlocal event_name, data_lines
        if not data_lines:
            event_name = None
            return None
        raw = "\n".join(data_lines).strip()
        event = event_name
        event_name = None
        data_lines = []
        if not raw or raw == "[DONE]":
            return None
        payload = json.loads(raw)
        if isinstance(payload, dict) and event and "type" not in payload:
            payload["type"] = event
        return payload

    for line in response.iter_lines():
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = str(line)
        if line == "":
            payload = flush()
            if payload is not None:
                yield payload
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())

    payload = flush()
    if payload is not None:
        yield payload


def _collect_image_b64(
    token: str,
    *,
    prompt: str,
    size: str,
    quality: str,
    reference_image_parts: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    """Stream a Codex Responses image_generation call and return the b64 image."""
    import httpx
    from agent.auxiliary_client import _codex_cloudflare_headers

    headers = _codex_cloudflare_headers(token)
    headers.update({
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    payload = _build_responses_payload(
        prompt=prompt,
        size=size,
        quality=quality,
        reference_image_parts=reference_image_parts,
    )
    timeout = httpx.Timeout(300.0, connect=30.0, read=300.0, write=30.0, pool=30.0)

    image_b64: Optional[str] = None
    with httpx.Client(timeout=timeout, headers=headers) as http:
        with http.stream("POST", f"{_CODEX_BASE_URL}/responses", json=payload) as response:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                exc.response.read()
                body = exc.response.text[:500]
                raise RuntimeError(
                    f"Codex Responses API returned HTTP {exc.response.status_code}: {body}"
                ) from exc
            for event in _iter_sse_json(response):
                error = _extract_codex_error(event)
                if error:
                    message, code = error
                    raise CodexImageGenerationError(message, code=code)

                found = _extract_image_b64(event)
                if found:
                    image_b64 = found

    return image_b64


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class OpenAICodexImageGenProvider(ImageGenProvider):
    """gpt-image-2 routed through ChatGPT/Codex OAuth instead of an API key."""

    @property
    def name(self) -> str:
        return "openai-codex"

    @property
    def display_name(self) -> str:
        return "OpenAI (Codex auth)"

    def is_available(self) -> bool:
        if not _read_codex_access_token():
            return False
        try:
            import httpx  # noqa: F401
        except ImportError:
            return False
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": "varies",
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "OpenAI (Codex auth)",
            "badge": "free",
            "tag": "gpt-image-2 via ChatGPT/Codex OAuth — no API key required",
            "env_vars": [],
            "post_setup_hint": (
                "Sign in with `hermes auth codex` (or `hermes setup` → Codex) "
                "if you haven't already. No API key needed."
            ),
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider="openai-codex",
                aspect_ratio=aspect,
            )

        if not _read_codex_access_token():
            return error_response(
                error=(
                    "No Codex/ChatGPT OAuth credentials available. Run "
                    "`hermes auth codex` (or `hermes setup` → Codex) to sign in."
                ),
                error_type="auth_required",
                provider="openai-codex",
                aspect_ratio=aspect,
            )

        try:
            import httpx  # noqa: F401
        except ImportError:
            return error_response(
                error="httpx Python package not installed (pip install httpx)",
                error_type="missing_dependency",
                provider="openai-codex",
                aspect_ratio=aspect,
            )

        tier_id, meta = _resolve_model()
        size = _SIZES.get(aspect, _SIZES["square"])
        try:
            reference_image_parts = _prepare_reference_image_parts(
                kwargs.get("reference_images")
            )
        except ReferenceImageError as exc:
            return error_response(
                error=str(exc),
                error_type="invalid_reference_image",
                provider="openai-codex",
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        token = _read_codex_access_token()
        if not token:
            return error_response(
                error=(
                    "No Codex/ChatGPT OAuth credentials available. Run "
                    "`hermes auth codex` (or `hermes setup` → Codex) to sign in."
                ),
                error_type="auth_required",
                provider="openai-codex",
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        b64: Optional[str] = None
        attempts = len(_RATE_LIMIT_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                collect_kwargs: Dict[str, Any] = {
                    "prompt": prompt,
                    "size": size,
                    "quality": meta["quality"],
                }
                if reference_image_parts:
                    collect_kwargs["reference_image_parts"] = reference_image_parts
                b64 = _collect_image_b64(token, **collect_kwargs)
                break
            except CodexImageGenerationError as exc:
                logger.debug(
                    "Codex image generation failed (code=%s, raw=%s)",
                    exc.code,
                    exc.raw_message,
                    exc_info=True,
                )
                if exc.error_type == "rate_limit" and attempt < len(_RATE_LIMIT_RETRY_DELAYS):
                    delay = _RATE_LIMIT_RETRY_DELAYS[attempt]
                    logger.info(
                        "Codex image generation hit rate limit; retrying in %.1fs",
                        delay,
                    )
                    time.sleep(delay)
                    continue
                prefix = (
                    "OpenAI image generation rate limit"
                    if exc.error_type == "rate_limit"
                    else "OpenAI image generation via Codex auth failed"
                )
                return error_response(
                    error=f"{prefix}: {exc}",
                    error_type=exc.error_type,
                    provider="openai-codex",
                    model=tier_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            except Exception as exc:
                logger.debug("Codex image generation failed", exc_info=True)
                return error_response(
                    error=f"OpenAI image generation via Codex auth failed: {exc}",
                    error_type="api_error",
                    provider="openai-codex",
                    model=tier_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )

        if not b64:
            return error_response(
                error="Codex response contained no image_generation_call result",
                error_type="empty_response",
                provider="openai-codex",
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            saved_path = save_b64_image(b64, prefix=f"openai_codex_{tier_id}")
        except Exception as exc:
            return error_response(
                error=f"Could not save image to cache: {exc}",
                error_type="io_error",
                provider="openai-codex",
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        return success_response(
            image=str(saved_path),
            model=tier_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="openai-codex",
            extra={
                "size": size,
                "quality": meta["quality"],
                "reference_images_count": len(reference_image_parts),
            },
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry point — register the Codex-backed image-gen provider."""
    ctx.register_image_gen_provider(OpenAICodexImageGenProvider())
