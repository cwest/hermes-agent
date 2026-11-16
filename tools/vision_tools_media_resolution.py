"""``media_resolution``: the Gemini/Vertex image-resolution cost lever for the auxiliary vision call.

Gemini's generateContent exposes ``generationConfig.mediaResolution``, which sets how many tokens
an image is tiled into. Measured on a 2400x1600 PNG with gemini-3.x flash: default 1,080 IMAGE
tokens, ``low`` 260 (same answer to a coarse question), ``high`` 1,080. So the lever only pays off
in the low direction.

The wire shape is a FLAT ``extra_body={"media_resolution": "MEDIA_RESOLUTION_LOW"}``. That is the
key LiteLLM's Vertex adapter maps onto generationConfig, and it was verified live. Nested
``google``/``generationConfig`` shapes are silently ignored by LiteLLM and rejected (400) by
direct Vertex, so don't "fix" this into a nested shape.

The vision router can pick any provider, so the field is sent only on a Gemini-family route.
Every other provider, an unrecognised value, or a resolver failure sends nothing, so a strict
provider never sees an unknown field.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ENUM = {"low": "MEDIA_RESOLUTION_LOW", "medium": "MEDIA_RESOLUTION_MEDIUM", "high": "MEDIA_RESOLUTION_HIGH"}
_GEMINI_PROVIDERS = frozenset({"gemini", "google", "google-gemini", "google-vertex", "google-vertex-gemini"})


def normalize(value: Any) -> Optional[str]:
    """The Gemini enum for a ``low``/``medium``/``high`` (or ``MEDIA_RESOLUTION_*``) value; None
    for anything else, so a typo or "default" leaves the provider default alone."""
    if not isinstance(value, str):
        return None
    token = value.strip()
    return _ENUM.get(token.lower()) or (token.upper() if token.upper() in _ENUM.values() else None)


def is_gemini_route(provider: Any, model: Any) -> bool:
    """A direct Gemini/Vertex provider, or any route (aggregator, proxy) to a Gemini-family model."""
    return str(provider or "").strip().lower() in _GEMINI_PROVIDERS or "gemini" in str(model or "").lower()


def extra_body_for(override: Any, configured: Any, model: Optional[str]) -> dict:
    """``{"media_resolution": <enum>}`` for the auxiliary vision call, or ``{}``.

    The per-call ``override`` wins over ``auxiliary.vision.media_resolution`` (``configured``).
    The vision route is resolved only once a value is set, so the default path never pays for it.
    """
    raw = override if isinstance(override, str) and override.strip() else configured
    enum = normalize(raw)
    if enum is None:
        return {}
    try:
        from agent.auxiliary_client import resolve_vision_provider_client
        provider, _client, routed_model = resolve_vision_provider_client(model=model or None)
    except Exception:  # health: allow BLE001 -- the cost lever must never break a working vision call
        logger.debug("media_resolution: vision route unresolved; leaving the request unchanged", exc_info=True)
        return {}
    if not is_gemini_route(provider, routed_model):
        return {}
    logger.info("vision: media_resolution=%s (provider=%s model=%s)", enum, provider, routed_model)
    return {"media_resolution": enum}
