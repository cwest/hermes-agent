"""Magic-byte sniffing for the image cache, and fitting an outbound image under an upload cap."""

import logging
import os
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# ISO-BMFF stills: iOS sends uncompressed photos as HEIC documents. Brand detection
# is agent.image_routing's, which transcodes these to PNG before vision sees them.
_ISOBMFF_IMAGE_MIMES = frozenset({"image/heic", "image/avif"})

# Long edge / JPEG quality rungs tried in order until the preview fits the cap.
_FIT_LADDER = ((2048, 88), (1568, 82), (1024, 76), (768, 72), (512, 68))


def _looks_like_image(data: bytes) -> bool:
    """Return True if *data* starts with a known image magic-byte sequence."""
    if len(data) >= 4 and (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff"
                           or data[:6] in {b"GIF87a", b"GIF89a"} or data[:2] == b"BM"
                           or (data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP")):
        return True
    # The ftyp box's own size field must cover at least size+type+brand+version (16 bytes) and fit in
    # the payload: a 12-byte stub or text that happens to hold ``ftypheic`` at offset 4 is not an image.
    if data[4:8] != b"ftyp" or not len(data) >= int.from_bytes(data[:4], "big") >= 16:
        return False
    from agent.image_routing import _sniff_mime_from_bytes
    return _sniff_mime_from_bytes(data[:16]) in _ISOBMFF_IMAGE_MIMES


def fit_image_under(path: str, max_bytes: int) -> Optional[str]:
    """Path of a JPEG preview of the image at *path* that is at most *max_bytes*, or None.

    For an outbound attachment over a platform's upload cap: a size-capped platform rejects or
    silently drops it, so a smaller preview is the only way the user sees the image at all. The
    preview goes in the image cache; the source is never touched or upscaled, and the aspect ratio
    is kept. None when *path* is not a readable image or no rung of the ladder fits, so the caller
    keeps its own oversized-file handling."""
    try:
        with open(path, "rb") as fh:
            if not _looks_like_image(fh.read(32)):
                return None
        from PIL import Image

        from gateway.platforms.base import get_image_cache_dir
        with Image.open(path) as source:
            img = source.convert("RGB")
        preview = str(get_image_cache_dir() / f"preview_{uuid.uuid4().hex[:12]}.jpg")
        long_edge = max(img.size)
        for target, quality in _FIT_LADDER:
            scale = min(target, long_edge) / long_edge
            size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
            img.resize(size, Image.Resampling.LANCZOS).save(preview, format="JPEG", quality=quality, optimize=True)
            if os.path.getsize(preview) <= max_bytes:
                return preview
        os.unlink(preview)
    except Exception:  # health: allow BLE001 -- fail open: any failure keeps the caller's own handling
        logger.debug("fit_image_under: no preview for %s", os.path.basename(path), exc_info=True)
    return None
