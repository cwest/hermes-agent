"""E2E: a real tool subprocess must not resolve a FOREIGN turn's kanban origin.

The unit tests in ``test_kanban_origin_cross_turn_race.py`` exercise
``get_kanban_origin`` in-process. This test exercises the genuine spawn boundary
that the live bug fired across (reported 2026-11-15, Discord thread
1566178432407044287): the gateway writes the process-global
``HERMES_KANBAN_ORIGIN`` mirror last-writer-wins, then a terminal / execute_code
/ delegate tool subprocess is spawned by a turn whose session differs from the
mirror's. ContextVars do NOT cross a process fork, so inside the subprocess the
origin ContextVar is ``_UNSET`` and the only origin source is the inherited
mirror — whose owner pid is the (foreign) parent's, so the owner-pid guard alone
accepts it. The per-turn ``HERMES_SESSION_ID`` DOES cross (the subprocess-env
bridge stamps it), and it is the discriminator the fix relies on.

We build the child env with the real subprocess-env bridge
(``tools.environments.local._make_run_env``) from a bound turn-A context while a
turn-B mirror sits in ``os.environ``, then spawn an actual Python interpreter
with that env and assert the child's ``get_kanban_origin()`` returns ``None``
rather than turn B's surface. A companion case proves the same-turn subprocess
still resolves its own origin.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import gateway.session_context as sc
from gateway.session_context import _VAR_MAP, clear_session_vars, set_session_vars
from tools.environments.local import _make_run_env

_ORIGIN_ENV = "HERMES_KANBAN_ORIGIN"
_REPO_ROOT = Path(__file__).resolve().parents[2]

# The child program: import the SAME gateway.session_context from the repo, read
# its kanban origin, and print a JSON verdict. It runs in a fresh interpreter, so
# every ContextVar is _UNSET and the only origin source is the inherited env
# mirror — exactly the production tool-subprocess shape.
_CHILD_PROGRAM = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from gateway.session_context import get_kanban_origin\n"
    "print(json.dumps(get_kanban_origin()))\n"
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Temp HERMES_HOME + clean ContextVar / mirror / session slate per test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    saved_origin_env = os.environ.get(_ORIGIN_ENV)
    saved_origin_ctx = sc._KANBAN_ORIGIN.get()
    saved_ctx = {name: var.get() for name, var in _VAR_MAP.items()}
    saved_engaged = sc._session_context_engaged
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
        if saved_origin_env is None:
            os.environ.pop(_ORIGIN_ENV, None)
        else:
            os.environ[_ORIGIN_ENV] = saved_origin_env


def _spawn_child_origin(child_env: dict) -> dict | None:
    """Run the child program with *child_env* and return its parsed origin."""
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_PROGRAM, str(_REPO_ROOT)],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"child exited {proc.returncode}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    return json.loads(proc.stdout.strip())


def test_real_subprocess_rejects_foreign_turn_origin():
    """A spawned subprocess must NOT resolve a concurrent turn's mirror origin."""
    # Turn B wrote the process-global mirror last (its surface, its pid). We stamp
    # the current process's pid so this is indistinguishable from the real case
    # where the gateway parent wrote it for turn B — the child's pid differs from
    # it regardless, which is the whole point.
    os.environ[_ORIGIN_ENV] = json.dumps({
        "platform": "discord", "chat_id": "TURN_B_CHAT", "thread_id": "TURN_B_THREAD",
        "user_id": "UB", "session_id": "SESSION_B",
        "_owner_pid": os.getpid(),
    })
    # Turn A is the live context that spawns the subprocess. Its session id is
    # bridged into the child by the real subprocess-env builder.
    tokens = set_session_vars(
        platform="discord", chat_id="TURN_A_CHAT", thread_id="TURN_A_THREAD",
        user_id="UA", session_id="SESSION_A",
    )
    try:
        child_env = _make_run_env({})
    finally:
        clear_session_vars(tokens)

    # Sanity: the bridge stamped turn A's session id into the child env, and the
    # foreign-turn mirror rode along in os.environ (the leak vector).
    assert child_env.get("HERMES_SESSION_ID") == "SESSION_A"
    assert child_env.get(_ORIGIN_ENV), "mirror should ride into the child env"

    origin = _spawn_child_origin(child_env)
    assert origin is None, (
        "real tool subprocess resolved a FOREIGN turn's origin off the "
        f"inherited mirror: {origin!r} (expected None; its own turn is SESSION_A)"
    )


def test_real_subprocess_accepts_same_turn_origin():
    """A spawned subprocess whose own turn matches the mirror resolves it."""
    os.environ[_ORIGIN_ENV] = json.dumps({
        "platform": "discord", "chat_id": "TURN_A_CHAT", "thread_id": "TURN_A_THREAD",
        "user_id": "UA", "session_id": "SESSION_A",
        "_owner_pid": os.getpid(),
    })
    tokens = set_session_vars(
        platform="discord", chat_id="TURN_A_CHAT", thread_id="TURN_A_THREAD",
        user_id="UA", session_id="SESSION_A",
    )
    try:
        child_env = _make_run_env({})
    finally:
        clear_session_vars(tokens)

    assert child_env.get("HERMES_SESSION_ID") == "SESSION_A"

    origin = _spawn_child_origin(child_env)
    assert origin is not None, "same-turn subprocess lost its own origin"
    assert origin["chat_id"] == "TURN_A_CHAT", origin
    assert origin["thread_id"] == "TURN_A_THREAD", origin


def test_real_subprocess_rejects_null_session_id_parent_mirror():
    """The exact live repro across a real process boundary: null session_id.

    The gateway parent's process-global mirror carried ``session_id=null`` (a
    surface captured with no session handle — the Home channel), while the tool
    subprocess's own turn has HERMES_SESSION_ID bound. A null session_id cannot be
    attributed to this turn, so the spawned child must resolve ``None`` rather than
    the parent/Home surface.
    """
    os.environ[_ORIGIN_ENV] = json.dumps({
        "platform": "discord", "chat_id": "1515879019269197885", "thread_id": None,
        "user_id": None, "session_id": None,
        "_owner_pid": os.getpid(),
    })
    tokens = set_session_vars(
        platform="discord", chat_id="LIVE_THREAD_CHAT", thread_id="LIVE_THREAD",
        user_id="ME", session_id="20261031_155357_ddba09e6",
    )
    try:
        child_env = _make_run_env({})
    finally:
        clear_session_vars(tokens)

    assert child_env.get("HERMES_SESSION_ID") == "20261031_155357_ddba09e6"
    assert child_env.get(_ORIGIN_ENV), "null-session mirror should ride into the child"

    origin = _spawn_child_origin(child_env)
    assert origin is None, (
        "real tool subprocess accepted a null-session_id parent mirror (the live "
        f"wrong-thread repro): {origin!r} (expected None)"
    )
