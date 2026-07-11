#!/usr/bin/env python3
"""Tests for the framework-level image cache GC janitor.

The janitor governs the whole shared ``$HERMES_HOME/cache/images/`` dir (used by
every image_gen backend), invoked opportunistically from ``save_b64_image`` /
``save_url_image``. Policy: prune when total size exceeds ``max_total_mb`` OR a
file's age exceeds ``max_age_days``, deleting oldest-first, never the file just
written, emitting one INFO line, best-effort (never fails a save).
"""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest


def _touch(path: Path, *, size: int = 1, mtime: float | None = None) -> Path:
    path.write_bytes(b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the framework cache at a temp dir."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    d = home / "cache" / "images"
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# _prune_image_cache
# ---------------------------------------------------------------------------


class TestPruneBySize:
    def test_prunes_oldest_first_until_under_size_cap(self, cache_dir):
        from agent.image_gen_provider import _prune_image_cache

        now = time.time()
        # 5 files x 1 MB each = 5 MB; cap 3 MB → must drop the 2 oldest.
        files = []
        for i in range(5):
            f = _touch(cache_dir / f"img_{i}.png", size=1024 * 1024, mtime=now - (5 - i) * 100)
            files.append(f)

        with patch(
            "agent.image_gen_provider._load_cache_config",
            return_value={"max_total_mb": 3, "max_age_days": 3650},
        ):
            _prune_image_cache()

        remaining = sorted(p.name for p in cache_dir.iterdir())
        # Oldest two (img_0, img_1) deleted; newest three kept.
        assert remaining == ["img_2.png", "img_3.png", "img_4.png"]

    def test_no_prune_when_under_size_cap(self, cache_dir):
        from agent.image_gen_provider import _prune_image_cache

        _touch(cache_dir / "a.png", size=1024)
        with patch(
            "agent.image_gen_provider._load_cache_config",
            return_value={"max_total_mb": 2048, "max_age_days": 3650},
        ):
            _prune_image_cache()
        assert (cache_dir / "a.png").exists()


class TestPruneByAge:
    def test_prunes_files_older_than_max_age(self, cache_dir):
        from agent.image_gen_provider import _prune_image_cache

        now = time.time()
        old = _touch(cache_dir / "old.png", size=10, mtime=now - 40 * 86400)
        fresh = _touch(cache_dir / "fresh.png", size=10, mtime=now - 1 * 86400)

        with patch(
            "agent.image_gen_provider._load_cache_config",
            return_value={"max_total_mb": 100000, "max_age_days": 30},
        ):
            _prune_image_cache()

        assert not old.exists()
        assert fresh.exists()


class TestKeepGuard:
    def test_never_deletes_the_just_written_file(self, cache_dir):
        from agent.image_gen_provider import _prune_image_cache

        now = time.time()
        # The kept file is the OLDEST and cache is over cap: without the guard
        # it would be first to go.
        keep = _touch(cache_dir / "keep.png", size=1024 * 1024, mtime=now - 10000)
        _touch(cache_dir / "other.png", size=1024 * 1024, mtime=now)

        with patch(
            "agent.image_gen_provider._load_cache_config",
            return_value={"max_total_mb": 1, "max_age_days": 3650},
        ):
            _prune_image_cache(keep=keep)

        assert keep.exists()


class TestLogging:
    def test_emits_single_info_line_on_prune(self, cache_dir, caplog):
        from agent.image_gen_provider import _prune_image_cache

        now = time.time()
        for i in range(3):
            _touch(cache_dir / f"f_{i}.png", size=1024 * 1024, mtime=now - (3 - i) * 100)

        with patch(
            "agent.image_gen_provider._load_cache_config",
            return_value={"max_total_mb": 1, "max_age_days": 3650},
        ), caplog.at_level(logging.INFO, logger="agent.image_gen_provider"):
            _prune_image_cache()

        gc_lines = [r for r in caplog.records if "image cache GC" in r.getMessage()]
        assert len(gc_lines) == 1
        assert "pruned" in gc_lines[0].getMessage()

    def test_silent_when_nothing_to_prune(self, cache_dir, caplog):
        from agent.image_gen_provider import _prune_image_cache

        _touch(cache_dir / "a.png", size=10)
        with patch(
            "agent.image_gen_provider._load_cache_config",
            return_value={"max_total_mb": 100000, "max_age_days": 3650},
        ), caplog.at_level(logging.INFO, logger="agent.image_gen_provider"):
            _prune_image_cache()
        assert not [r for r in caplog.records if "image cache GC" in r.getMessage()]


class TestBestEffort:
    def test_gc_exception_is_swallowed(self, cache_dir):
        from agent.image_gen_provider import _prune_image_cache

        with patch(
            "agent.image_gen_provider._load_cache_config",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise.
            _prune_image_cache()


class TestConfigDefaults:
    def test_load_cache_config_defaults(self, cache_dir):
        from agent.image_gen_provider import _load_cache_config

        with patch("agent.image_gen_provider._load_image_gen_config", return_value={}):
            cfg = _load_cache_config()
        assert cfg["max_age_days"] == 30
        assert cfg["max_total_mb"] == 2048

    def test_load_cache_config_overrides(self, cache_dir):
        from agent.image_gen_provider import _load_cache_config

        with patch(
            "agent.image_gen_provider._load_image_gen_config",
            return_value={"cache": {"max_age_days": 7, "max_total_mb": 512}},
        ):
            cfg = _load_cache_config()
        assert cfg["max_age_days"] == 7
        assert cfg["max_total_mb"] == 512


# ---------------------------------------------------------------------------
# save functions invoke the janitor
# ---------------------------------------------------------------------------


class TestSaveHooks:
    def test_save_b64_image_invokes_gc_with_keep(self, cache_dir):
        from agent import image_gen_provider as mod

        b64 = base64.b64encode(b"\x89PNG\r\n").decode("ascii")
        with patch.object(mod, "_prune_image_cache") as gc:
            saved = mod.save_b64_image(b64, prefix="test")
        assert saved.exists()
        gc.assert_called_once()
        # keep= is the just-written path.
        assert gc.call_args.kwargs.get("keep") == saved

    def test_save_url_image_invokes_gc_with_keep(self, cache_dir):
        from unittest.mock import MagicMock

        from agent import image_gen_provider as mod

        resp = MagicMock()
        resp.headers = {"Content-Type": "image/png"}
        resp.raise_for_status = MagicMock()
        resp.iter_content = lambda chunk_size=0: [b"\x89PNG\r\n"]

        with patch("requests.get", return_value=resp), \
             patch.object(mod, "_prune_image_cache") as gc:
            saved = mod.save_url_image("https://x/y.png", prefix="test")
        assert saved.exists()
        gc.assert_called_once()
        assert gc.call_args.kwargs.get("keep") == saved

    def test_save_still_returns_path_when_gc_raises(self, cache_dir):
        from agent import image_gen_provider as mod

        b64 = base64.b64encode(b"\x89PNG\r\n").decode("ascii")
        with patch.object(mod, "_prune_image_cache", side_effect=RuntimeError("gc boom")):
            saved = mod.save_b64_image(b64, prefix="test")
        assert saved.exists()
