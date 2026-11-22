"""`hermes computer-use doctor` fails when /Applications/CuaDriver.app — the path cua-driver launches its
daemon from — or the daemon on the socket is not the pinned driver.

Regression: a standalone 0.7.0 CuaDriver.app owned the daemon socket the pinned driver needed, while every
binary-level check stayed green and doctor exited 0.
"""

from __future__ import annotations

import json
import plistlib
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pm import cua_app_link

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


def _ok_report(version: str) -> dict:
    return {"schema_version": "1", "platform": "darwin", "driver_version": version, "overall": "ok",
            "checks": [{"name": "binary_version", "status": "pass", "message": f"cua-driver {version}"},
                       {"name": "tcc_accessibility", "status": "pass", "message": "granted"}]}


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A pinned 0.52.0 app in the store, a stale 0.21.0 one beside it, and a fake /Applications."""
    from tools.computer_use import doctor

    store = tmp_path / "store"
    pinned = _app(store / "cua-driver-0.52.0-darwin-arm64", "0.52.0")
    stale = _app(store / "cua-driver-0.21.0-darwin-arm64", "0.21.0")
    link = tmp_path / "Applications" / "CuaDriver.app"
    link.parent.mkdir()
    monkeypatch.setattr(cua_app_link, "SYSTEM_APP", link)
    monkeypatch.delenv("HERMES_CUA_DRIVER_CMD", raising=False)
    monkeypatch.setattr(doctor, "_read_cli_version", lambda binary, timeout=5.0: "cua-driver 0.52.0")
    monkeypatch.setattr(doctor, "_socket_owner_version", lambda binary: None, raising=False)
    binary = pinned / "Contents" / "MacOS" / "cua-driver"
    return SimpleNamespace(pinned=pinned, stale=stale, link=link, binary=binary, doctor=doctor)


def _run(host, *, guard=True) -> tuple[int, dict]:
    doctor = host.doctor
    with patch("pm.installed_package", return_value=SimpleNamespace(binary=host.binary)), \
         patch.object(doctor, "_drive_health_report", return_value=_ok_report("0.52.0")), \
         patch("sys.stdout", new_callable=StringIO) as out:
        if guard:
            code = doctor.run_doctor(json_output=True)
        else:
            with patch.object(doctor, "_apply_app_link_guard", lambda report, binary: report, create=True):
                code = doctor.run_doctor(json_output=True)
    payload = json.loads(out.getvalue())
    return code, {c["name"]: c for c in payload["checks"]}


def test_link_to_a_non_pinned_version_fails_where_it_used_to_pass(host):
    host.link.symlink_to(host.stale)

    # Negative control: the pre-fix doctor (no app-link guard) calls this host healthy.
    assert _run(host, guard=False)[0] == 0

    code, checks = _run(host)
    assert code == 1
    row = checks["app_link"]
    assert row["status"] == "fail"
    assert "0.21.0" in row["message"] and "not the pinned cua-driver 0.52.0" in row["message"]
    assert row["hint"] == "Run: hermes computer-use install"


@pytest.mark.parametrize("shape", ["missing", "dangling"])
def test_missing_or_dangling_link_fails(host, shape):
    if shape == "dangling":
        host.link.symlink_to(host.link.parent / "gone" / "CuaDriver.app")
    code, checks = _run(host)
    assert code == 1
    assert checks["app_link"]["status"] == "fail"
    assert shape in checks["app_link"]["message"]
    assert "hermes computer-use install" in checks["app_link"]["hint"]


def test_foreign_bundle_of_another_version_fails_with_move_aside_hint(host):
    _app(host.link.parent, "0.7.0")
    code, checks = _run(host)
    assert code == 1
    assert "0.7.0" in checks["app_link"]["message"]
    assert "Move" in checks["app_link"]["hint"]


def test_link_to_the_pin_passes(host):
    host.link.symlink_to(host.pinned)
    code, checks = _run(host)
    assert code == 0
    assert checks["app_link"]["status"] == "pass"


def test_socket_owner_of_another_version_fails(host, monkeypatch):
    host.link.symlink_to(host.pinned)
    monkeypatch.setattr(host.doctor, "_socket_owner_version", lambda binary: "0.7.0")
    code, checks = _run(host)
    assert code == 1
    row = checks["daemon_version"]
    assert row["status"] == "fail" and "0.7.0" in row["message"] and "0.52.0" in row["message"]
    assert "hermes computer-use install" in row["hint"]


def test_socket_owner_version_reads_the_serving_app(tmp_path, monkeypatch):
    """The owner's version comes from the app bundle of the pid `status` reports, not from the pin."""
    from tools.computer_use import doctor

    stale = _app(tmp_path / "standalone", "0.7.0")
    stale_exe = stale / "Contents" / "MacOS" / "cua-driver"

    def cli(binary, *args, timeout, exc_types=doctor._IO_EXC):
        if args == ("status",):
            return SimpleNamespace(returncode=0, stdout="Cua Driver daemon is running\n  socket: /x.sock\n  pid: 4242\n",
                                   stderr="")
        assert (binary, args) == ("/bin/ps", ("-o", "comm=", "-p", "4242"))
        return SimpleNamespace(returncode=0, stdout=f"{stale_exe}\n", stderr="")

    monkeypatch.setattr(doctor, "_cli_text", cli)
    assert doctor._socket_owner_version("/pinned/cua-driver") == "0.7.0"


def test_socket_owner_unknown_when_no_daemon(monkeypatch):
    from tools.computer_use import doctor

    monkeypatch.setattr(doctor, "_cli_text", lambda binary, *args, timeout, exc_types=doctor._IO_EXC:
                        SimpleNamespace(returncode=1, stdout="", stderr="daemon is not running"))
    assert doctor._socket_owner_version("/pinned/cua-driver") is None


def test_override_outside_an_app_is_not_judged(host, monkeypatch, tmp_path):
    bare = tmp_path / "bin" / "cua-driver"
    bare.parent.mkdir()
    bare.write_text("#!/bin/sh\n")
    bare.chmod(0o755)
    host.binary = bare
    code, checks = _run(host)
    assert code == 0
    assert "app_link" not in checks
