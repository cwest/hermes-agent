"""Origin-channel ContextVar behaviour for kanban inheritance.

The kanban "origin" (the concrete delivery surface a transition wake routes to)
must survive the spawn boundary into detached contexts (dispatched worker,
delegate_task, background process) WITHOUT riding the session-identity vars,
which are deliberately reset/stripped per message to stop cross-session leaks.

These tests pin the contract of the standalone ``HERMES_KANBAN_ORIGIN`` channel:

- it is NOT a member of ``_VAR_MAP`` (so ``reset_session_vars`` and the
  ``_inject_session_context_env`` engaged-strip never touch it);
- ``set_kanban_origin`` mirrors to ``os.environ`` (like ``set_current_session_id``)
  so it crosses the process boundary via the already-copied environ;
- ``capture_kanban_origin_from_session`` INHERITS an already-set origin verbatim,
  else snapshots the live ``HERMES_SESSION_*`` as the root capture, else ``None``;
- binding an origin does NOT mutate any ``HERMES_SESSION_*`` var (C4 invariant).
"""

import json
import os

import pytest

import gateway.session_context as sc
from gateway.session_context import (
    _VAR_MAP,
    capture_kanban_origin_from_session,
    clear_session_vars,
    get_kanban_origin,
    reset_session_vars,
    set_kanban_origin,
    set_session_vars,
)

_ORIGIN_ENV = "HERMES_KANBAN_ORIGIN"
SESSION_VARS = list(_VAR_MAP.keys())


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Clean ContextVar + os.environ + engaged-latch + origin slate per test."""
    saved_env = {k: os.environ.get(k) for k in SESSION_VARS}
    saved_origin = os.environ.get(_ORIGIN_ENV)
    saved_ctx = {name: var.get() for name, var in _VAR_MAP.items()}
    saved_engaged = sc._session_context_engaged
    saved_origin_ctx = sc._KANBAN_ORIGIN.get()
    for var in _VAR_MAP.values():
        var.set(sc._UNSET)
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    os.environ.pop(_ORIGIN_ENV, None)
    sc._session_context_engaged = False
    try:
        yield
    finally:
        for var, val in zip(_VAR_MAP.values(), saved_ctx.values()):
            var.set(val)
        sc._KANBAN_ORIGIN.set(saved_origin_ctx)
        sc._session_context_engaged = saved_engaged
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if saved_origin is None:
            os.environ.pop(_ORIGIN_ENV, None)
        else:
            os.environ[_ORIGIN_ENV] = saved_origin


# --------------------------------------------------------------------------- #
# The channel is deliberately OUTSIDE _VAR_MAP.
# --------------------------------------------------------------------------- #

def test_origin_var_not_in_var_map():
    """The origin var must NOT be a _VAR_MAP member (design §3.1).

    Membership would subject it to reset_session_vars strip-to-_UNSET and the
    _inject_session_context_env engaged-strip, both of which would drop the
    inherited origin in a detached child — exactly what must NOT happen.
    """
    assert _ORIGIN_ENV not in _VAR_MAP


# --------------------------------------------------------------------------- #
# set / get round-trip + os.environ mirror
# --------------------------------------------------------------------------- #

def test_set_get_round_trip():
    set_kanban_origin(
        platform="discord", chat_id="C1", thread_id="T1",
        user_id="U1", session_id="S1",
    )
    got = get_kanban_origin()
    assert got == {
        "platform": "discord", "chat_id": "C1", "thread_id": "T1",
        "user_id": "U1", "session_id": "S1",
    }


def test_set_mirrors_to_os_environ_for_subprocess_bridge():
    """Origin must mirror to os.environ so it crosses the process boundary."""
    set_kanban_origin(platform="discord", chat_id="C1", thread_id="T1")
    blob = os.environ.get(_ORIGIN_ENV)
    assert blob, "origin was not mirrored into os.environ"
    parsed = json.loads(blob)
    assert parsed["platform"] == "discord"
    assert parsed["chat_id"] == "C1"
    assert parsed["thread_id"] == "T1"


def test_get_reads_os_environ_when_contextvar_unset():
    """A child process inherits origin only via os.environ (ContextVar _UNSET)."""
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    os.environ[_ORIGIN_ENV] = json.dumps(
        {"platform": "discord", "chat_id": "C9", "thread_id": "T9"}
    )
    got = get_kanban_origin()
    assert got["platform"] == "discord"
    assert got["chat_id"] == "C9"
    assert got["thread_id"] == "T9"


def test_get_returns_none_when_unset_everywhere():
    assert get_kanban_origin() is None


# --------------------------------------------------------------------------- #
# capture: INHERIT existing > snapshot live session > None
# --------------------------------------------------------------------------- #

def test_capture_inherits_existing_origin_verbatim():
    """When an origin is already set, capture returns it verbatim (INHERIT).

    Even if HERMES_SESSION_* names a DIFFERENT (detached) identity, the inherited
    origin wins — this is the whole point of crossing the spawn boundary.
    """
    set_kanban_origin(platform="discord", chat_id="HUMAN", thread_id="HT")
    tokens = set_session_vars(
        platform="webhook", chat_id="DETACHED", thread_id="",
    )
    try:
        got = capture_kanban_origin_from_session()
    finally:
        clear_session_vars(tokens)
    assert got["platform"] == "discord"
    assert got["chat_id"] == "HUMAN"
    assert got["thread_id"] == "HT"


def test_capture_snapshots_live_session_as_root():
    """No inherited origin but a live session → snapshot it as the ROOT origin."""
    tokens = set_session_vars(
        platform="discord", chat_id="ROOTCHAT", thread_id="ROOTTHREAD",
        user_id="ROOTUSER", session_id="ROOTSESS",
    )
    try:
        got = capture_kanban_origin_from_session()
    finally:
        clear_session_vars(tokens)
    assert got["platform"] == "discord"
    assert got["chat_id"] == "ROOTCHAT"
    assert got["thread_id"] == "ROOTTHREAD"


def test_capture_returns_none_in_detached_context():
    """No inherited origin AND no live session (platform/chat) → None."""
    assert capture_kanban_origin_from_session() is None


# --------------------------------------------------------------------------- #
# C4 — binding an origin does NOT mutate session identity.
# --------------------------------------------------------------------------- #

def test_set_origin_does_not_mutate_session_vars():
    """C4: origin binding leaves every HERMES_SESSION_* var untouched."""
    before = {name: var.get() for name, var in _VAR_MAP.items()}
    set_kanban_origin(platform="discord", chat_id="C", thread_id="T")
    after = {name: var.get() for name, var in _VAR_MAP.items()}
    assert before == after


def test_reset_session_vars_does_not_clear_origin():
    """C2 foundation: reset_session_vars (per-message) must NOT drop the origin.

    A freshly-spawned task resets its session identity at the top of the handler,
    but the inherited kanban origin must survive that reset.
    """
    set_kanban_origin(platform="discord", chat_id="KEEP", thread_id="KT")
    reset_session_vars()
    got = get_kanban_origin()
    assert got is not None
    assert got["chat_id"] == "KEEP"


# --------------------------------------------------------------------------- #
# Root capture at session bind (does not clobber an inherited origin).
# --------------------------------------------------------------------------- #

def test_root_capture_binds_live_session_when_no_origin():
    """A live session with no inherited origin becomes the ROOT origin."""
    from gateway.session_context import capture_root_origin_if_absent
    tokens = set_session_vars(
        platform="discord", chat_id="ROOT", thread_id="RT", user_id="RU",
    )
    try:
        captured = capture_root_origin_if_absent()
    finally:
        clear_session_vars(tokens)
    assert captured is not None
    got = get_kanban_origin()
    assert got["platform"] == "discord"
    assert got["chat_id"] == "ROOT"
    assert got["thread_id"] == "RT"


def test_root_capture_live_session_is_authoritative():
    """A live gateway turn (re)binds its OWN origin, overriding a leaked one.

    The handler-entry reset_kanban_origin drops a sibling's inherited origin to
    _UNSET; the live turn then stamps its own surface. Simulate that sequence.
    """
    from gateway.session_context import (
        capture_root_origin_if_absent,
        reset_kanban_origin,
    )
    # A concurrent sibling had leaked its origin into this task's ContextVar.
    set_kanban_origin(platform="discord", chat_id="SIBLING_LEAK", thread_id="X")
    # Handler entry resets it; the live turn binds its own.
    reset_kanban_origin()
    tokens = set_session_vars(
        platform="discord", chat_id="MY_LIVE", thread_id="MT",
    )
    try:
        capture_root_origin_if_absent()
    finally:
        clear_session_vars(tokens)
    got = get_kanban_origin()
    assert got["chat_id"] == "MY_LIVE", got


def test_detached_context_preserves_inherited_worker_origin():
    """No live session (a worker subprocess) → inherited worker origin survives.

    A dispatched worker carries the human origin via the os.environ mirror and
    never binds a live gateway session; capture must NOT wipe it.
    """
    from gateway.session_context import capture_root_origin_if_absent
    import os
    # Simulate the seeded worker env: origin in the mirror, ContextVar unset.
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    os.environ[_ORIGIN_ENV] = json.dumps(
        {"platform": "discord", "chat_id": "WORKER_HUMAN", "thread_id": "WT"}
    )
    # No live session bound.
    captured = capture_root_origin_if_absent()
    assert captured is None  # nothing to bind (no live session)
    got = get_kanban_origin()
    assert got["chat_id"] == "WORKER_HUMAN", got


def test_root_capture_noop_without_live_session():
    """No live session (platform/chat) and no inherited origin → binds nothing."""
    from gateway.session_context import capture_root_origin_if_absent
    captured = capture_root_origin_if_absent()
    assert captured is None
    assert get_kanban_origin() is None

