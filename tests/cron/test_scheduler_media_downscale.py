"""Cron media delivery must honor the outbound image size cap.

Regression test for the cron `no_agent` / MEDIA: delivery path.

THE BUG: every other outbound image send site in the codebase routes its path
through ``gateway.platforms.base.prepare_outbound_image`` before handing it to
an adapter — ``_process_message_background`` (via
``prepare_outbound_image_paths``), the gateway reply path in ``gateway/run.py``,
the kanban watchers, weixin, and yuanbao. ``cron/scheduler.py``'s
``_send_media_via_adapter`` did NOT. It passed the raw path straight to
``adapter.send_image_file``.

Size-capped platforms SILENTLY DROP an oversized attachment: the message sends,
Discord reports "Couldn't deliver the image attachment," and the file never
arrives. So a cron job that emitted a ``MEDIA:`` tag for a 4K render (Nano
Banana Pro at 5504x3072 is routinely 15-20 MB, well past Discord's ~10 MB
non-Nitro cap) reported work it could not actually show — every frame lost,
with the text half of the message arriving normally so the job looked healthy.

The downscale machinery already existed and was already generic; this path was
simply not wired into it. The fix is to call it here too, not to build a second
downscaler.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture()
def scheduler_mod():
    return importlib.import_module("cron.scheduler")


class _RecordingAdapter:
    """Captures the exact path handed to each send_* method."""

    platform = "discord"

    def __init__(self):
        self.image_paths: list[str] = []
        self.doc_paths: list[str] = []

    async def send_image_file(self, chat_id, image_path, metadata=None):
        self.image_paths.append(str(image_path))
        return True

    async def send_document(self, chat_id, file_path, metadata=None):
        self.doc_paths.append(str(file_path))
        return True

    async def send_voice(self, chat_id, audio_path, metadata=None):
        return True

    async def send_video(self, chat_id, video_path, metadata=None):
        return True


def _make_png(path: Path, size_bytes: int) -> Path:
    """Write a file with a real PNG magic header padded to *size_bytes*."""
    header = b"\x89PNG\r\n\x1a\n"
    path.write_bytes(header + b"\x00" * max(0, size_bytes - len(header)))
    return path


def _run_send(scheduler_mod, adapter, media_files, monkeypatch):
    """Drive _send_media_via_adapter with a loop stub that runs coros inline."""
    import asyncio

    scheduled: list = []

    def _fake_schedule(coro, loop):
        # Execute the coroutine immediately so the adapter records the call,
        # and hand back a future-like object the caller can .result() on.
        asyncio.run(coro)
        fut = types.SimpleNamespace(result=lambda timeout=None: True)
        scheduled.append(fut)
        return fut

    fake_async_utils = types.ModuleType("agent.async_utils")
    fake_async_utils.safe_schedule_threadsafe = _fake_schedule
    monkeypatch.setitem(sys.modules, "agent.async_utils", fake_async_utils)

    scheduler_mod._send_media_via_adapter(
        adapter,
        "chan-1",
        media_files,
        None,
        object(),  # loop sentinel; _fake_schedule ignores it
        {"id": "job-1"},
        platform="discord",
    )
    return scheduled


def test_oversized_cron_image_is_downscaled_before_send(
    scheduler_mod, tmp_path, monkeypatch
):
    """An over-cap image must NOT be handed to the adapter at its raw path.

    This is the actual reported failure: a 15 MB render emitted by a cron job's
    MEDIA: tag arrived at Discord unmodified and was silently dropped.
    """
    big = _make_png(tmp_path / "hero_4k.png", 15 * 1024 * 1024)
    preview = tmp_path / "preview.jpg"
    preview.write_bytes(b"\xff\xd8\xff" + b"\x00" * 1024)

    called: dict = {}

    def _fake_prepare(path, platform=None, max_bytes=None):
        called["path"] = str(path)
        called["platform"] = platform
        return str(preview)

    monkeypatch.setattr(
        "gateway.platforms.base.prepare_outbound_image", _fake_prepare
    )

    adapter = _RecordingAdapter()
    _run_send(scheduler_mod, adapter, [(str(big), False)], monkeypatch)

    assert called.get("path") == str(big), (
        "cron media delivery never called prepare_outbound_image — the "
        "oversized image goes to the platform raw and is silently dropped"
    )
    assert adapter.image_paths == [str(preview)], (
        f"adapter received {adapter.image_paths!r}; expected the downscaled "
        f"preview {str(preview)!r}"
    )
    assert str(big) not in adapter.image_paths


def test_under_cap_image_is_passed_through_unchanged(
    scheduler_mod, tmp_path, monkeypatch
):
    """A small image must reach the adapter untouched (no needless re-encode)."""
    small = _make_png(tmp_path / "small.png", 4096)

    monkeypatch.setattr(
        "gateway.platforms.base.prepare_outbound_image",
        lambda path, platform=None, max_bytes=None: str(path),
    )

    adapter = _RecordingAdapter()
    _run_send(scheduler_mod, adapter, [(str(small), False)], monkeypatch)

    assert adapter.image_paths == [str(small)]


def test_non_image_media_is_not_routed_through_the_downscaler(
    scheduler_mod, tmp_path, monkeypatch
):
    """A document must not be handed to the image downscaler."""
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4\n" + b"\x00" * 2048)

    calls: list = []

    monkeypatch.setattr(
        "gateway.platforms.base.prepare_outbound_image",
        lambda path, platform=None, max_bytes=None: calls.append(path) or str(path),
    )

    adapter = _RecordingAdapter()
    _run_send(scheduler_mod, adapter, [(str(doc), False)], monkeypatch)

    assert adapter.doc_paths == [str(doc)]
    assert calls == [], "non-image media must not go through the image downscaler"


def test_downscale_failure_falls_open_to_the_original_path(
    scheduler_mod, tmp_path, monkeypatch
):
    """A downscaler exception must never block a delivery that could succeed.

    Fail-open matches prepare_outbound_image's own contract: a downscale bug
    should degrade to the previous behavior, not drop the attachment entirely.
    """
    big = _make_png(tmp_path / "hero_4k.png", 15 * 1024 * 1024)

    def _boom(path, platform=None, max_bytes=None):
        raise RuntimeError("PIL exploded")

    monkeypatch.setattr("gateway.platforms.base.prepare_outbound_image", _boom)

    adapter = _RecordingAdapter()
    _run_send(scheduler_mod, adapter, [(str(big), False)], monkeypatch)

    assert adapter.image_paths == [str(big)], (
        "a downscaler failure must fall open to the original path, not drop "
        "the attachment"
    )
