"""nano-banana image generation backend — Google Gemini image models via proxy.

Serves Google's "nano banana" Gemini image family through the local
OpenAI-compatible proxy (e.g. ``http://127.0.0.1:4000/v1``), which authenticates
to the upstream image service server-side. The client sends only
``Authorization: Bearer <proxy token>`` — no Google credential is ever handled
client-side.

Protocol (OpenAI ``/chat/completions`` image output — the same shape the
``openrouter`` backend speaks):

- ``POST {base_url}/chat/completions`` with ``modalities: ["image", "text"]``,
  the prompt (and any source/reference images) as ``messages[0].content`` parts,
  and an optional ``image_config.aspect_ratio``.
- The generated image comes back at
  ``choices[0].message.images[0].image_url.url`` as a ``data:image/...;base64``
  URI (``message.content`` is ``null`` on this protocol). It is decoded and
  saved under ``$HERMES_HOME/cache/images/`` via the framework's
  ``save_b64_image``; the tool returns the path, delivered as ``MEDIA:/path``.

Unified generate + edit + reference: when ``image_url`` (primary source to edit)
or ``reference_image_urls`` (style/subject references, e.g. a likeness for
character consistency) are present, they are inlined as ``image_url`` content
parts and the call routes to image-to-image / editing; otherwise text-to-image.

Model routing is Pro-default and fully config-driven (precedence, first hit
wins):

1. ``model`` kwarg from dispatch (explicit caller override)
2. ``NANO_BANANA_IMAGE_MODEL`` env (escape hatch for scripts / tests)
3. ``image_gen.nano-banana.model`` in config.yaml
4. ``image_gen.model`` in config.yaml (only when it names a known model id)
5. :data:`DEFAULT_MODEL` — ``gemini-3-pro-image`` (Pro; quality default)

The model catalog is data, not a two-model assumption: Nano Banana 2 Lite
(``gemini-3.1-flash-lite-image``) is listed as a documented slot that drops in
with zero code change once the proxy serves it.

Prompt craft lives in the ``nano-banana-prompting`` skill, not in this backend.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)
from hermes_cli.runtime_provider import resolve_runtime_provider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model catalog — data, not a two-model assumption.
# ---------------------------------------------------------------------------

# Pro is the quality default (cost is a non-issue; Pro reasons for the best
# result). Flash is the one-parameter fast path (sub-few-second latency). Lite
# is a documented slot — not yet on the proxy — that becomes selectable via
# config the moment the proxy serves it, with no code change here.
_MODELS: Dict[str, Dict[str, Any]] = {
    "gemini-3-pro-image": {
        "display": "Nano Banana Pro (Gemini 3 Pro Image)",
        "speed": "~18s",
        "strengths": "Highest fidelity; strongest prompt adherence — quality default",
    },
    "gemini-3.1-flash-image": {
        "display": "Nano Banana 2 (Gemini 3.1 Flash Image)",
        "speed": "~2s",
        "strengths": "Fast path — sub-few-second latency for quick iteration",
    },
    # Documented zero-code slot. Not on the proxy yet; selecting it via
    # config.yaml (image_gen.nano-banana.model) or the model kwarg will route to
    # it automatically once the proxy serves it.
    "gemini-3.1-flash-lite-image": {
        "display": "Nano Banana 2 Lite (Gemini 3.1 Flash Lite Image)",
        "speed": "~1s",
        "strengths": "Lightest/cheapest fast path (drops in when proxy serves it)",
    },
}

DEFAULT_MODEL = "gemini-3-pro-image"

# Runtime (proxy) resolution: the proxy is a configured custom_providers entry.
# Override via image_gen.nano-banana.runtime in config.yaml if the entry is
# named differently.
_DEFAULT_RUNTIME = "custom:vertex-llm-proxy"

_MODEL_ENV_VAR = "NANO_BANANA_IMAGE_MODEL"

# Semantic aspect ratio (the image_gen contract) → proxy image_config strings.
# Verified against the live proxy: these change output dimensions on the
# text-to-image path (1:1→1024×1024, 16:9→1376×768, 9:16→768×1376). On the edit
# path the model tends to preserve the source image's dimensions regardless.
_ASPECT_RATIOS = {
    "square": "1:1",
    "landscape": "16:9",
    "portrait": "9:16",
}

# Gemini image models accept up to 3 input images per request; clamp references
# so we never overflow the model's limit.
_MAX_REFERENCE_IMAGES = 3

# Pro reasons for a while; give the request real headroom before we treat it as
# hung. The proxy call is a single request (no client-side fallback chain).
_REQUEST_TIMEOUT = 300.0


def _load_image_gen_config() -> Dict[str, Any]:
    """Read the ``image_gen`` section from config.yaml (``{}`` on failure)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:  # noqa: BLE001 - config is best-effort
        logger.debug("could not load image_gen config: %s", exc)
        return {}


def _to_image_url_part(ref: str) -> Optional[str]:
    """Turn a reference (local path or http URL) into an ``image_url`` value.

    Remote / data URIs pass through unchanged; local files are inlined as base64
    data URIs so the request is self-contained (the proxy can't reach a path on
    our disk). Returns ``None`` when the reference can't be read.
    """
    ref = str(ref or "").strip()
    if not ref:
        return None
    if ref.startswith(("http://", "https://", "data:")):
        return ref
    path = Path(ref)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.debug("could not read reference image %s: %s", ref, exc)
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _extract_images(payload: Dict[str, Any]) -> List[str]:
    """Pull generated image URLs from a chat-completions response.

    The image is at ``choices[0].message.images[].image_url.url`` (a base64 data
    URI); ``message.content`` is ``null`` on this protocol.
    """
    out: List[str] = []
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list):
        return out
    for choice in choices:
        message = choice.get("message") if isinstance(choice, dict) else None
        images = message.get("images") if isinstance(message, dict) else None
        if not isinstance(images, list):
            continue
        for image in images:
            if not isinstance(image, dict):
                continue
            image_url = image.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else None
            if isinstance(url, str) and url.strip():
                out.append(url.strip())
    return out


class NanoBananaImageGenProvider(ImageGenProvider):
    """Google Gemini image models served through the local proxy."""

    @property
    def name(self) -> str:
        return "nano-banana"

    @property
    def display_name(self) -> str:
        return "Nano Banana (Gemini image)"

    def _runtime_name(self) -> str:
        """Which runtime provider supplies ``(base_url, api_key)`` for the proxy.

        Defaults to the ``vertex-llm-proxy`` custom_providers entry; overridable
        via ``image_gen.nano-banana.runtime`` for a differently-named entry.
        """
        sub = _load_image_gen_config().get("nano-banana")
        if isinstance(sub, dict):
            value = sub.get("runtime")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return _DEFAULT_RUNTIME

    def _resolve_runtime(self) -> Dict[str, Any]:
        """Resolve ``(base_url, api_key)`` via the shared runtime resolver."""
        return resolve_runtime_provider(requested=self._runtime_name())

    def is_available(self) -> bool:
        try:
            runtime = self._resolve_runtime()
        except Exception as exc:  # noqa: BLE001 - resolution failure → unavailable
            logger.debug("nano-banana runtime resolution failed: %s", exc)
            return False
        return bool(str(runtime.get("api_key") or "").strip())

    def capabilities(self) -> Dict[str, Any]:
        return {
            "modalities": ["text", "image"],
            "max_reference_images": _MAX_REFERENCE_IMAGES,
        }

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return self._resolve_model()

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Nano Banana (Gemini image)",
            "badge": "proxy",
            "tag": (
                "Google Gemini image (Pro default, Flash fast-path) via the local "
                "OpenAI-compatible proxy; text-to-image, edit, and reference "
                "grounding. See the nano-banana-prompting skill for prompt craft."
            ),
            "env_vars": [],
            "post_setup_hint": (
                "Set image_gen.provider: nano-banana. The proxy base_url + token "
                "come from the vertex-llm-proxy custom_providers entry (override "
                "with image_gen.nano-banana.runtime). Load the nano-banana-prompting "
                "skill for prompt best-practices."
            ),
        }

    def _resolve_model(self, explicit: Optional[str] = None) -> str:
        """Pick the model id per the documented precedence (first hit wins)."""
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        env_override = os.environ.get(_MODEL_ENV_VAR, "").strip()
        if env_override:
            return env_override
        cfg = _load_image_gen_config()
        scoped = cfg.get("nano-banana") if isinstance(cfg.get("nano-banana"), dict) else {}
        if isinstance(scoped, dict):
            value = scoped.get("model")
            if isinstance(value, str) and value.strip():
                return value.strip()
        top = cfg.get("model")
        # Only honour a top-level image_gen.model when it names a model we know;
        # otherwise it belongs to a different backend and must not hijack us.
        if isinstance(top, str) and top.strip() in _MODELS:
            return top.strip()
        return DEFAULT_MODEL

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        import requests

        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        proxy_aspect = _ASPECT_RATIOS.get(aspect, "1:1")
        model_id = self._resolve_model(kwargs.get("model"))

        # Collect every source/reference image. The generic tool surface uses
        # image_url / reference_image_urls; some callers (e.g. the pet
        # generator) pass local paths via reference_images. Accept all.
        references: List[str] = []
        for ref in kwargs.get("reference_images") or []:
            references.append(str(ref))
        if image_url:
            references.append(str(image_url))
        for ref in normalize_reference_images(reference_image_urls) or []:
            references.append(str(ref))
        has_source = bool(references)
        modality = "image" if has_source else "text"

        if not prompt and not has_source:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider="nano-banana",
                model=model_id,
                aspect_ratio=aspect,
            )

        try:
            runtime = self._resolve_runtime()
        except Exception as exc:  # noqa: BLE001
            return error_response(
                error=f"Could not resolve nano-banana proxy credentials: {exc}",
                error_type="missing_api_key",
                provider="nano-banana",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        api_key = str(runtime.get("api_key") or "").strip()
        base_url = str(runtime.get("base_url") or "").strip().rstrip("/")
        if not api_key or not base_url:
            return error_response(
                error=(
                    "No nano-banana proxy credentials found. Configure the "
                    "vertex-llm-proxy custom_providers entry (base_url + token) "
                    "or set image_gen.nano-banana.runtime to your proxy entry."
                ),
                error_type="missing_api_key",
                provider="nano-banana",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for ref in references[:_MAX_REFERENCE_IMAGES]:
            part = _to_image_url_part(ref)
            if part:
                content.append({"type": "image_url", "image_url": {"url": part}})

        payload: Dict[str, Any] = {
            "model": model_id,
            "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": content}],
            "image_config": {"aspect_ratio": proxy_aspect},
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except requests.Timeout:
            return error_response(
                error=f"nano-banana image generation timed out ({int(_REQUEST_TIMEOUT)}s)",
                error_type="timeout",
                provider="nano-banana",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.ConnectionError as exc:
            return error_response(
                error=f"nano-banana proxy connection error: {exc}",
                error_type="connection_error",
                provider="nano-banana",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.HTTPError as exc:
            resp = exc.response
            status = resp.status_code if resp is not None else 0
            if resp is not None:
                try:
                    err_msg = resp.json().get("error", {}).get("message", resp.text[:300])
                except Exception:  # noqa: BLE001
                    err_msg = resp.text[:300]
            else:
                err_msg = str(exc)
            logger.error("nano-banana image gen failed (%s) on %s: %s", status, model_id, err_msg)
            return error_response(
                error=f"nano-banana image generation failed ({status}): {err_msg}",
                error_type="api_error",
                provider="nano-banana",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            result = response.json()
        except Exception as exc:  # noqa: BLE001
            return error_response(
                error=f"nano-banana proxy returned invalid JSON: {exc}",
                error_type="invalid_response",
                provider="nano-banana",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        images = _extract_images(result)
        if not images:
            return error_response(
                error=(
                    f"nano-banana returned no image. Ensure the model '{model_id}' "
                    "supports image output on the proxy."
                ),
                error_type="empty_response",
                provider="nano-banana",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        first = images[0]
        try:
            if first.startswith("data:"):
                b64 = first.split(",", 1)[1] if "," in first else ""
                saved_path = save_b64_image(b64, prefix="nano_banana")
            else:
                # The proxy returns inline base64 data URIs; a bare URL is
                # unexpected but handled for robustness.
                from agent.image_gen_provider import save_url_image

                saved_path = save_url_image(first, prefix="nano_banana")
        except Exception as exc:  # noqa: BLE001
            return error_response(
                error=f"Could not save generated image: {exc}",
                error_type="io_error",
                provider="nano-banana",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        return success_response(
            image=str(saved_path),
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="nano-banana",
            modality=modality,
        )


def register(ctx: Any) -> None:
    """Plugin entry point — register the nano-banana image gen provider."""
    ctx.register_image_gen_provider(NanoBananaImageGenProvider())
