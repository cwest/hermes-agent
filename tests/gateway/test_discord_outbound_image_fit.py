"""An image over Discord's upload cap is sent as a downscaled preview instead of being dropped.

A 4K render can run past the channel's attachment cap. The upload preflight (#50846) used to
reject it (``send_image_file``) or skip it out of a batch with a notice, so the user never saw the
image. Now an oversized local IMAGE is first shrunk to a JPEG preview that fits the channel's own
cap; the original stays untouched on disk. Anything that can't be fitted (not an image, or too
big even at the smallest rung) keeps the old reject/skip notice, and documents are never altered.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.discord.adapter_media import DiscordMediaMixin

_LIMIT = 1024 * 1024


class _RecordedFile:
    def __init__(self, fp, filename=None, **_):
        self.fp, self.filename = fp, filename
        self.size = os.path.getsize(fp)


def _noise_png(path, size=(1200, 900)):
    # Random pixels defeat PNG compression, so the file is reliably over _LIMIT.
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(path, format="PNG")
    assert os.path.getsize(path) > _LIMIT
    return path


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(sys.modules["discord"], "File", _RecordedFile)
    monkeypatch.setattr(DiscordMediaMixin, "_discord_upload_limit_bytes", staticmethod(lambda _ch: _LIMIT))
    sent = SimpleNamespace(id=42, attachments=[SimpleNamespace(filename="x")])
    channel = SimpleNamespace(id=9, guild=None, send=AsyncMock(return_value=sent))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._is_forum_parent = lambda _ch: False  # type: ignore[method-assign]
    adapter._client = SimpleNamespace(get_channel=lambda _cid: channel, fetch_channel=AsyncMock())
    adapter.test_channel = channel
    return adapter


@pytest.mark.asyncio
async def test_send_image_file_sends_a_preview_that_fits(adapter, tmp_path):
    big = _noise_png(tmp_path / "render.png")
    before = big.read_bytes()

    result = await adapter.send_image_file("9", str(big))

    assert result.success is True
    files = adapter.test_channel.send.await_args.kwargs["files"]
    assert len(files) == 1 and files[0].size <= _LIMIT
    assert files[0].filename == "render.jpg"
    assert big.read_bytes() == before


@pytest.mark.asyncio
async def test_batch_keeps_an_oversized_image_as_a_preview(adapter, tmp_path):
    small = tmp_path / "small.png"
    Image.new("RGB", (8, 8)).save(small)
    big = _noise_png(tmp_path / "big.png")

    result = await adapter.send_multiple_images("9", [(f"file://{small}", ""), (f"file://{big}", "")])

    assert result.success is True
    kwargs = adapter.test_channel.send.await_args.kwargs
    assert [f.filename for f in kwargs["files"]] == ["small.png", "big.jpg"]
    assert all(f.size <= _LIMIT for f in kwargs["files"])
    assert not kwargs.get("content")


@pytest.mark.asyncio
async def test_image_that_cannot_fit_is_still_rejected_with_a_notice(adapter, tmp_path, monkeypatch):
    monkeypatch.setattr(DiscordMediaMixin, "_discord_upload_limit_bytes", staticmethod(lambda _ch: 200))
    big = _noise_png(tmp_path / "huge.png")

    result = await adapter.send_image_file("9", str(big))

    assert result.success is False and "too large" in result.error.lower()
    kwargs = adapter.test_channel.send.await_args.kwargs
    assert "files" not in kwargs and "huge.png" in kwargs["content"]


@pytest.mark.asyncio
async def test_oversized_document_is_never_altered(adapter, tmp_path):
    big = _noise_png(tmp_path / "scan.png")

    result = await adapter.send_document("9", str(big))

    assert result.success is False and "too large" in result.error.lower()
    assert "files" not in adapter.test_channel.send.await_args.kwargs


class TestFitImageUnder:
    def test_preview_fits_keeps_aspect_and_never_upscales(self, tmp_path):
        from gateway.platforms.base_image_sniff import fit_image_under

        big = _noise_png(tmp_path / "a.png", size=(1200, 600))
        preview = fit_image_under(str(big), _LIMIT)

        assert preview is not None and os.path.getsize(preview) <= _LIMIT
        with Image.open(preview) as img:
            assert img.format == "JPEG"
            assert img.width <= 1200 and abs(img.width / img.height - 2.0) < 0.02

    def test_returns_none_when_it_cannot_help(self, tmp_path):
        from gateway.platforms.base_image_sniff import fit_image_under

        text = tmp_path / "notes.png"
        text.write_bytes(b"not an image at all" * 1000)
        assert fit_image_under(str(text), 10) is None
        assert fit_image_under(str(tmp_path / "missing.png"), 10) is None
        assert fit_image_under(str(_noise_png(tmp_path / "b.png")), 200) is None
