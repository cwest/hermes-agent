"""nano-banana image generation backend — Google Gemini image models via proxy.

Serves Google's "nano banana" Gemini image family through the local
OpenAI-compatible proxy (e.g. ``http://127.0.0.1:4000/v1``), which authenticates
to the upstream image service server-side. The client sends only
``Authorization: Bearer <proxy token>`` — no Google credential is ever handled
client-side.

Two request protocols, split by whether the call has an input image — because
the proxy honors output resolution on only ONE of them:

TEXT-TO-IMAGE → ``POST {base_url}/v1/images/generations`` (OpenAI images API):

- Body: ``{"model": id, "prompt": ..., "imageConfig": {"imageSize": res,
  "aspectRatio": ratio}}`` — a NESTED ``imageConfig``. This is the path where
  LiteLLM (>=1.92.0) maps ``imageConfig`` → Vertex
  ``generationConfig.imageConfig`` for pro/flash/lite (model-agnostic, no
  capability flag), so ``gemini-3-pro-image`` actually honors 4K here.
- The chat/completions path has NO ``imageConfig``→``generationConfig`` mapping
  (verified against LiteLLM 1.91.2 AND 1.92.0 source), so ``image_size`` on chat
  is INERT for Pro — it silently returns ~1K regardless. That is the bug this
  split fixes. FLAT ``imageSize``/``image_size`` at the top level are DROPPED by
  the proxy; only the nested ``imageConfig`` is honored.
- Response is the images-API shape: ``data[0].b64_json`` (base64 PNG) and/or
  ``data[0].url``. ``b64_json`` is decoded via ``save_b64_image``; a bare ``url``
  is fetched via ``save_url_image``. The tool returns the saved path
  (``MEDIA:/path``).
- Requires the proxy on LiteLLM >=1.92.0. If an older proxy rejects the nested
  ``imageConfig``, the request is retried once WITHOUT it so generation still
  lands (falls back to default geometry rather than hard-failing).

EDIT / reference → ``POST {base_url}/chat/completions`` (unchanged):

- When ``image_url`` (primary source to edit) or ``reference_image_urls``
  (style/subject references, e.g. a likeness for character consistency) are
  present, they are inlined as ``image_url`` content parts and the call routes
  to image-to-image / editing on the chat path with ``modalities: ["image",
  "text"]``. The image comes back at
  ``choices[0].message.images[0].image_url.url`` (``message.content`` is
  ``null`` on this protocol).
- Why keep edits on chat: the images-API endpoint has no clean input-image
  contract at 1.92.0 (image input lives on the multipart ``/v1/images/edits``
  route, unverified against this proxy). The chat edit path works today, and
  resolution matters less for edits because the model preserves the source
  image's dimensions regardless. So text-to-image — where 4K actually matters —
  moves to the images path, and edit/reference stays on chat.

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

Resolution is config-only (no per-call parameter on the ``image_generate`` tool
schema): ``image_gen.nano-banana.resolution`` in config.yaml selects the output
size (``1K``/``2K``/``4K``, uppercase K), defaulting to ``4K``. On the
text-to-image path it is sent as ``imageConfig.imageSize`` on
``/v1/images/generations`` — the only place the proxy actually honors it (see
above). A per-model cap degrades gracefully (a capped model like Lite = 1K
clamps down rather than erroring), and if the proxy rejects the nested
``imageConfig`` (older LiteLLM) the request is retried once WITHOUT it so the
generation still lands. On the EDIT path resolution is not sent — the model
preserves the source image's dimensions regardless.

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
    save_url_image,
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
        "max_resolution": "4K",
    },
    "gemini-3.1-flash-image": {
        "display": "Nano Banana 2 (Gemini 3.1 Flash Image)",
        "speed": "~2s",
        "strengths": "Fast path — sub-few-second latency for quick iteration",
        "max_resolution": "4K",
    },
    # Documented zero-code slot. Not on the proxy yet; selecting it via
    # config.yaml (image_gen.nano-banana.model) or the model kwarg will route to
    # it automatically once the proxy serves it. Per Google's docs Lite tops out
    # at 1K, so a higher configured resolution degrades gracefully to its cap.
    "gemini-3.1-flash-lite-image": {
        "display": "Nano Banana 2 Lite (Gemini 3.1 Flash Lite Image)",
        "speed": "~1s",
        "strengths": "Lightest/cheapest fast path (drops in when proxy serves it)",
        "max_resolution": "1K",
    },
}

DEFAULT_MODEL = "gemini-3-pro-image"

# Resolution ladder for the proxy's image_config.image_size field. VERIFIED
# against the live proxy: image_config.image_size ∈ {"1K","2K","4K"} changes the
# decoded PNG dimensions on the text-to-image path and composes with
# aspect_ratio (e.g. 4K+16:9 → 5504×3072, 4K+1:1 → 4096×4096, 2K+16:9 →
# 2752×1536, 1K+16:9 → 1376×768). The proxy REQUIRES an uppercase "K"
# (lowercase "1k" is rejected) and 400s on an unknown field name (image_config.
# resolution / response_format.image_size are NOT honoured — only
# image_config.image_size). Ordered smallest → largest for cap clamping.
_RESOLUTION_LADDER = ["1K", "2K", "4K"]
_RESOLUTIONS = set(_RESOLUTION_LADDER)

# Config-only, per Casey: every call uses the configured resolution; there is no
# per-call resolution parameter on the image_generate tool schema. Default 4K.
DEFAULT_RESOLUTION = "4K"

_RESOLUTION_ENV_VAR = "NANO_BANANA_IMAGE_RESOLUTION"

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


def _extract_images_api(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pull image items from an OpenAI images-API (/v1/images/generations) response.

    Shape: ``{"data": [{"b64_json": "..."} | {"url": "https://..."}]}``. Returns
    the raw ``data`` item dicts (each carrying ``b64_json`` and/or ``url``) so the
    caller can prefer inline base64 over a bare URL.
    """
    out: List[Dict[str, Any]] = []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return out
    for item in data:
        if isinstance(item, dict) and (item.get("b64_json") or item.get("url")):
            out.append(item)
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

    def _resolve_resolution(self, model: Optional[str] = None) -> str:
        """Pick the output resolution for ``image_config.image_size``.

        Config-only (no per-call parameter): precedence, first hit wins —

        1. ``NANO_BANANA_IMAGE_RESOLUTION`` env (escape hatch for scripts/tests)
        2. ``image_gen.nano-banana.resolution`` in config.yaml
        3. :data:`DEFAULT_RESOLUTION` — ``"4K"``

        The value is normalised to an uppercase ladder token (the proxy rejects
        lowercase ``k``); an out-of-ladder value falls back to the default
        rather than being sent verbatim (which would 400 the proxy). Finally the
        result is clamped to the model's documented ``max_resolution`` so a
        capped model (e.g. Lite = 1K) degrades gracefully instead of erroring.
        """
        raw = os.environ.get(_RESOLUTION_ENV_VAR, "").strip()
        if not raw:
            cfg = _load_image_gen_config()
            scoped = cfg.get("nano-banana") if isinstance(cfg.get("nano-banana"), dict) else {}
            if isinstance(scoped, dict):
                value = scoped.get("resolution")
                if isinstance(value, str) and value.strip():
                    raw = value.strip()

        normalized = raw.upper() if raw else ""
        if normalized not in _RESOLUTIONS:
            if normalized:
                logger.warning(
                    "nano-banana: unknown resolution %r (expected one of %s); "
                    "falling back to %s",
                    raw,
                    ", ".join(_RESOLUTION_LADDER),
                    DEFAULT_RESOLUTION,
                )
            normalized = DEFAULT_RESOLUTION

        return self._clamp_resolution(normalized, model)

    @staticmethod
    def _clamp_resolution(resolution: str, model: Optional[str]) -> str:
        """Degrade ``resolution`` to the model's documented ceiling, if lower."""
        meta = _MODELS.get(model or "") or {}
        cap = meta.get("max_resolution")
        if cap not in _RESOLUTIONS:
            return resolution
        if _RESOLUTION_LADDER.index(resolution) <= _RESOLUTION_LADDER.index(cap):
            return resolution
        logger.info(
            "nano-banana: %s caps resolution at %s; degrading requested %s to %s",
            model,
            cap,
            resolution,
            cap,
        )
        return cap

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        proxy_aspect = _ASPECT_RATIOS.get(aspect, "1:1")
        model_id = self._resolve_model(kwargs.get("model"))
        resolution = self._resolve_resolution(model_id)

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

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # Split by whether we have an input image. Text-to-image goes to the
        # images API (/v1/images/generations) where the proxy honors 4K via a
        # nested imageConfig; edit/reference stays on chat/completions (the
        # images path has no clean input-image contract at LiteLLM 1.92.0). See
        # the module docstring for the full rationale.
        if has_source:
            return self._generate_edit(
                base_url=base_url,
                headers=headers,
                prompt=prompt,
                references=references,
                model_id=model_id,
                proxy_aspect=proxy_aspect,
                resolution=resolution,
                aspect=aspect,
            )
        return self._generate_text_to_image(
            base_url=base_url,
            headers=headers,
            prompt=prompt,
            model_id=model_id,
            proxy_aspect=proxy_aspect,
            resolution=resolution,
            aspect=aspect,
        )

    def _generate_text_to_image(
        self,
        *,
        base_url: str,
        headers: Dict[str, str],
        prompt: str,
        model_id: str,
        proxy_aspect: str,
        resolution: str,
        aspect: str,
    ) -> Dict[str, Any]:
        """Text-to-image via the OpenAI images API (/v1/images/generations).

        This is the ONLY path where LiteLLM maps the nested ``imageConfig`` to
        Vertex ``generationConfig.imageConfig``, so Pro honors the configured
        resolution (4K) here. The chat path silently returns ~1K for Pro.
        """
        payload: Dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            # NESTED imageConfig — flat imageSize/image_size at the top level are
            # dropped by the proxy and would leave Pro stuck at ~1K.
            "imageConfig": {"imageSize": resolution, "aspectRatio": proxy_aspect},
        }
        # base_url already ends in /v1 (the proxy's OpenAI-compatible root), so
        # this resolves to {host}/v1/images/generations — the images-API path.
        url = f"{base_url}/images/generations"

        result = self._post_with_image_config_fallback(
            url=url,
            headers=headers,
            payload=payload,
            model_id=model_id,
            prompt=prompt,
            aspect=aspect,
            resolution=resolution,
        )
        if isinstance(result, dict) and result.get("success") is False:
            return result

        items = _extract_images_api(result)
        if not items:
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

        first = items[0]
        b64 = first.get("b64_json")
        item_url = first.get("url")
        try:
            # Prefer inline base64 (the proxy's default) over a bare URL.
            if isinstance(b64, str) and b64.strip():
                saved_path = save_b64_image(b64, prefix="nano_banana")
            else:
                saved_path = save_url_image(str(item_url), prefix="nano_banana")
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
            modality="text",
            extra={"resolution": resolution},
        )

    def _generate_edit(
        self,
        *,
        base_url: str,
        headers: Dict[str, str],
        prompt: str,
        references: List[str],
        model_id: str,
        proxy_aspect: str,
        resolution: str,
        aspect: str,
    ) -> Dict[str, Any]:
        """Edit / reference via chat/completions (unchanged behavior).

        Kept on the chat path because the images API has no clean input-image
        contract at LiteLLM 1.92.0. Resolution matters less here — the model
        preserves the source image's dimensions regardless — so the existing
        chat ``image_config`` (with its resolution-field fallback) is retained
        to avoid any regression in the currently-working edit flow.
        """
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for ref in references[:_MAX_REFERENCE_IMAGES]:
            part = _to_image_url_part(ref)
            if part:
                content.append({"type": "image_url", "image_url": {"url": part}})

        payload: Dict[str, Any] = {
            "model": model_id,
            "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": content}],
            "image_config": {"aspect_ratio": proxy_aspect, "image_size": resolution},
        }
        url = f"{base_url}/chat/completions"

        result = self._post_with_resolution_fallback(
            url=url,
            headers=headers,
            payload=payload,
            model_id=model_id,
            prompt=prompt,
            aspect=aspect,
            resolution=resolution,
        )
        # A helper failure returns an error_response dict (success is False).
        if isinstance(result, dict) and result.get("success") is False:
            return result

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
            modality="image",
            extra={"resolution": resolution},
        )

    def _post_with_image_config_fallback(
        self,
        *,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        model_id: str,
        prompt: str,
        aspect: str,
        resolution: str,
    ) -> Dict[str, Any]:
        """POST the text-to-image request to the images API; on an
        ``imageConfig`` rejection (an older proxy that predates the nested
        imageConfig mapping), retry once WITHOUT it so the generation still
        lands rather than hard-failing on an un-upgraded proxy.

        Returns the parsed JSON result on success, or an ``error_response`` dict
        (``success`` is ``False``) on a non-recoverable failure.
        """
        result = self._post_once(
            url=url,
            headers=headers,
            payload=payload,
            model_id=model_id,
            prompt=prompt,
            aspect=aspect,
        )
        if (
            isinstance(result, dict)
            and result.get("success") is False
            and result.get("error_type") == "api_error"
            and self._is_resolution_field_error(result.get("error", ""))
            and "imageConfig" in payload
        ):
            logger.warning(
                "nano-banana: proxy rejected imageConfig (imageSize=%s); retrying "
                "without it (falling back to default geometry on an un-upgraded "
                "proxy)",
                resolution,
            )
            fallback = {k: v for k, v in payload.items() if k != "imageConfig"}
            result = self._post_once(
                url=url,
                headers=headers,
                payload=fallback,
                model_id=model_id,
                prompt=prompt,
                aspect=aspect,
            )
        return result

    def _post_with_resolution_fallback(
        self,
        *,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        model_id: str,
        prompt: str,
        aspect: str,
        resolution: str,
    ) -> Dict[str, Any]:
        """POST the generation request; on a resolution-field rejection, retry
        once WITHOUT ``image_config.image_size`` (current no-resolution
        behavior) rather than failing the whole generation.

        Returns the parsed JSON result on success, or an ``error_response`` dict
        (``success`` is ``False``) on a non-recoverable failure.
        """
        result = self._post_once(
            url=url,
            headers=headers,
            payload=payload,
            model_id=model_id,
            prompt=prompt,
            aspect=aspect,
        )
        # Graceful degradation: if the proxy rejected the resolution field
        # specifically, drop it and retry once so the generation still lands.
        if (
            isinstance(result, dict)
            and result.get("success") is False
            and result.get("error_type") == "api_error"
            and self._is_resolution_field_error(result.get("error", ""))
            and "image_size" in payload.get("image_config", {})
        ):
            logger.warning(
                "nano-banana: proxy rejected image_size=%s; retrying without it "
                "(falling back to default geometry)",
                resolution,
            )
            fallback = {
                **payload,
                "image_config": {
                    k: v
                    for k, v in payload.get("image_config", {}).items()
                    if k != "image_size"
                },
            }
            result = self._post_once(
                url=url,
                headers=headers,
                payload=fallback,
                model_id=model_id,
                prompt=prompt,
                aspect=aspect,
            )
        return result

    @staticmethod
    def _is_resolution_field_error(message: str) -> bool:
        """True when an API error is about the resolution field itself.

        Matches the field names used across both request protocols and proxy
        versions — the chat path's ``image_size`` and the images path's nested
        ``imageConfig`` / ``imageSize`` (LiteLLM error text is not always
        snake_case). Scopes the fallback to genuine field-contract rejections so
        an unrelated 400 (e.g. a safety block) still surfaces as an error
        instead of being masked by a retry.
        """
        low = str(message or "").lower()
        return "image_size" in low or "imagesize" in low or "imageconfig" in low

    def _post_once(
        self,
        *,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        model_id: str,
        prompt: str,
        aspect: str,
    ) -> Dict[str, Any]:
        """Single POST + parse. Returns parsed JSON or an ``error_response``."""
        import requests

        try:
            response = requests.post(
                url,
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
            return response.json()
        except Exception as exc:  # noqa: BLE001
            return error_response(
                error=f"nano-banana proxy returned invalid JSON: {exc}",
                error_type="invalid_response",
                provider="nano-banana",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )


def register(ctx: Any) -> None:
    """Plugin entry point — register the nano-banana image gen provider."""
    ctx.register_image_gen_provider(NanoBananaImageGenProvider())
