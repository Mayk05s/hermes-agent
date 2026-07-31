"""Google Gemini image generation backend.

Uses the official Gemini API ``generateContent`` endpoint with
``gemini-2.5-flash-image`` (Nano Banana). Authentication is via
``GEMINI_API_KEY`` or ``GOOGLE_API_KEY`` from Google AI Studio; this is
separate from the ``google-gemini-cli`` OAuth provider used for Gemini CLI /
Code Assist text inference.

Selection precedence:
1. ``GEMINI_IMAGE_MODEL`` env var
2. ``image_gen.gemini.model`` in ``config.yaml``
3. ``image_gen.model`` in ``config.yaml`` when it is one of our IDs
4. :data:`DEFAULT_MODEL`
"""

from __future__ import annotations

import logging
import os
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


_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

_MODELS: Dict[str, Dict[str, Any]] = {
    "gemini-2.5-flash-image": {
        "display": "Gemini 2.5 Flash Image (Nano Banana)",
        "speed": "~5-15s",
        "strengths": "Native Gemini image generation and editing",
        "price": "Gemini API / AI Studio quota",
    },
}

DEFAULT_MODEL = "gemini-2.5-flash-image"

_ASPECT_HINTS = {
    "landscape": "Use a landscape 16:9 composition.",
    "square": "Use a square 1:1 composition.",
    "portrait": "Use a portrait 9:16 composition.",
}


def _get_api_key() -> str:
    value = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    if not value.strip():
        try:
            from hermes_cli.config import get_env_value

            value = get_env_value("GEMINI_API_KEY") or get_env_value("GOOGLE_API_KEY") or ""
        except Exception:
            value = ""
    return value.strip()


def _load_image_gen_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def _resolve_model() -> Tuple[str, Dict[str, Any]]:
    env_override = os.environ.get("GEMINI_IMAGE_MODEL")
    if env_override and env_override in _MODELS:
        return env_override, _MODELS[env_override]

    cfg = _load_image_gen_config()
    gemini_cfg = cfg.get("gemini") if isinstance(cfg.get("gemini"), dict) else {}
    candidate: Optional[str] = None
    if isinstance(gemini_cfg, dict):
        value = gemini_cfg.get("model")
        if isinstance(value, str) and value in _MODELS:
            candidate = value
    if candidate is None:
        top = cfg.get("model")
        if isinstance(top, str) and top in _MODELS:
            candidate = top

    if candidate is not None:
        return candidate, _MODELS[candidate]
    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


def _iter_parts(payload: Dict[str, Any]):
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content") or {}
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if isinstance(part, dict):
                yield part


class GeminiImageGenProvider(ImageGenProvider):
    """Google Gemini API image-generation backend."""

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def display_name(self) -> str:
        return "Google Gemini"

    def is_available(self) -> bool:
        if not _get_api_key():
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
                "price": meta["price"],
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Google Gemini",
            "badge": "api key",
            "tag": "Native Gemini image generation via Google AI Studio API key",
            "env_vars": [
                {
                    "key": "GEMINI_API_KEY",
                    "prompt": "Gemini API key",
                    "url": "https://aistudio.google.com/app/apikey",
                },
                {
                    "key": "GOOGLE_API_KEY",
                    "prompt": "Google API key (alias)",
                    "url": "https://aistudio.google.com/app/apikey",
                },
            ],
            "post_setup_hint": (
                "This uses Gemini API / AI Studio quota. It is separate from "
                "`google-gemini-cli` OAuth used for Gemini CLI text inference."
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
                provider="gemini",
                aspect_ratio=aspect,
            )

        api_key = _get_api_key()
        if not api_key:
            return error_response(
                error=(
                    "GEMINI_API_KEY or GOOGLE_API_KEY is not set. Create a "
                    "Google AI Studio API key and configure it in the Gemini "
                    "provider settings."
                ),
                error_type="auth_required",
                provider="gemini",
                aspect_ratio=aspect,
            )

        try:
            import httpx
        except ImportError:
            return error_response(
                error="httpx Python package not installed (pip install httpx)",
                error_type="missing_dependency",
                provider="gemini",
                aspect_ratio=aspect,
            )

        model_id, _meta = _resolve_model()
        effective_prompt = f"{prompt}\n\n{_ASPECT_HINTS.get(aspect, '')}".strip()
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": effective_prompt},
                    ],
                },
            ],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
            },
        }
        url = f"{_API_BASE}/models/{model_id}:generateContent"

        try:
            with httpx.Client(timeout=httpx.Timeout(120.0, connect=20.0)) as client:
                response = client.post(
                    url,
                    headers={
                        "x-goog-api-key": api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if response.status_code >= 400:
                    body = response.text[:1000]
                    return error_response(
                        error=f"Gemini image API returned HTTP {response.status_code}: {body}",
                        error_type="api_error",
                        provider="gemini",
                        model=model_id,
                        prompt=prompt,
                        aspect_ratio=aspect,
                    )
                data = response.json()
        except Exception as exc:
            logger.debug("Gemini image generation failed", exc_info=True)
            return error_response(
                error=f"Gemini image generation failed: {exc}",
                error_type="api_error",
                provider="gemini",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        texts: List[str] = []
        for part in _iter_parts(data):
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
            inline = part.get("inlineData") or part.get("inline_data") or {}
            if isinstance(inline, dict):
                b64 = inline.get("data")
                if isinstance(b64, str) and b64:
                    try:
                        saved_path = save_b64_image(b64, prefix=f"gemini_{model_id}")
                    except Exception as exc:
                        return error_response(
                            error=f"Could not save Gemini image to cache: {exc}",
                            error_type="io_error",
                            provider="gemini",
                            model=model_id,
                            prompt=prompt,
                            aspect_ratio=aspect,
                        )
                    return success_response(
                        image=str(saved_path),
                        model=model_id,
                        prompt=prompt,
                        aspect_ratio=aspect,
                        provider="gemini",
                        extra={"text": "\n".join(texts) if texts else None},
                    )

        return error_response(
            error=(
                "Gemini returned no image data"
                + (f": {' '.join(texts)[:500]}" if texts else "")
            ),
            error_type="empty_response",
            provider="gemini",
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
        )


def register(ctx) -> None:
    ctx.register_image_gen_provider(GeminiImageGenProvider())
