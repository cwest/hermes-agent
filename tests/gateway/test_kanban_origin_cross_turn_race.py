"""Cross-turn origin poisoning race for the ``HERMES_KANBAN_ORIGIN`` mirror.

The origin channel keeps a process-global ``os.environ`` mirror so a DETACHED
worker can inherit the human origin across the spawn boundary (its own
ContextVar is ``_UNSET``, so it reads the mirror). But the mirror is
last-writer-wins across CONCURRENT gateway turns in the SAME process: turn A
binds the mirror to thread X; turn B, whose handler-entry ``reset_kanban_origin``
has just dropped its ContextVar to ``_UNSET``, then falls through to the mirror
and reads A's thread X instead of its own.

The two requirements are in tension and BOTH must hold:

  (A) Concurrent turns must not read each other's origin. A same-process
      mirror-sourced read (ContextVar ``_UNSET``) is a foreign sibling's
      leftover and must NOT be returned.
  (B) A detached worker must still inherit its origin across the spawn boundary.
      Its mirror was written by a DIFFERENT process (the parent) and copied into
      its environ at spawn — that value is legitimate and must be returned.

The discriminator is the writer's PID stamped into the mirror payload: a
mirror-sourced read is accepted only when its owner PID differs from the reading
process's PID (inherited across a spawn), and rejected when it equals the
reader's PID (a concurrent sibling in this same process).
"""

import json
import os

import pytest

import gateway.session_context as sc
from gateway.session_context import (
    capture_kanban_origin_from_session,
    clear_session_vars,
    get_kanban_origin,
    reset_kanban_origin,
    set_kanban_origin,
    set_session_vars,
)

_ORIGIN_ENV = "HERMES_KANBAN_ORIGIN"


@pytest.fixture(autouse=True)
def _isolate():
    """Clean the origin ContextVar + os.environ mirror + session vars per test."""
    saved_origin_env = os.environ.get(_ORIGIN_ENV)
    saved_origin_ctx = sc._KANBAN_ORIGIN.get()
    saved_ctx = {name: var.get() for name, var in sc._VAR_MAP.items()}
    saved_engaged = sc._session_context_engaged
    for var in sc._VAR_MAP.values():
        var.set(sc._UNSET)
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    os.environ.pop(_ORIGIN_ENV, None)
    sc._session_context_engaged = False
    try:
        yield
    finally:
        for var, val in zip(sc._VAR_MAP.values(), saved_ctx.values()):
            var.set(val)
        sc._KANBAN_ORIGIN.set(saved_origin_ctx)
        sc._session_context_engaged = saved_engaged
        if saved_origin_env is None:
            os.environ.pop(_ORIGIN_ENV, None)
        else:
            os.environ[_ORIGIN_ENV] = saved_origin_env


# --------------------------------------------------------------------------- #
# (A) Concurrent sibling turn must NOT read the poisoned mirror.
# --------------------------------------------------------------------------- #

def test_sibling_turn_does_not_read_poisoned_mirror():
    """A same-process mirror-sourced read (foreign sibling) must return None.

    Reproduces the live 2026-07-26 poisoning: turn A binds the mirror to its
    thread; turn B resets its own ContextVar at handler entry and then reads
    the origin in the race window before it binds its own. B must NOT resolve
    A's thread.
    """
    # Turn A (this same process) binds its origin — writes the ContextVar AND
    # the os.environ mirror, both stamped with THIS process's pid.
    set_kanban_origin(
        platform="discord", chat_id="A_THREAD", thread_id="A_THREAD",
        user_id="UA", session_id="SA",
    )
    # Turn B is a concurrent sibling in the same process. At handler entry it
    # drops its inherited ContextVar to _UNSET (reset_kanban_origin). The
    # os.environ mirror is process-global and still holds A's value.
    reset_kanban_origin()
    # In the race window (before B binds its own via capture_root_origin_if_absent)
    # B reads the origin. It must NOT fall through to A's poisoned mirror.
    got = get_kanban_origin()
    assert got is None, (
        "sibling turn read the poisoned mirror: got a foreign origin "
        f"{got!r} instead of None"
    )


# --------------------------------------------------------------------------- #
# (B) A detached worker MUST inherit its origin across the spawn boundary.
# --------------------------------------------------------------------------- #

def test_detached_worker_inherits_foreign_pid_mirror():
    """A mirror written by a DIFFERENT process (spawn inheritance) is accepted.

    A dispatched worker inherits the mirror via the parent's copied environ; its
    ContextVar is _UNSET and the mirror's owner pid is the parent's (not the
    worker's own). That value is legitimate and must be returned — this is the
    whole reason the mirror exists.
    """
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    # Simulate the seeded worker env: a mirror written by the PARENT process
    # (a different pid than os.getpid()).
    foreign_pid = os.getpid() + 100000
    os.environ[_ORIGIN_ENV] = json.dumps({
        "platform": "discord", "chat_id": "WORKER_HUMAN", "thread_id": "WT",
        "user_id": "UW", "session_id": "SW",
        "_owner_pid": foreign_pid,
    })
    got = get_kanban_origin()
    assert got is not None, "detached worker lost its inherited origin"
    assert got["chat_id"] == "WORKER_HUMAN", got
    assert got["thread_id"] == "WT", got


def test_mirror_without_owner_pid_is_treated_as_inherited():
    """A legacy mirror with no owner-pid stamp is treated as inherited (accepted).

    An environ mirror seeded by an older process (or a raw env export) has no
    ``_owner_pid``. Since it cannot be attributed to THIS process's concurrent
    turns, it is treated as an inherited/foreign value and returned — preserving
    the pre-fix cross-process inheritance contract for un-stamped mirrors.
    """
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    os.environ[_ORIGIN_ENV] = json.dumps({
        "platform": "discord", "chat_id": "LEGACY", "thread_id": "LT",
    })
    got = get_kanban_origin()
    assert got is not None
    assert got["chat_id"] == "LEGACY", got


def test_own_task_contextvar_still_wins():
    """When THIS task bound its own origin, the ContextVar wins over the mirror."""
    # A foreign sibling poisoned the mirror.
    os.environ[_ORIGIN_ENV] = json.dumps({
        "platform": "discord", "chat_id": "SIBLING", "thread_id": "ST",
        "_owner_pid": os.getpid(),
    })
    # But this task bound its OWN origin (ContextVar set).
    set_kanban_origin(platform="discord", chat_id="MINE", thread_id="MT")
    got = get_kanban_origin()
    assert got["chat_id"] == "MINE", got


# --------------------------------------------------------------------------- #
# Consumer chain: card filing must NOT inherit a poisoned sibling origin.
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# (C/D) A per-turn TOOL SUBPROCESS must not read a FOREIGN turn's mirror.
#
# The owner-pid guard alone (requirement A/B above) is read IN the gateway
# process. A terminal/execute_code/delegate tool subprocess spawned by the
# gateway has a DIFFERENT pid, so a mirror written by the gateway parent — whose
# value is whichever concurrent turn wrote it LAST — passes the foreign-pid
# check and is wrongly accepted as a legitimate spawn-boundary inheritance. The
# per-turn HERMES_SESSION_ID *is* bridged correctly into that subprocess env, so
# it is the discriminator: a mirror whose session_id disagrees with the
# subprocess's own HERMES_SESSION_ID is a foreign turn's leftover, not this
# turn's origin.
# --------------------------------------------------------------------------- #

def test_subprocess_rejects_foreign_turn_mirror_by_session_id(monkeypatch):
    """A tool subprocess must reject a mirror whose session_id is a foreign turn.

    Reproduces the live 2026-11-15 wrong-thread report: the gateway parent wrote
    the process-global mirror for turn B (last-writer-wins). Turn A then spawns a
    terminal subprocess; in that subprocess the ContextVar is _UNSET and the
    bridged HERMES_SESSION_ID is turn A's. The mirror's owner pid is the gateway
    parent's (foreign to the subprocess), so the owner-pid guard accepts it — but
    its session_id is turn B's. The subprocess must NOT resolve turn B's origin.
    """
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    # Seeded subprocess env: mirror written by the gateway PARENT (foreign pid)
    # for turn B (the last concurrent turn to write the process-global mirror).
    foreign_pid = os.getpid() + 100000
    os.environ[_ORIGIN_ENV] = json.dumps({
        "platform": "discord", "chat_id": "TURN_B_CHAT", "thread_id": "TURN_B_THREAD",
        "user_id": "UB", "session_id": "SESSION_B",
        "_owner_pid": foreign_pid,
    })
    # This subprocess belongs to turn A: HERMES_SESSION_ID is bridged in as A's.
    monkeypatch.setenv("HERMES_SESSION_ID", "SESSION_A")

    got = get_kanban_origin()
    assert got is None, (
        "tool subprocess read a FOREIGN turn's origin off the process-global "
        f"mirror: got {got!r} (session_id SESSION_B) while its own turn is "
        "SESSION_A"
    )


def test_subprocess_accepts_same_turn_mirror_by_session_id(monkeypatch):
    """A subprocess whose own session_id matches the mirror accepts it.

    The same-turn subprocess case: turn A spawns a tool subprocess and the
    mirror the subprocess inherited IS turn A's own origin. Its session_id
    matches the bridged HERMES_SESSION_ID, so it must be returned — the fix must
    only reject a *disagreeing* session_id, never a matching one.
    """
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    foreign_pid = os.getpid() + 100000
    os.environ[_ORIGIN_ENV] = json.dumps({
        "platform": "discord", "chat_id": "TURN_A_CHAT", "thread_id": "TURN_A_THREAD",
        "user_id": "UA", "session_id": "SESSION_A",
        "_owner_pid": foreign_pid,
    })
    monkeypatch.setenv("HERMES_SESSION_ID", "SESSION_A")

    got = get_kanban_origin()
    assert got is not None, "same-turn subprocess lost its own inherited origin"
    assert got["chat_id"] == "TURN_A_CHAT", got
    assert got["thread_id"] == "TURN_A_THREAD", got


def test_detached_worker_without_local_session_still_inherits(monkeypatch):
    """A detached worker with no bound session_id keeps the inherited origin.

    The legitimate detached-worker case (requirement B) must survive the
    session_id guard: a dispatched worker's own session is foreign/unbound, so it
    has NO HERMES_SESSION_ID to compare against. With nothing to disagree with,
    the foreign-pid mirror it inherited is still accepted.
    """
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    foreign_pid = os.getpid() + 100000
    os.environ[_ORIGIN_ENV] = json.dumps({
        "platform": "discord", "chat_id": "WORKER_HUMAN", "thread_id": "WT",
        "user_id": "UW", "session_id": "SW",
        "_owner_pid": foreign_pid,
    })

    got = get_kanban_origin()
    assert got is not None, "detached worker lost its inherited origin"
    assert got["chat_id"] == "WORKER_HUMAN", got


def test_subprocess_rejects_foreign_mirror_with_null_session_id(monkeypatch):
    """A foreign mirror with a NULL session_id is rejected by a per-turn reader.

    The exact live 2026-11-15 repro shape: the gateway parent's process-global
    mirror carried ``session_id=null`` (a surface captured with no session
    handle) while the tool subprocess's own turn had HERMES_SESSION_ID bound. A
    null session_id cannot be attributed to this turn, so a per-turn reader must
    NOT accept it — otherwise the card routes to the parent/Home channel instead
    of the live thread.
    """
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    monkeypatch.setenv("HERMES_SESSION_ID", "20261031_155357_ddba09e6")
    foreign_pid = os.getpid() + 100000
    os.environ[_ORIGIN_ENV] = json.dumps({
        "platform": "discord", "chat_id": "1515879019269197885", "thread_id": None,
        "user_id": None, "session_id": None,
        "_owner_pid": foreign_pid,
    })

    got = get_kanban_origin()
    assert got is None, (
        "per-turn subprocess accepted a null-session_id foreign mirror (the live "
        f"wrong-thread repro): got {got!r}, expected None"
    )


def test_detached_worker_accepts_null_session_id_mirror(monkeypatch):
    """A detached worker (no bound session) still inherits a null-session_id seed.

    The dispatcher seeds a worker's HERMES_KANBAN_ORIGIN from the card's
    notify-sub, which carries ``session_id=None`` by construction
    (kanban_db.worker_origin_env). The worker binds no HERMES_SESSION_ID, so the
    null-session_id guard must NOT fire — the seed is the worker's legitimate
    origin and must be returned.
    """
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    foreign_pid = os.getpid() + 100000
    os.environ[_ORIGIN_ENV] = json.dumps({
        "platform": "discord", "chat_id": "WORKER_HUMAN", "thread_id": "WT",
        "user_id": None, "session_id": None,
        "_owner_pid": foreign_pid,
    })

    got = get_kanban_origin()
    assert got is not None, "detached worker lost its null-session_id seed origin"
    assert got["chat_id"] == "WORKER_HUMAN", got


def test_capture_ignores_poisoned_mirror_and_snapshots_live_session():
    """The card-filing seam resolves the LIVE session, not the sibling's mirror.

    This is the wrong-thread card-filing bug end to end: a sibling turn poisoned
    the process-global mirror with its own thread; THIS turn has bound its own
    live session and its origin ContextVar is _UNSET in the race window. The
    capture used to stamp cards (``capture_kanban_origin_from_session``) must
    NOT return the sibling's origin — it must fall through to ROOT-capture this
    turn's own live session.
    """
    # A concurrent sibling (this same process) poisoned the mirror.
    set_kanban_origin(
        platform="discord", chat_id="SIBLING_THREAD", thread_id="SIBLING_THREAD",
    )
    # THIS turn's handler entry drops the inherited ContextVar to _UNSET.
    reset_kanban_origin()
    # THIS turn binds its own live session.
    tokens = set_session_vars(
        platform="discord", chat_id="MY_LIVE_THREAD", thread_id="MY_LIVE_THREAD",
        user_id="ME", session_id="MY_SESSION",
    )
    try:
        captured = capture_kanban_origin_from_session()
    finally:
        clear_session_vars(tokens)
    assert captured is not None
    assert captured["chat_id"] == "MY_LIVE_THREAD", (
        f"card filing inherited a poisoned sibling origin: {captured!r}"
    )
    assert captured["thread_id"] == "MY_LIVE_THREAD", captured
