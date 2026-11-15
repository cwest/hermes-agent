"""A turn subprocess must resolve ITS OWN thread, never a sibling's (empty id).

PR #159 closed the case where a tool subprocess's ``HERMES_SESSION_ID`` differs
from the inherited ``HERMES_KANBAN_ORIGIN`` mirror's. The case it left open was
observed live on 2026-11-15 (gateway restarted with #159): inside a Discord turn's
terminal subprocess

- ``HERMES_SESSION_ID`` was present but EMPTY;
- ``HERMES_SESSION_THREAD_ID`` named the live thread (1566178432407044287);
- the inherited mirror named a DIFFERENT thread (1564649651460640769), carried
  ``session_id: null`` and the gateway's pid as ``_owner_pid``;

so the empty id disabled #159's session guard, the foreign-pid mirror passed the
owner-pid guard, and a card filed from that turn reported to the sibling thread.

Two spawn-seam defects produced that env, plus one reader regression:

1. ``GatewayRunner._set_session_env`` never passed the turn's ``session_id`` to
   ``set_session_vars``, binding the ContextVar to ``""``. The subprocess bridge
   treats a bound ``""`` as authoritative, so the child saw an empty id, and the
   root origin capture stamped ``session_id=None``.
2. The subprocess bridge carried the process-global origin mirror (last writer
   wins across concurrent turns) instead of THIS task's origin ContextVar.
3. #159 rejected every null-session mirror whenever the reader had a session id.
   A real dispatched worker always has one (``agent_init`` binds the worker's own
   id), so the dispatcher's seed (``session_id=None`` by construction) was
   discarded and worker-filed cards lost their inherited origin.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import gateway.session_context as sc
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource
from gateway.session_context import (
    _VAR_MAP,
    capture_kanban_origin_from_session,
    clear_session_vars,
    get_kanban_origin,
    get_session_env,
)
from tools.environments.local import _make_run_env

_ORIGIN_ENV = "HERMES_KANBAN_ORIGIN"
_REPO_ROOT = Path(__file__).resolve().parents[2]

# The exact ids from the live report.
LIVE_THREAD = "1566178432407044287"
SIBLING_THREAD = "1564649651460640769"
LIVE_SESSION = "20261115_115319_7288f621"

_CHILD_PROGRAM = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from gateway.session_context import (\n"
    "    capture_kanban_origin_from_session, get_kanban_origin)\n"
    "print(json.dumps({'origin': get_kanban_origin(),\n"
    "                  'captured': capture_kanban_origin_from_session()}))\n"
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Temp HERMES_HOME + clean ContextVar / mirror / session slate per test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    saved_env = {k: os.environ.get(k) for k in [*_VAR_MAP, _ORIGIN_ENV]}
    saved_ctx = {name: var.get() for name, var in _VAR_MAP.items()}
    saved_origin_ctx = sc._KANBAN_ORIGIN.get()
    saved_engaged = sc._session_context_engaged
    for name, var in _VAR_MAP.items():
        var.set(sc._UNSET)
        os.environ.pop(name, None)
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


def _sibling_mirror(owner_pid: int, session_id=None) -> str:
    return json.dumps({
        "platform": "discord", "chat_id": SIBLING_THREAD,
        "thread_id": SIBLING_THREAD, "user_id": None, "session_id": session_id,
        "_owner_pid": owner_pid,
    })


def _bind_live_turn(monkeypatch, session_id: str = "") -> None:
    """Simulate the tool-subprocess env: live surface bridged, ContextVars unset."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", LIVE_THREAD)
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", LIVE_THREAD)
    monkeypatch.setenv("HERMES_SESSION_ID", session_id)


# --------------------------------------------------------------------------- #
# Reader guard (get_kanban_origin) — the exact live env.
# --------------------------------------------------------------------------- #

def test_exact_live_env_rejects_sibling_thread_mirror(monkeypatch):
    """Empty HERMES_SESSION_ID + live thread set + sibling null-session mirror."""
    _bind_live_turn(monkeypatch, session_id="")
    os.environ[_ORIGIN_ENV] = _sibling_mirror(owner_pid=os.getpid() + 100000)

    assert get_kanban_origin() is None, (
        "turn subprocess resolved a SIBLING thread's origin off the mirror "
        f"(live thread {LIVE_THREAD})"
    )
    captured = capture_kanban_origin_from_session()
    assert captured is not None
    assert captured["thread_id"] == LIVE_THREAD, captured


def test_thread_mismatch_rejected_even_when_session_ids_agree(monkeypatch):
    """A mirror naming a different thread than the live one is never this turn's."""
    _bind_live_turn(monkeypatch, session_id=LIVE_SESSION)
    os.environ[_ORIGIN_ENV] = _sibling_mirror(
        owner_pid=os.getpid() + 100000, session_id=LIVE_SESSION,
    )
    assert get_kanban_origin() is None


def test_thread_only_env_rejects_sibling_thread_mirror(monkeypatch):
    """Only HERMES_SESSION_THREAD_ID set: the thread guard still applies."""
    monkeypatch.setenv("HERMES_SESSION_ID", "")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", LIVE_THREAD)
    os.environ[_ORIGIN_ENV] = _sibling_mirror(owner_pid=os.getpid() + 100000)
    assert get_kanban_origin() is None


def test_turn_subprocess_accepts_its_own_origin(monkeypatch):
    """Same thread AND same session id: the inherited origin is this turn's."""
    _bind_live_turn(monkeypatch, session_id=LIVE_SESSION)
    os.environ[_ORIGIN_ENV] = json.dumps({
        "platform": "discord", "chat_id": LIVE_THREAD, "thread_id": LIVE_THREAD,
        "user_id": "U", "session_id": LIVE_SESSION,
        "_owner_pid": os.getpid() + 100000,
    })
    got = get_kanban_origin()
    assert got is not None
    assert got["thread_id"] == LIVE_THREAD, got


def test_real_worker_env_keeps_dispatcher_seed(monkeypatch):
    """A dispatched worker binds its OWN session id but has no live surface.

    The dispatcher seed carries ``session_id=None`` and no owner pid
    (kanban_db.worker_origin_env). ``agent_init`` always binds the worker's own
    HERMES_SESSION_ID, so a guard keyed on "reader has a session id" discards the
    seed. The worker must keep it — it is the worker's only origin source.
    """
    monkeypatch.setenv("HERMES_SESSION_ID", "20261115_125449_4f0eb6")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    os.environ[_ORIGIN_ENV] = json.dumps({
        "platform": "discord", "chat_id": LIVE_THREAD, "thread_id": LIVE_THREAD,
        "user_id": None, "session_id": None,
    })
    got = get_kanban_origin()
    assert got is not None, "real worker env lost its dispatcher-seeded origin"
    assert got["thread_id"] == LIVE_THREAD, got


def test_worker_with_foreign_live_surface_keeps_dispatcher_seed(monkeypatch):
    """A worker whose own env names a webhook surface still keeps its seed.

    A worker woken by a webhook carries that run's HERMES_SESSION_PLATFORM /
    _CHAT_ID. The seed has no owner-pid stamp (only set_kanban_origin stamps), so
    it is an explicit spawn-boundary inheritance, not a turn's leftover, and the
    live-surface rules do not apply to it.
    """
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webhook")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "DETACHED_WORKER")
    monkeypatch.setenv("HERMES_SESSION_ID", "20261115_125449_4f0eb6")
    os.environ[_ORIGIN_ENV] = json.dumps({
        "platform": "discord", "chat_id": LIVE_THREAD, "thread_id": LIVE_THREAD,
        "user_id": None, "session_id": None,
    })
    got = get_kanban_origin()
    assert got is not None, "worker on a webhook surface lost its dispatcher seed"
    assert got["thread_id"] == LIVE_THREAD, got


# --------------------------------------------------------------------------- #
# Spawn seam — the gateway bind + the subprocess bridge.
# --------------------------------------------------------------------------- #

def _live_context() -> SessionContext:
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=LIVE_THREAD,
        chat_name="thread",
        chat_type="thread",
        user_id="U1",
        user_name="casey",
        thread_id=LIVE_THREAD,
    )
    ctx = SessionContext(source=source, connected_platforms=[], home_channels={})
    ctx.session_key = f"agent:main:discord:thread:{LIVE_THREAD}:{LIVE_THREAD}"
    ctx.session_id = LIVE_SESSION
    return ctx


def test_gateway_bind_exposes_turn_session_id():
    """_set_session_env must bind the turn's session id, not an empty string."""
    runner = object.__new__(GatewayRunner)
    tokens = runner._set_session_env(_live_context())
    try:
        assert get_session_env("HERMES_SESSION_ID") == LIVE_SESSION
        origin = get_kanban_origin()
        assert origin is not None
        assert origin["session_id"] == LIVE_SESSION, origin
    finally:
        clear_session_vars(tokens)


def test_bridge_carries_this_turns_origin_not_the_global_mirror():
    """The child env's origin is the task's own, not the last-writer mirror."""
    runner = object.__new__(GatewayRunner)
    tokens = runner._set_session_env(_live_context())
    try:
        # A concurrent sibling turn (same gateway process) writes the global
        # mirror AFTER this turn bound its origin — last writer wins.
        os.environ[_ORIGIN_ENV] = _sibling_mirror(owner_pid=os.getpid())
        child_env = _make_run_env({})
    finally:
        clear_session_vars(tokens)

    assert child_env.get("HERMES_SESSION_ID") == LIVE_SESSION
    blob = json.loads(child_env[_ORIGIN_ENV])
    assert blob["thread_id"] == LIVE_THREAD, blob
    assert blob["session_id"] == LIVE_SESSION, blob


def test_real_subprocess_files_to_its_own_thread():
    """E2E across a real interpreter: the child resolves the live turn's thread."""
    runner = object.__new__(GatewayRunner)
    tokens = runner._set_session_env(_live_context())
    try:
        os.environ[_ORIGIN_ENV] = _sibling_mirror(owner_pid=os.getpid())
        child_env = _make_run_env({})
    finally:
        clear_session_vars(tokens)

    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_PROGRAM, str(_REPO_ROOT)],
        env=child_env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert out["captured"] is not None
    assert out["captured"]["thread_id"] == LIVE_THREAD, out
    assert out["captured"]["session_id"] == LIVE_SESSION, out
