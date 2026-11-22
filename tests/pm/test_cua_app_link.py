"""PM owns /Applications/CuaDriver.app: it re-points the link at the active pin, never replaces a
real bundle, and the store sweep keeps whatever entry the link holds."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest

from pm import cua_app_link
from pm.cua_app_link import DANGLING, DRIFT, FOREIGN, MISSING, OK, app_link_state, ensure_app_link
from pm.install import InstalledPackage
from pm.store import Store

# The hardcoded app path, and the sync that owns it, exist only on macOS.
pytestmark = pytest.mark.platforms("macos")


def _app(root: Path, version: str) -> Path:
    app = root / "CuaDriver.app"
    exe = app / "Contents" / "MacOS" / "cua-driver"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    with open(app / "Contents" / "Info.plist", "wb") as handle:
        plistlib.dump({"CFBundleShortVersionString": version}, handle)
    return app


def _entry(store_root: Path, version: str) -> Path:
    return _app(store_root / f"cua-driver-{version}-darwin-arm64", version)


@pytest.fixture
def link(tmp_path, monkeypatch) -> Path:
    applications = tmp_path / "Applications"
    applications.mkdir()
    path = applications / "CuaDriver.app"
    monkeypatch.setattr(cua_app_link, "SYSTEM_APP", path)
    monkeypatch.delenv("HERMES_CUA_DRIVER_CMD", raising=False)
    return path


def _select(monkeypatch, app: Path, version: str) -> None:
    """PM reports *app*'s binary as the selected cua-driver pin."""
    import pm.install

    monkeypatch.setattr(pm.install, "installed_package", lambda name, **kw: InstalledPackage(
        app.parent, version, app / "Contents" / "MacOS" / "cua-driver"))


def test_missing_link_is_created(tmp_path, link):
    app = _entry(tmp_path / "store", "0.52.0")
    assert app_link_state(app).status == MISSING
    assert ensure_app_link(app).status == OK
    assert link.is_symlink() and link.resolve() == app.resolve()


def test_dangling_and_drifted_links_are_repointed(tmp_path, link):
    store = tmp_path / "store"
    old, new = _entry(store, "0.21.0"), _entry(store, "0.52.0")
    link.symlink_to(old)
    state = app_link_state(new)
    assert (state.status, state.version, state.expected_version) == (DRIFT, "0.21.0", "0.52.0")
    assert ensure_app_link(new).status == OK
    assert link.resolve() == new.resolve()

    link.unlink()
    link.symlink_to(store / "cua-driver-0.9.0-darwin-arm64" / "CuaDriver.app")
    assert app_link_state(new).status == DANGLING
    assert ensure_app_link(new).status == OK
    assert link.resolve() == new.resolve()


def test_foreign_bundle_is_reported_not_replaced(tmp_path, link):
    app = _entry(tmp_path / "store", "0.52.0")
    foreign = _app(link.parent, "0.7.0")
    marker = foreign / "Contents" / "user-owned"
    marker.write_text("keep")

    state = ensure_app_link(app)

    assert (state.status, state.version) == (FOREIGN, "0.7.0")
    assert not state.healthy
    assert not link.is_symlink() and marker.read_text() == "keep"
    assert "not a Hermes-managed symlink" in state.fix()
    assert "0.7.0" in state.describe() and "0.52.0" in state.describe()


def test_sync_reports_foreign_bundle_without_touching_it(tmp_path, link, monkeypatch):
    app = _entry(tmp_path / "store", "0.52.0")
    _select(monkeypatch, app, "0.52.0")
    _app(link.parent, "0.7.0")

    message = cua_app_link.sync_installed_app_link()

    assert message is not None and message.startswith("⚠") and "0.7.0" in message
    assert not link.is_symlink()


def test_sync_leaves_an_override_alone(tmp_path, link, monkeypatch):
    _select(monkeypatch, _entry(tmp_path / "store", "0.52.0"), "0.52.0")
    monkeypatch.setenv("HERMES_CUA_DRIVER_CMD", "/opt/custom/cua-driver")
    assert cua_app_link.sync_installed_app_link() is None
    assert not link.exists() and not link.is_symlink()


def _facts(store_root: Path, entry: str, version: str) -> None:
    # Rows without owners are always live to the sweep's pin check.
    (store_root / "facts.json").write_text(json.dumps(
        {"schema": 1, "packages": {"cua-driver": {"entry": entry, "version": version, "env": {}}}}))


def test_pin_bump_repoints_link_and_gc_keeps_its_target(tmp_path, link, monkeypatch):
    from pm.cli import _gc_store
    from pm.lock import Facts

    store_root = tmp_path / "store"
    old, new = _entry(store_root, "0.52.0"), _entry(store_root, "0.53.0")
    link.symlink_to(old)

    # The bump publishes 0.53.0 and selects it; the facts no longer name 0.52.0.
    _facts(store_root, new.parent.name, "0.53.0")
    _select(monkeypatch, new, "0.53.0")
    message = cua_app_link.sync_installed_app_link()

    assert message is not None and "0.53.0" in message
    assert link.resolve() == new.resolve()
    _gc_store(Store(store_root), Facts(store_root / "facts.json"))
    assert new.is_dir() and link.resolve() == new.resolve()
    assert not old.parent.exists()


def test_gc_never_deletes_the_entry_the_link_still_targets(tmp_path, link):
    """Before the link is re-pointed (an update that failed half-way), GC must not dangle it."""
    from pm.cli import _gc_store
    from pm.lock import Facts

    store_root = tmp_path / "store"
    linked, selected = _entry(store_root, "0.52.0"), _entry(store_root, "0.53.0")
    link.symlink_to(linked)
    _facts(store_root, selected.parent.name, "0.53.0")

    _gc_store(Store(store_root), Facts(store_root / "facts.json"))

    assert linked.is_dir() and selected.is_dir()
    assert app_link_state(selected).status == DRIFT


def test_pm_install_of_cua_driver_syncs_the_link(monkeypatch, capsys):
    import pm.cli

    calls = []
    monkeypatch.setattr(pm.cli, "ensure", lambda name, **kw: None)
    monkeypatch.setattr(cua_app_link, "sync_installed_app_link",
                        lambda: calls.append("sync") or "✓ /Applications/CuaDriver.app -> pinned")
    assert pm.cli._install_names(["cua-driver"]) == 0
    assert calls == ["sync"]
    assert "/Applications/CuaDriver.app" in capsys.readouterr().out

    calls.clear()
    assert pm.cli._install_names(["faketool-not-cua"]) == 0
    assert calls == []
