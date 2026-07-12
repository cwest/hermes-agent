#!/usr/bin/env python3
"""Tests for ``save_b64_image`` extension sniffing.

``save_b64_image`` is the shared framework helper every image_gen backend uses
to persist inline base64 payloads. It historically hardcoded a ``.png``
extension regardless of the actual bytes, which mis-labels backends that return
other formats — notably Nano Banana Lite (``gemini-3.1-flash-lite-image``),
which returns JPEG. A wrong extension can break downstream consumers that key on
it (Discord attachment sniffing, file tooling).

These tests pin the contract: when no explicit ``extension`` is passed, the
helper sniffs the format from the decoded magic bytes and picks the extension;
an explicit ``extension`` argument always wins (back-compat).

Fixtures are minimal valid magic-byte headers plus a few trailing bytes — they
need not be decodable images, only carry the signature the sniffer keys on.
"""

from __future__ import annotations

import base64

import pytest

# Import once at collection time. Some sibling image_gen tests pop ``agent.*``
# out of ``sys.modules`` mid-run; a top-level import here binds the real
# functions before that can happen, so random test ordering can't shadow them.
from agent.image_gen_provider import _sniff_image_extension, save_b64_image


# --- minimal magic-byte fixtures (header + filler; not decodable images) ----
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 16
_WEBP_MAGIC = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"VP8 " + b"\x00" * 8
_GIF87_MAGIC = b"GIF87a" + b"\x00" * 16
_GIF89_MAGIC = b"GIF89a" + b"\x00" * 16
_UNKNOWN = b"\x00\x01\x02\x03not-an-image-header" + b"\x00" * 8


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the framework image cache at a temp dir."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    d = home / "cache" / "images"
    d.mkdir(parents=True)
    return d


class TestSaveB64ImageSniff:
    def test_png_bytes_saved_as_png(self, cache_dir):
        path = save_b64_image(_b64(_PNG_MAGIC), prefix="test")
        assert path.suffix == ".png"

    def test_jpeg_bytes_saved_as_jpg(self, cache_dir):
        """The Lite regression-catcher: JPEG must not be saved as .png."""
        path = save_b64_image(_b64(_JPEG_MAGIC), prefix="test")
        assert path.suffix == ".jpg"

    def test_webp_bytes_saved_as_webp(self, cache_dir):
        path = save_b64_image(_b64(_WEBP_MAGIC), prefix="test")
        assert path.suffix == ".webp"

    def test_gif87_bytes_saved_as_gif(self, cache_dir):
        path = save_b64_image(_b64(_GIF87_MAGIC), prefix="test")
        assert path.suffix == ".gif"

    def test_gif89_bytes_saved_as_gif(self, cache_dir):
        path = save_b64_image(_b64(_GIF89_MAGIC), prefix="test")
        assert path.suffix == ".gif"

    def test_unknown_bytes_fall_back_to_png(self, cache_dir):
        path = save_b64_image(_b64(_UNKNOWN), prefix="test")
        assert path.suffix == ".png"

    def test_explicit_extension_wins_over_sniff(self, cache_dir):
        """Back-compat contract: an explicit ``extension`` is honored verbatim,
        even when it disagrees with the sniffed format (JPEG bytes forced to
        .png)."""
        path = save_b64_image(_b64(_JPEG_MAGIC), prefix="test", extension="png")
        assert path.suffix == ".png"

    def test_explicit_extension_non_default_honored(self, cache_dir):
        """An explicit extension unrelated to the bytes is used verbatim."""
        path = save_b64_image(_b64(_PNG_MAGIC), prefix="test", extension="bin")
        assert path.suffix == ".bin"


class TestSniffHelper:
    """The extracted DRY helper both save_b64_image and save_url_image share."""

    def test_helper_sniffs_each_known_format(self):
        assert _sniff_image_extension(_PNG_MAGIC) == "png"
        assert _sniff_image_extension(_JPEG_MAGIC) == "jpg"
        assert _sniff_image_extension(_WEBP_MAGIC) == "webp"
        assert _sniff_image_extension(_GIF87_MAGIC) == "gif"
        assert _sniff_image_extension(_GIF89_MAGIC) == "gif"

    def test_helper_returns_none_on_unknown(self):
        assert _sniff_image_extension(_UNKNOWN) is None


class TestSaveB64ImageExplicitExtensionNormalization:
    def test_explicit_extension_normalizes_leading_dot(self, cache_dir):
        """A caller passing ``.png`` must not produce a double-dot filename."""
        path = save_b64_image(_b64(_JPEG_MAGIC), prefix="test", extension=".png")
        assert path.suffix == ".png"
        assert ".." not in path.name


class TestSaveUrlImageSniffFallback:
    """save_url_image shares the sniffer as its final fallback when content-type
    and URL suffix are both uninformative (e.g. application/octet-stream CDNs)."""

    def _resp(self, chunks):
        from unittest import mock

        resp = mock.MagicMock()
        resp.headers = {"Content-Type": "application/octet-stream"}
        resp.raise_for_status = mock.MagicMock()
        resp.iter_content = lambda chunk_size=0: chunks
        return resp

    def test_octet_stream_url_falls_back_to_magic_bytes(self, cache_dir):
        from unittest import mock

        from agent.image_gen_provider import save_url_image

        with mock.patch("requests.get", return_value=self._resp([_JPEG_MAGIC])):
            path = save_url_image("https://cdn.example/opaque", prefix="test")
        assert path.suffix == ".jpg"

    def test_unknown_bytes_and_type_fall_back_to_png(self, cache_dir):
        from unittest import mock

        from agent.image_gen_provider import save_url_image

        with mock.patch("requests.get", return_value=self._resp([_UNKNOWN])):
            path = save_url_image("https://cdn.example/opaque", prefix="test")
        assert path.suffix == ".png"
