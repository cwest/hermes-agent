"""The ``/Applications/CuaDriver.app`` link that the PM-pinned cua-driver needs on macOS.

cua-driver hardcodes ``/Applications/CuaDriver.app`` for its daemon launch, so whatever bundle sits
there owns the machine-wide daemon socket. PM installs the signed app inside a versioned store entry
(``cua-driver-<version>-<target>/CuaDriver.app``); this module keeps the system path a symlink to the
active entry's app, reports anything else there, and names the entry the link holds so the store
sweep never deletes it. Only a symlink is ever replaced: a real bundle belongs to someone else.
"""

from __future__ import annotations

import os
import plistlib
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SYSTEM_APP = Path("/Applications/CuaDriver.app")
FIX_COMMAND = "hermes computer-use install"

OK, MISSING, DANGLING, DRIFT, FOREIGN = "ok", "missing", "dangling", "drift", "foreign"


def managed_here() -> bool:
    """Only macOS has the hardcoded app path."""
    return sys.platform == "darwin"


@dataclass(frozen=True)
class AppLinkState:
    status: str
    link: Path
    expected: Path
    target: Optional[Path] = None
    version: Optional[str] = None
    expected_version: Optional[str] = None

    @property
    def healthy(self) -> bool:
        """A link to the pinned app, or a real bundle of the same version someone else installed."""
        return self.status == OK or (self.status == FOREIGN and self.version is not None
                                     and self.version == self.expected_version)

    def describe(self) -> str:
        link, want = self.link, self.expected_version or "?"
        if self.status == OK:
            return f"{link} -> {self.target} (cua-driver {want})"
        if self.status == MISSING:
            return f"{link} is missing; the pinned cua-driver {want} launches its daemon from that path"
        if self.status == DANGLING:
            return f"{link} is a dangling symlink to {self.target}; the pinned cua-driver {want} cannot launch its daemon"
        if self.status == DRIFT:
            return (f"{link} resolves to {self.target} (cua-driver {self.version or '?'}), "
                    f"not the pinned cua-driver {want} at {self.expected}")
        return (f"{link} is a real app bundle Hermes does not manage (cua-driver {self.version or '?'}); "
                f"the pinned cua-driver is {want}")

    def fix(self) -> str:
        if self.status == FOREIGN:
            return f"Move {self.link} aside (it is not a Hermes-managed symlink), then run: {FIX_COMMAND}"
        return f"Run: {FIX_COMMAND}"


def bundle_version(app: Path) -> Optional[str]:
    """``CFBundleShortVersionString`` of an app bundle, or None when unreadable."""
    try:
        with open(app / "Contents" / "Info.plist", "rb") as handle:
            value = plistlib.load(handle).get("CFBundleShortVersionString")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return str(value) if value else None


def _link_target(link: Path) -> Path:
    raw = Path(os.readlink(link))
    return raw if raw.is_absolute() else link.parent / raw


def app_link_state(expected_app: Path, link: Optional[Path] = None) -> AppLinkState:
    """Classify what sits at *link* relative to the app the pinned binary lives in."""
    link = SYSTEM_APP if link is None else link
    expected_app = Path(expected_app)
    want = bundle_version(expected_app)
    if link.is_symlink():
        target = _link_target(link)
        if not target.exists():
            return AppLinkState(DANGLING, link, expected_app, target, None, want)
        same = os.path.realpath(target) == os.path.realpath(expected_app)
        return AppLinkState(OK if same else DRIFT, link, expected_app, target, bundle_version(target), want)
    if link.exists():
        return AppLinkState(FOREIGN, link, expected_app, None, bundle_version(link), want)
    return AppLinkState(MISSING, link, expected_app, None, None, want)


def ensure_app_link(expected_app: Path, link: Optional[Path] = None) -> AppLinkState:
    """Point *link* at *expected_app* unless a real bundle owns the path; return the resulting state.

    A missing, dangling, or drifted symlink is replaced atomically (a sibling symlink renamed over
    it). A real bundle is never touched; its state comes back FOREIGN for the caller to report.
    ``OSError`` (e.g. a non-admin account that cannot write ``/Applications``) propagates.
    """
    link = SYSTEM_APP if link is None else link
    state = app_link_state(expected_app, link)
    if state.status in (OK, FOREIGN):
        return state
    staged = link.with_name(f".{link.name}.hermes-{uuid.uuid4().hex[:8]}")
    os.symlink(os.path.realpath(expected_app), staged)
    try:
        os.replace(staged, link)
    except OSError:
        staged.unlink(missing_ok=True)
        raise
    return app_link_state(expected_app, link)


def selected_app() -> Optional[tuple[Path, str]]:
    """(app bundle, version) of PM's selected cua-driver pin, or None when it is not a macOS app install."""
    from pm.install import installed_package

    installed = installed_package("cua-driver")
    if installed is None or installed.binary is None:
        return None
    head, marker, _ = str(installed.binary).partition(".app/Contents/MacOS/")
    return (Path(head + ".app"), installed.version) if marker else None


def sync_installed_app_link() -> Optional[str]:
    """Point the system app path at the selected pin after an install or bump; return a status line.

    Never raises: the link is host integration around an install that already succeeded, so a
    failure here is reported, not fatal. A user-selected driver (``HERMES_CUA_DRIVER_CMD``) owns its
    own app and is left alone, as is a host without the hardcoded path.
    """
    if not managed_here() or os.environ.get("HERMES_CUA_DRIVER_CMD", "").strip():
        return None
    try:
        selected = selected_app()
        if selected is None:
            return None
        state = ensure_app_link(selected[0])
    except OSError as exc:
        return f"⚠ could not point {SYSTEM_APP} at the pinned cua-driver: {exc}. {FIX_COMMAND} retries it."
    if state.status == OK:
        return f"✓ {state.describe()}"
    return f"⚠ {state.describe()}. {state.fix()}"


def linked_store_entries(store_root: Path, link: Optional[Path] = None) -> set[str]:
    """Store entry names the system app link points into (dangling included), for the store sweep."""
    link = SYSTEM_APP if link is None else link
    try:
        if not link.is_symlink():
            return set()
        target = Path(os.path.normpath(_link_target(link)))
    except OSError:
        return set()
    for root in {Path(os.path.normpath(store_root)), Path(os.path.realpath(store_root))}:
        try:
            parts = target.relative_to(root).parts
        except ValueError:
            continue
        if parts:
            return {parts[0]}
    return set()
