"""
Image Generation Provider ABC
=============================

Defines the pluggable-backend interface for image generation. Providers register
instances via ``PluginContext.register_image_gen_provider()``; the active one
(selected via ``image_gen.provider`` in ``config.yaml``) services every
``image_generate`` tool call.

Providers live in ``<repo>/plugins/image_gen/<name>/`` (built-in, auto-loaded
as ``kind: backend``) or ``~/.hermes/plugins/image_gen/<name>/`` (user, opt-in
via ``plugins.enabled``).

Unified surface
---------------
One tool — ``image_generate`` — covers **text-to-image** and
**image-to-image / image editing**. The router is the presence of
``image_url`` (and/or ``reference_image_urls``): if any source image is
provided, the provider routes to its image-to-image / edit endpoint; if
omitted, the provider routes to text-to-image. Users pick one **model**
(e.g. nano-banana-pro, gpt-image-2, grok-imagine-image); the provider
handles which underlying endpoint to hit. This mirrors the ``video_gen``
provider design (``agent/video_gen_provider.py``) so the two surfaces
stay learnable together.

Response shape
--------------
All providers return a dict that :func:`success_response` / :func:`error_response`
produce. The tool wrapper JSON-serializes it. Keys:

    success        bool
    image          str | None       URL or absolute file path
    model          str              provider-specific model identifier
    prompt         str              echoed prompt
    aspect_ratio   str              "landscape" | "square" | "portrait"
    modality       str              "text" | "image" (which mode was used)
    provider       str              provider name (for diagnostics)
    error          str              only when success=False
    error_type     str              only when success=False
"""

from __future__ import annotations

import abc
import base64
import datetime
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


VALID_ASPECT_RATIOS: Tuple[str, ...] = ("landscape", "square", "portrait")
DEFAULT_ASPECT_RATIO = "landscape"


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class ImageGenProvider(abc.ABC):
    """Abstract base class for an image generation backend.

    Subclasses must implement :meth:`generate`. Everything else has sane
    defaults — override only what your provider needs.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable short identifier used in ``image_gen.provider`` config.

        Lowercase, no spaces. Examples: ``fal``, ``openai``, ``replicate``.
        """

    @property
    def display_name(self) -> str:
        """Human-readable label shown in ``hermes tools``. Defaults to ``name.title()``."""
        return self.name.title()

    def is_available(self) -> bool:
        """Return True when this provider can service calls.

        Typically checks for a required API key. Default: True
        (providers with no external dependencies are always available).
        """
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        """Return catalog entries for ``hermes tools`` model picker.

        Each entry::

            {
                "id": "gpt-image-1.5",               # required
                "display": "GPT Image 1.5",          # optional; defaults to id
                "speed": "~10s",                     # optional
                "strengths": "...",                  # optional
                "price": "$...",                     # optional
            }

        Default: empty list (provider has no user-selectable models).
        """
        return []

    def get_setup_schema(self) -> Dict[str, Any]:
        """Return provider metadata for the ``hermes tools`` picker.

        Used by ``tools_config.py`` to inject this provider as a row in
        the Image Generation provider list. Shape::

            {
                "name": "OpenAI",                     # picker label
                "badge": "paid",                      # optional short tag
                "tag": "One-line description...",     # optional subtitle
                "env_vars": [                         # keys to prompt for
                    {"key": "OPENAI_API_KEY",
                     "prompt": "OpenAI API key",
                     "url": "https://platform.openai.com/api-keys"},
                ],
            }

        Default: minimal entry derived from ``display_name``. Override to
        expose API key prompts and custom badges.
        """
        return {
            "name": self.display_name,
            "badge": "",
            "tag": "",
            "env_vars": [],
        }

    def default_model(self) -> Optional[str]:
        """Return the default model id, or None if not applicable."""
        models = self.list_models()
        if models:
            return models[0].get("id")
        return None

    def capabilities(self) -> Dict[str, Any]:
        """Return what this provider supports.

        Returned dict (all keys optional)::

            {
                "modalities": ["text", "image"],   # which inputs the backend accepts
                "max_reference_images": 9,          # cap for reference_image_urls
            }

        ``modalities`` declares whether the active backend/model supports
        text-to-image (``"text"``), image-to-image / editing (``"image"``),
        or both. The tool layer surfaces this in the dynamic schema so the
        model knows when ``image_url`` is honored. Used by ``hermes tools``
        for the picker too. Default: text-only (backward compatible — a
        provider that doesn't override this advertises text-to-image only).
        """
        return {
            "modalities": ["text"],
            "max_reference_images": 0,
        }

    @abc.abstractmethod
    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Generate an image from a text prompt, or edit/transform a source image.

        Routing: if ``image_url`` (or any ``reference_image_urls``) is
        provided, the provider should route to its image-to-image / edit
        endpoint; otherwise text-to-image. ``image_url`` is the primary
        source image to edit; ``reference_image_urls`` are additional
        style/composition references (provider clamps to its declared
        ``max_reference_images``).

        Implementations should return the dict from :func:`success_response`
        or :func:`error_response`. ``kwargs`` may contain forward-compat
        parameters future versions of the schema will expose —
        implementations MUST ignore unknown keys (no TypeError).
        """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def resolve_aspect_ratio(value: Optional[str]) -> str:
    """Clamp an aspect_ratio value to the valid set, defaulting to landscape.

    Invalid values are coerced rather than rejected so the tool surface is
    forgiving of agent mistakes.
    """
    if not isinstance(value, str):
        return DEFAULT_ASPECT_RATIO
    v = value.strip().lower()
    if v in VALID_ASPECT_RATIOS:
        return v
    return DEFAULT_ASPECT_RATIO


def normalize_reference_images(value: Any) -> Optional[List[str]]:
    """Coerce a reference-image argument into a clean list of URL/path strings.

    Accepts a single string or a list; strips blanks and whitespace. Returns
    ``None`` when nothing usable remains so providers can treat "no refs" as a
    single sentinel.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    out: List[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out or None


def _images_cache_dir() -> Path:
    """Return ``$HERMES_HOME/cache/images/``, creating parents as needed."""
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "cache" / "images"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Shared cache GC janitor
# ---------------------------------------------------------------------------
#
# ``$HERMES_HOME/cache/images/`` is shared by every image_gen backend, so the
# retention policy is framework-level (one dir, one janitor) rather than
# per-backend. It runs opportunistically at save time — no cron needed — and is
# safe by construction: durable copies of anything that matters already live in
# the consuming workflow's destination (a blog/writing repo, etc.); the cache is
# disposable machine-local state.

_DEFAULT_CACHE_MAX_AGE_DAYS = 30
_DEFAULT_CACHE_MAX_TOTAL_MB = 2048  # ~2 GB


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


def _load_cache_config() -> Dict[str, int]:
    """Resolve the cache-GC caps, applying defaults for anything unset.

    Reads ``image_gen.cache.max_age_days`` / ``image_gen.cache.max_total_mb``
    from config.yaml. Config, not env vars, per the contribution rubric.
    """
    max_age = _DEFAULT_CACHE_MAX_AGE_DAYS
    max_total = _DEFAULT_CACHE_MAX_TOTAL_MB
    cache = _load_image_gen_config().get("cache")
    if isinstance(cache, dict):
        raw_age = cache.get("max_age_days")
        if isinstance(raw_age, (int, float)) and raw_age > 0:
            max_age = int(raw_age)
        raw_total = cache.get("max_total_mb")
        if isinstance(raw_total, (int, float)) and raw_total > 0:
            max_total = int(raw_total)
    return {"max_age_days": max_age, "max_total_mb": max_total}


def _prune_image_cache(keep: Optional[Path] = None) -> None:
    """Prune the shared image cache to stay under the configured caps.

    Deletes files (oldest-first, by mtime) when EITHER a file's age exceeds
    ``max_age_days`` OR the cache's total size exceeds ``max_total_mb``. The
    file at ``keep`` (typically the one just written) is never deleted. Emits a
    single INFO line when it prunes, and stays silent otherwise.

    Best-effort: any error is swallowed (logged at debug) so a GC hiccup can
    never fail an image save.
    """
    try:
        caps = _load_cache_config()
        max_age_days = caps["max_age_days"]
        max_total_bytes = caps["max_total_mb"] * 1024 * 1024

        cache_dir = _images_cache_dir()
        keep_resolved = keep.resolve() if isinstance(keep, Path) else None

        entries: List[Tuple[float, int, Path]] = []
        for entry in cache_dir.iterdir():
            if not entry.is_file():
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, entry))

        if not entries:
            return

        # Oldest first — the deletion order for both age and size passes.
        entries.sort(key=lambda t: t[0])

        now = datetime.datetime.now().timestamp()
        age_cutoff = now - max_age_days * 86400
        total_size = sum(size for _, size, _ in entries)

        pruned_count = 0
        freed_bytes = 0
        survivors: List[Tuple[float, int, Path]] = []

        # Pass 1 — age: drop anything older than the cutoff.
        for mtime, size, path in entries:
            if keep_resolved is not None and path.resolve() == keep_resolved:
                survivors.append((mtime, size, path))
                continue
            if mtime < age_cutoff:
                try:
                    path.unlink()
                    pruned_count += 1
                    freed_bytes += size
                    total_size -= size
                except OSError:
                    survivors.append((mtime, size, path))
            else:
                survivors.append((mtime, size, path))

        # Pass 2 — size: keep dropping oldest survivors until under the cap.
        for mtime, size, path in survivors:
            if total_size <= max_total_bytes:
                break
            if keep_resolved is not None and path.resolve() == keep_resolved:
                continue
            try:
                path.unlink()
                pruned_count += 1
                freed_bytes += size
                total_size -= size
            except OSError:
                continue

        if pruned_count:
            logger.info(
                "image cache GC: pruned %d files, freed %.1f MB",
                pruned_count,
                freed_bytes / (1024 * 1024),
            )
    except Exception as exc:  # noqa: BLE001 - GC must never fail a save
        logger.debug("image cache GC skipped: %s", exc)


def _sniff_image_extension(raw: bytes) -> Optional[str]:
    """Infer an image file extension from a payload's magic bytes.

    Returns ``"png"`` / ``"jpg"`` / ``"webp"`` / ``"gif"`` for a recognised
    signature, or ``None`` when the bytes don't match a known image format so
    the caller can apply its own fallback. Shared by :func:`save_b64_image` and
    :func:`save_url_image` so both helpers agree on the same detection.
    """
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:2] == b"\xff\xd8":
        return "jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def save_b64_image(
    b64_data: str,
    *,
    prefix: str = "image",
    extension: Optional[str] = None,
) -> Path:
    """Decode base64 image data and write it under ``$HERMES_HOME/cache/images/``.

    Returns the absolute :class:`Path` to the saved file.

    Filename format: ``<prefix>_<YYYYMMDD_HHMMSS>_<short-uuid>.<ext>``.

    The extension is inferred from the decoded bytes' magic number (PNG, JPEG,
    WEBP, GIF; defaulting to ``png`` when unrecognised) so a backend returning,
    e.g., JPEG (Nano Banana Lite) is saved with a truthful ``.jpg`` rather than
    a mislabelled ``.png``. Pass an explicit ``extension`` to override the sniff
    verbatim — back-compat for callers that already know their format.
    """
    raw = base64.b64decode(b64_data)
    if extension is None:
        extension = _sniff_image_extension(raw) or "png"
    else:
        extension = extension.lstrip(".") or "png"
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:8]
    path = _images_cache_dir() / f"{prefix}_{ts}_{short}.{extension}"
    path.write_bytes(raw)
    try:
        _prune_image_cache(keep=path)
    except Exception as exc:  # noqa: BLE001 - GC must never fail a save
        logger.debug("image cache GC skipped: %s", exc)
    return path


# Extension inference for save_url_image — keep small and explicit.  We don't
# want to import mimetypes for a handful of formats every image_gen provider
# actually returns, and we never want to inherit a content-type that points
# at HTML or JSON when the API gives us a degenerate response.
_URL_IMAGE_CONTENT_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def save_url_image(
    url: str,
    *,
    prefix: str = "image",
    timeout: float = 60.0,
    max_bytes: int = 25 * 1024 * 1024,
) -> Path:
    """Download an image URL and write it under ``$HERMES_HOME/cache/images/``.

    Used by providers (xAI, fallback OpenAI) whose API returns an *ephemeral*
    URL instead of inline base64 — those URLs frequently expire before a
    downstream consumer (Telegram ``send_photo``, browser fetch) can resolve
    them, so we materialise the bytes locally at tool-completion time.
    Mirrors :func:`save_b64_image`'s shape so providers can swap in one line.

    Returns the absolute :class:`Path` to the saved file.  Raises on any
    network / HTTP / oversize / non-image-content-type error so callers can
    fall back to returning the bare URL with a clear error message.
    """
    import requests

    response = requests.get(url, timeout=timeout, stream=True)
    response.raise_for_status()

    # Infer extension from the response content-type, falling back to the
    # URL suffix when xAI / OpenAI omit a precise type (some CDNs return
    # ``application/octet-stream``).  Magic-byte sniffing of the first bytes is
    # the final fallback (shared with :func:`save_b64_image`) before ``png``.
    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    extension = _URL_IMAGE_CONTENT_TYPES.get(content_type)
    if extension is None:
        url_path = url.split("?", 1)[0].lower()
        for ext in ("png", "jpg", "jpeg", "webp", "gif"):
            if url_path.endswith(f".{ext}"):
                extension = "jpg" if ext == "jpeg" else ext
                break

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:8]
    # The format may still be unknown here; write to a provisional path, capture
    # the leading bytes, then resolve the extension and rename so the final name
    # always carries a truthful extension.
    provisional_ext = extension or "tmp"
    path = _images_cache_dir() / f"{prefix}_{ts}_{short}.{provisional_ext}"

    bytes_written = 0
    header_bytes = b""
    with path.open("wb") as fh:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            if len(header_bytes) < 12:
                header_bytes += chunk[: 12 - len(header_bytes)]
            bytes_written += len(chunk)
            if bytes_written > max_bytes:
                fh.close()
                try:
                    path.unlink()
                except OSError:
                    pass
                raise ValueError(
                    f"Image at {url} exceeds {max_bytes // (1024 * 1024)}MB cap; refusing to cache."
                )
            fh.write(chunk)

    if bytes_written == 0:
        try:
            path.unlink()
        except OSError:
            pass
        raise ValueError(f"Image at {url} returned 0 bytes; refusing to cache.")

    # Content-type and URL suffix both failed to name the format: sniff the
    # magic bytes, else default to png.  Rename the already-written file to
    # carry the resolved extension.
    if extension is None:
        extension = _sniff_image_extension(header_bytes) or "png"
        final_path = _images_cache_dir() / f"{prefix}_{ts}_{short}.{extension}"
        try:
            path.rename(final_path)
            path = final_path
        except OSError:
            pass

    try:
        _prune_image_cache(keep=path)
    except Exception as exc:  # noqa: BLE001 - GC must never fail a save
        logger.debug("image cache GC skipped: %s", exc)
    return path


def success_response(
    *,
    image: str,
    model: str,
    prompt: str,
    aspect_ratio: str,
    provider: str,
    modality: str = "text",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a uniform success response dict.

    ``image`` may be an HTTP URL or an absolute filesystem path (for b64
    providers like OpenAI). ``modality`` is ``"text"`` (text-to-image) or
    ``"image"`` (image-to-image / editing) — indicates which endpoint was
    actually hit, useful for diagnostics. Callers that need to pass through
    additional backend-specific fields can supply ``extra``.
    """
    payload: Dict[str, Any] = {
        "success": True,
        "image": image,
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "modality": modality,
        "provider": provider,
    }
    if extra:
        for k, v in extra.items():
            payload.setdefault(k, v)
    return payload


def error_response(
    *,
    error: str,
    error_type: str = "provider_error",
    provider: str = "",
    model: str = "",
    prompt: str = "",
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
) -> Dict[str, Any]:
    """Build a uniform error response dict."""
    return {
        "success": False,
        "image": None,
        "error": error,
        "error_type": error_type,
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "provider": provider,
    }
