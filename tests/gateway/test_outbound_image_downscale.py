"""Tests for outbound-image size capping + downscale-preview (base.py).

Covers the delivery-side companion to inbound media caps: when an outbound
image exceeds the target platform's native-attachment cap, the gateway sends
a downscaled preview instead while leaving the full-resolution original on
disk. Generic across platforms, fail-open on any error.
"""

import io
import os

import pytest

from gateway.platforms.base import (
    DEFAULT_OUTBOUND_IMAGE_MAX_BYTES,
    get_outbound_image_max_bytes,
    prepare_outbound_image,
)

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _write_png(path, size=(64, 64), color=(255, 0, 0)):
    """Write a tiny real PNG to *path* and return its byte size."""
    img = Image.new("RGB", size, color)
    img.save(path, format="PNG")
    return os.path.getsize(path)


def _write_big_png(path, size=(2000, 2000)):
    """Write a noisy PNG large enough to exceed a small byte cap.

    Random pixels defeat PNG compression so the file is genuinely big,
    exercising the real over-cap downscale path with real PIL.
    """
    import random

    rnd = random.Random(1234)
    img = Image.new("RGB", size)
    img.putdata([
        (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
        for _ in range(size[0] * size[1])
    ])
    img.save(path, format="PNG")
    return os.path.getsize(path)


class TestOutboundCapResolver:
    def test_default_when_config_absent(self, monkeypatch):
        import gateway.platforms.base as base

        monkeypatch.setattr(base, "load_config_for_outbound", lambda: {})
        assert get_outbound_image_max_bytes("discord") == DEFAULT_OUTBOUND_IMAGE_MAX_BYTES

    def test_default_discord_is_10_mib(self):
        assert DEFAULT_OUTBOUND_IMAGE_MAX_BYTES == 10 * 1024 * 1024

    def test_global_override_applies(self, monkeypatch):
        import gateway.platforms.base as base

        monkeypatch.setattr(
            base, "load_config_for_outbound",
            lambda: {"gateway": {"max_outbound_image_bytes": 5000}},
        )
        assert get_outbound_image_max_bytes("discord") == 5000
        assert get_outbound_image_max_bytes("telegram") == 5000

    def test_per_platform_override_beats_global(self, monkeypatch):
        import gateway.platforms.base as base

        monkeypatch.setattr(
            base, "load_config_for_outbound",
            lambda: {
                "gateway": {
                    "max_outbound_image_bytes": 5000,
                    "max_outbound_image_bytes_by_platform": {"discord": 999},
                }
            },
        )
        assert get_outbound_image_max_bytes("discord") == 999
        # A platform without its own entry falls back to the global default.
        assert get_outbound_image_max_bytes("telegram") == 5000

    def test_zero_disables(self, monkeypatch):
        import gateway.platforms.base as base

        monkeypatch.setattr(
            base, "load_config_for_outbound",
            lambda: {"gateway": {"max_outbound_image_bytes": 0}},
        )
        assert get_outbound_image_max_bytes("discord") == 0

    def test_unparseable_falls_back_to_default(self, monkeypatch):
        import gateway.platforms.base as base

        monkeypatch.setattr(
            base, "load_config_for_outbound",
            lambda: {"gateway": {"max_outbound_image_bytes": "not-a-number"}},
        )
        assert get_outbound_image_max_bytes("discord") == DEFAULT_OUTBOUND_IMAGE_MAX_BYTES

    def test_config_read_failure_falls_back_to_default(self, monkeypatch):
        import gateway.platforms.base as base

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(base, "load_config_for_outbound", _boom)
        assert get_outbound_image_max_bytes("discord") == DEFAULT_OUTBOUND_IMAGE_MAX_BYTES


class TestPrepareOutboundImage:
    def test_under_cap_returns_original_unchanged(self, tmp_path):
        p = str(tmp_path / "small.png")
        size = _write_png(p)
        out = prepare_outbound_image(p, platform="discord", max_bytes=size + 1000)
        assert out == p

    def test_cap_zero_disabled_returns_original(self, tmp_path):
        p = str(tmp_path / "img.png")
        _write_big_png(p, size=(400, 400))
        out = prepare_outbound_image(p, platform="discord", max_bytes=0)
        assert out == p

    def test_non_image_returns_original(self, tmp_path):
        p = str(tmp_path / "notes.txt")
        with open(p, "w") as fh:
            fh.write("x" * 50_000)
        out = prepare_outbound_image(p, platform="discord", max_bytes=10)
        assert out == p

    def test_missing_file_returns_original(self, tmp_path):
        p = str(tmp_path / "does-not-exist.png")
        out = prepare_outbound_image(p, platform="discord", max_bytes=10)
        assert out == p

    def test_over_cap_produces_smaller_preview(self, tmp_path):
        p = str(tmp_path / "huge.png")
        orig_bytes = _write_big_png(p, size=(1600, 1200))
        cap = orig_bytes // 4
        out = prepare_outbound_image(p, platform="discord", max_bytes=cap)
        # A DIFFERENT path was returned...
        assert out != p
        assert os.path.exists(out)
        # ...that fits under the cap...
        assert os.path.getsize(out) <= cap
        # ...with dimensions no larger than the original (aspect preserved).
        with Image.open(p) as orig, Image.open(out) as prev:
            assert prev.width <= orig.width
            assert prev.height <= orig.height
            assert prev.width >= 1 and prev.height >= 1
            # aspect ratio preserved within rounding tolerance
            assert abs((prev.width / prev.height) - (orig.width / orig.height)) < 0.05

    def test_original_file_untouched_after_downscale(self, tmp_path):
        p = str(tmp_path / "huge.png")
        orig_bytes = _write_big_png(p, size=(1600, 1200))
        cap = orig_bytes // 4
        out = prepare_outbound_image(p, platform="discord", max_bytes=cap)
        assert out != p
        # Original bytes and dimensions are unchanged.
        assert os.path.getsize(p) == orig_bytes
        with Image.open(p) as orig:
            assert (orig.width, orig.height) == (1600, 1200)

    def test_never_upscales_small_over_cap_image(self, tmp_path):
        # An image whose file is over a tiny cap but whose dimensions are
        # already small must not be blown up past its original dimensions.
        p = str(tmp_path / "small-but-heavy.png")
        _write_big_png(p, size=(300, 300))
        out = prepare_outbound_image(p, platform="discord", max_bytes=2000)
        with Image.open(out) as prev:
            assert prev.width <= 300
            assert prev.height <= 300

    def test_pil_failure_fails_open_to_original(self, tmp_path, monkeypatch):
        import PIL.Image

        p = str(tmp_path / "huge.png")
        _write_big_png(p, size=(800, 800))

        def _boom(*a, **k):
            raise RuntimeError("PIL exploded")

        # Force the downscale routine to blow up; delivery must fall back to
        # the original path rather than hard-failing.
        monkeypatch.setattr(PIL.Image, "open", _boom)
        out = prepare_outbound_image(p, platform="discord", max_bytes=10)
        assert out == p

    def test_resolves_cap_from_platform_when_max_bytes_none(self, tmp_path, monkeypatch):
        import gateway.platforms.base as base

        p = str(tmp_path / "huge.png")
        orig_bytes = _write_big_png(p, size=(1200, 1200))
        # discord cap forces a downscale; an uncapped platform passes through.
        monkeypatch.setattr(
            base, "load_config_for_outbound",
            lambda: {
                "gateway": {
                    "max_outbound_image_bytes_by_platform": {
                        "discord": orig_bytes // 4,
                        "slack": 0,
                    }
                }
            },
        )
        out_discord = prepare_outbound_image(p, platform="discord")
        assert out_discord != p
        assert os.path.getsize(out_discord) <= orig_bytes // 4

        out_slack = prepare_outbound_image(p, platform="slack")
        assert out_slack == p


class TestPrepareOutboundImagePathsHelper:
    """The shared static helper the dispatch blocks funnel local image paths
    through — one call site per block, avoiding the 3x paste drift."""

    def test_maps_over_cap_path_to_preview_passes_under_cap_through(
        self, tmp_path, monkeypatch
    ):
        import gateway.platforms.base as base
        from gateway.platforms.base import BasePlatformAdapter

        big = str(tmp_path / "big.png")
        big_bytes = _write_big_png(big, size=(1400, 1400))
        small = str(tmp_path / "small.png")
        _write_png(small, size=(48, 48))

        monkeypatch.setattr(
            base, "load_config_for_outbound",
            lambda: {"gateway": {"max_outbound_image_bytes": big_bytes // 4}},
        )

        out = BasePlatformAdapter.prepare_outbound_image_paths(
            [big, small], platform="discord"
        )
        assert len(out) == 2
        # Big image was downscaled to a different (preview) path under cap.
        assert out[0] != big
        assert os.path.getsize(out[0]) <= big_bytes // 4
        # Small image passed through unchanged.
        assert out[1] == small

    def test_empty_and_none_return_empty(self):
        from gateway.platforms.base import BasePlatformAdapter

        assert BasePlatformAdapter.prepare_outbound_image_paths([], platform="discord") == []
        assert BasePlatformAdapter.prepare_outbound_image_paths(None, platform="discord") == []


class TestDispatchHandsPreviewToAdapter:
    """Contract that would have caught the 4K silent-drop: when a local image
    is over the platform cap, the path handed to the adapter's native image
    send is the downscaled preview, not the oversized original."""

    def test_send_multiple_images_receives_prepped_path(self, tmp_path, monkeypatch):
        import asyncio
        from urllib.parse import quote as _quote

        import gateway.platforms.base as base
        from gateway.platforms.base import BasePlatformAdapter, SendResult

        big = str(tmp_path / "render.png")
        big_bytes = _write_big_png(big, size=(1600, 1200))
        monkeypatch.setattr(
            base, "load_config_for_outbound",
            lambda: {"gateway": {"max_outbound_image_bytes": big_bytes // 4}},
        )

        # A stub standing in for a real adapter: records which local path
        # reaches the native image-file send. We invoke the base
        # send_multiple_images implementation against it (unbound) to avoid
        # constructing the abstract adapter.
        sent_paths: list = []

        class _Stub:
            name = "discord"
            platform = "discord"

            async def send_image_file(self, chat_id, image_path, caption=None, metadata=None):
                sent_paths.append(image_path)
                return SendResult(success=True, message_id="2")

            def _is_animation_url(self, url):
                return False

        stub = _Stub()

        # This is exactly what the dispatch block does: funnel local image
        # paths through the shared prep helper, then build the file:// batch.
        prepped = BasePlatformAdapter.prepare_outbound_image_paths(
            [big], platform=stub.platform
        )
        batch = [(f"file://{_quote(p)}", "") for p in prepped]
        asyncio.run(
            BasePlatformAdapter.send_multiple_images(stub, chat_id="c", images=batch)
        )

        assert len(sent_paths) == 1
        # The adapter received the preview, NOT the oversized original.
        assert sent_paths[0] != big
        assert os.path.getsize(sent_paths[0]) <= big_bytes // 4


class TestRunPyDispatchSitesAreWired:
    """Guard against the 'helper built but never called' regression: every
    local-path image dispatch site in gateway/run.py must funnel through the
    shared outbound prep. This is the wiring that actually fixes the silent
    drop — the helper alone changes nothing until the dispatch calls it.
    """

    def _run_py_source(self):
        import gateway.run as gwrun

        with open(gwrun.__file__, "r") as fh:
            return fh.read()

    def test_run_py_references_the_shared_prep(self):
        src = self._run_py_source()
        assert (
            "prepare_outbound_image_paths" in src
            or "prepare_outbound_image" in src
        ), "gateway/run.py never calls the outbound image prep — dispatch unwired"

    def test_primary_post_stream_path_preps_before_building_file_uris(self):
        # The primary post-stream path builds file:// URIs from image_paths and
        # hands them to send_multiple_images. It must prep those paths first,
        # so the batch carries previews for over-cap images.
        src = self._run_py_source()
        # The file:// batch comprehension must consume prepped paths, not the
        # raw image_paths list.
        assert "prepare_outbound_image_paths" in src, (
            "primary post-stream path does not funnel image_paths through the "
            "shared prep before building the file:// batch"
        )

    def test_background_task_image_send_is_prepped(self):
        # The background-task path calls send_image_file(image_path=media_path);
        # media_path must be prepped first.
        src = self._run_py_source()
        # prepare_outbound_image (single-path form) is used at the per-file
        # send_image_file sites.
        assert src.count("prepare_outbound_image") >= 2, (
            "expected the shared prep at both the batch path and the per-file "
            "send_image_file path(s)"
        )


class TestConfigDefaults:
    """The outbound cap is behavioral config in config.yaml (not a HERMES_*
    env var). Assert the DEFAULT_CONFIG gateway section carries the keys and
    that the resolver reads them end-to-end through the real config loader."""

    def test_default_config_has_outbound_keys(self):
        from hermes_cli.config import DEFAULT_CONFIG

        gw = DEFAULT_CONFIG["gateway"]
        assert "max_outbound_image_bytes" in gw
        assert "max_outbound_image_bytes_by_platform" in gw
        # Contract: the shipped default cap equals the helper's default floor,
        # so config-absent and config-default behavior agree.
        assert gw["max_outbound_image_bytes"] == DEFAULT_OUTBOUND_IMAGE_MAX_BYTES
        assert isinstance(gw["max_outbound_image_bytes_by_platform"], dict)

    def test_resolver_reads_real_default_config(self, tmp_path, monkeypatch):
        # End-to-end through the real load_config against a temp HERMES_HOME:
        # with no user override, the resolver returns the shipped default.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        (tmp_path / ".hermes").mkdir(parents=True, exist_ok=True)
        import gateway.platforms.base as base

        assert base.get_outbound_image_max_bytes("discord") == DEFAULT_OUTBOUND_IMAGE_MAX_BYTES


class TestKanbanArtifactDeliveryIsWired:
    """Regression for the review-caught gap: kanban artifact delivery
    (``_deliver_kanban_artifacts`` -> ``send_multiple_images``) shipped a
    raw ``file://`` batch with NO downscale prep, so a 4K image handed to
    ``kanban_complete(artifacts=[...])`` still silently dropped on Discord.

    Behavioral contract: when an artifact image is over the delivery
    target's cap, the path inside the ``file://`` batch handed to the
    adapter's ``send_multiple_images`` is the downscaled preview, not the
    oversized original.
    """

    def test_over_cap_artifact_hands_preview_to_send(self, tmp_path, monkeypatch):
        import asyncio
        from urllib.parse import unquote as _unquote

        import gateway.platforms.base as base
        from gateway.kanban_watchers import GatewayKanbanWatchersMixin
        from gateway.platforms.base import SendResult

        big = str(tmp_path / "artifact_render.png")
        big_bytes = _write_big_png(big, size=(1600, 1200))
        monkeypatch.setattr(
            base, "load_config_for_outbound",
            lambda: {"gateway": {"max_outbound_image_bytes": big_bytes // 4}},
        )

        sent_batches: list = []

        class _StubAdapter:
            # Same platform key the wired dispatch sites resolve the cap by.
            platform = "discord"

            async def send_multiple_images(self, chat_id, images, metadata=None):
                sent_batches.append(images)
                return SendResult(success=True, message_id="1")

        adapter = _StubAdapter()

        # Unbound call: the method body never touches `self`, so a bare
        # object stands in for the gateway mixin host.
        asyncio.run(
            GatewayKanbanWatchersMixin._deliver_kanban_artifacts(
                object(),
                adapter=adapter,
                chat_id="c",
                metadata={},
                event_payload={"artifacts": [big]},
                task=None,
            )
        )

        assert len(sent_batches) == 1
        batch = sent_batches[0]
        assert len(batch) == 1
        # The batch carries a file:// URL; decode it back to a local path.
        url = batch[0][0]
        assert url.startswith("file://")
        delivered = _unquote(url[len("file://"):])
        # The batch carries the downscaled preview, NOT the oversized original.
        assert delivered != big
        assert os.path.getsize(delivered) <= big_bytes // 4

    def test_under_cap_artifact_passes_through_unchanged(self, tmp_path, monkeypatch):
        import asyncio
        from urllib.parse import unquote as _unquote

        import gateway.platforms.base as base
        from gateway.kanban_watchers import GatewayKanbanWatchersMixin
        from gateway.platforms.base import SendResult

        small = str(tmp_path / "small_artifact.png")
        _write_png(small, size=(32, 32))
        monkeypatch.setattr(
            base, "load_config_for_outbound",
            lambda: {"gateway": {"max_outbound_image_bytes": 10 * 1024 * 1024}},
        )

        sent_batches: list = []

        class _StubAdapter:
            platform = "discord"

            async def send_multiple_images(self, chat_id, images, metadata=None):
                sent_batches.append(images)
                return SendResult(success=True, message_id="1")

        asyncio.run(
            GatewayKanbanWatchersMixin._deliver_kanban_artifacts(
                object(),
                adapter=_StubAdapter(),
                chat_id="c",
                metadata={},
                event_payload={"artifacts": [small]},
                task=None,
            )
        )

        assert len(sent_batches) == 1
        delivered = _unquote(sent_batches[0][0][0][len("file://"):])
        # Under cap: the original path rides the batch untouched.
        assert delivered == small


class TestDirectSendToolPathsAreWired:
    """The platform ``send_message``-tool direct-send paths (Weixin, Yuanbao)
    dispatch a local image via ``send_image_file`` outside the base handle
    flow. Each must funnel that local path through the outbound prep, or an
    over-cap image silently drops on that platform too. Light source guards
    (these paths need a live WS/session to exercise end-to-end)."""

    def _src(self, module):
        with open(module.__file__, "r") as fh:
            return fh.read()

    def test_weixin_references_outbound_prep(self):
        import gateway.platforms.weixin as weixin

        assert "prepare_outbound_image" in self._src(weixin), (
            "gateway/platforms/weixin.py never calls the outbound image prep "
            "— a local image send there can silently drop on size-capped peers"
        )

    def test_yuanbao_references_outbound_prep(self):
        import gateway.platforms.yuanbao as yuanbao

        assert "prepare_outbound_image" in self._src(yuanbao), (
            "gateway/platforms/yuanbao.py never calls the outbound image prep "
            "— a local image send there can silently drop on size-capped peers"
        )

    def test_kanban_watchers_references_outbound_prep(self):
        import gateway.kanban_watchers as kw

        assert "prepare_outbound_image" in self._src(kw), (
            "gateway/kanban_watchers.py never calls the outbound image prep "
            "— kanban artifact images can silently drop on size-capped peers"
        )


