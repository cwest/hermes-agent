"""A worker its own gateway stopped (restart or shutdown) is requeued, not charged to the card.

Regression for the restart that parked four healthy cards at once: the gateway restarted, every
in-flight managed worker exited 130, and the owner settled each admission ``interrupted`` with the
stop cause it sent (``Gateway restarting``). The crash sweep booked them as ordinary crashes; four
identical errors in one sweep read as systemic, so each card tripped the breaker at a limit of one
(``gave_up {"failures": 1, "effective_limit": 1}``) on its first attempt.

The discriminator is the owner's own record that it was stopping, never the exit code: a worker that
exits 130 because a user pressed Stop, or with no owner at all, still counts.

The same sweep also ignored ``kanban.failure_limit``: crash and timeout accounting always used
``DEFAULT_FAILURE_LIMIT`` whatever the dispatcher was configured with.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_session, settle_claim

RESTART = "Gateway restarting"
SHUTDOWN = "Gateway shutting down"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    store = open_persistence(profile_scope(home=owner_root), eager=True)
    with kbc.connect() as conn:
        yield conn, store, owner_root / "state.db"
    store.close()


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _owner_settles(store, session_id: str, request_id: str, result: dict | None) -> None:
    """The owner admits the card's turn, starts it, and settles it ``interrupted`` with ``result``."""
    seed_session(store, session_id, "cli")
    epoch = store.runtime.begin_epoch(instance_id=f"owner:{request_id}")
    store.admissions.admit(epoch=epoch, principal_id="uid:501", session_id=session_id,
                           request_id=request_id, payload={"text": request_id})
    claimed = store.admissions.claim(epoch=epoch, session_id=session_id)
    settle_claim(store, claimed, "interrupted", result=result)


def _managed_run_killed(conn, store, ledger, tid: str, exit_code: int, *, cause: str | None,
                        receipt: bool = True, bound: bool = True) -> int:
    """Claim ``tid`` and leave a dead managed run: its interpreter exited ``exit_code`` and the owner
    settled the admission ``interrupted`` with ``cause`` as its recorded stop reason."""
    host = kb._claimer_id().split(":", 1)[0]
    task = kb.claim_task(conn, tid, claimer=f"{host}:owner")
    assert task is not None
    run = task.current_run_id
    interpreter = _dead_pid()
    kbd._set_worker_pid(conn, tid, _dead_pid())
    session_id, request_id = f"sess-{tid}-{run}", f'kanban:["default","{tid}",{run}]'
    with kb.write_txn(conn):
        if receipt:
            kb._append_event(conn, tid, "owner_admitted", {"db": str(ledger), "session_id": session_id,
                             "request_id": request_id, "pid": _dead_pid(), "birth": 1.0}, run_id=run)
        if bound:
            kb._append_event(conn, tid, "worker_bound", {"pid": interpreter, "claim_lock": task.claim_lock,
                                                         "started_at": "x"}, run_id=run)
            kb._append_event(conn, tid, "worker_result", {"pid": interpreter, "claim_lock": task.claim_lock,
                             "exit_code": exit_code, "last_output": ""}, run_id=run)
    if receipt:
        # The envelope the owner's settlement stores (``session_results.prepare_result``).
        result = {"final_response": "", "interrupted": True}
        if cause:
            result["interrupt_message"] = cause
        _owner_settles(store, session_id, request_id, {"result": result, "usage": {}})
    return run


def _row(conn, tid):
    return tuple(conn.execute("SELECT status, consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone())


def _kinds(conn, tid):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]


def test_a_restart_that_kills_every_worker_requeues_them_all_uncharged(env):
    conn, store, ledger = env
    tids = [kb.create_task(conn, title=f"card {i}", assignee="w") for i in range(4)]
    # One card already carries a failure: the restart must not be the one that trips it.
    conn.execute("UPDATE tasks SET consecutive_failures=? WHERE id=?", (kbd.DEFAULT_FAILURE_LIMIT - 1, tids[0]))
    conn.commit()
    runs = [_managed_run_killed(conn, store, ledger, tid, 130, cause=RESTART) for tid in tids]

    kbd.detect_crashed_workers(conn)

    assert kbd.detect_crashed_workers._last_auto_blocked == []
    assert _row(conn, tids[0]) == ("ready", kbd.DEFAULT_FAILURE_LIMIT - 1)
    for tid, run_id in zip(tids, runs):
        assert _row(conn, tid)[0] == "ready"
        assert "gave_up" not in _kinds(conn, tid)
        meta = json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()[0])
        assert meta["owner_stopped"] == RESTART
    for tid in tids[1:]:
        assert _row(conn, tid) == ("ready", 0)


def test_a_shutdown_sigterm_is_requeued_like_a_restart(env):
    conn, store, ledger = env
    tid = kb.create_task(conn, title="shutdown", assignee="w")
    _managed_run_killed(conn, store, ledger, tid, 143, cause=SHUTDOWN)

    kbd.detect_crashed_workers(conn)

    assert _row(conn, tid) == ("ready", 0)


@pytest.mark.parametrize("shape", [
    dict(cause=None),                    # the owner settled it interrupted, but for a user's Stop
    dict(cause=None, receipt=False),     # no owner at all: a plain worker that exited 130 itself
], ids=["user-stop", "no-owner"])
def test_an_exit_130_without_an_owner_stop_still_counts(env, shape):
    conn, store, ledger = env
    tid = kb.create_task(conn, title="interrupted on its own", assignee="w")
    conn.execute("UPDATE tasks SET consecutive_failures=? WHERE id=?", (kbd.DEFAULT_FAILURE_LIMIT - 1, tid))
    conn.commit()
    _managed_run_killed(conn, store, ledger, tid, 130, **shape)

    kbd.detect_crashed_workers(conn)

    assert _row(conn, tid) == ("blocked", kbd.DEFAULT_FAILURE_LIMIT)
    assert "gave_up" in _kinds(conn, tid)


def test_a_restart_killed_attempt_that_never_bound_is_reclaimed_uncharged(env):
    """A worker the restart killed before it bound an interpreter is released by the TTL reclaim,
    which books through the same accounting."""
    conn, store, ledger = env
    tid = kb.create_task(conn, title="never bound", assignee="w")
    _managed_run_killed(conn, store, ledger, tid, 130, cause=RESTART, bound=False)
    conn.execute("UPDATE tasks SET claim_expires=1 WHERE id=?", (tid,))
    conn.commit()

    assert kb.release_stale_claims(conn) == 1

    assert _row(conn, tid) == ("ready", 0)
    reclaimed = json.loads(conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='reclaimed'", (tid,)).fetchone()[0])
    assert reclaimed["owner_stopped"] == RESTART


def test_crash_accounting_honours_the_configured_failure_limit(env):
    """``kanban.failure_limit`` reaches the crash sweep: a limit of 3 is not tripped at the default 2."""
    conn, store, ledger = env
    assert kbd.DEFAULT_FAILURE_LIMIT < 3
    tid = kb.create_task(conn, title="really failing", assignee="w")
    conn.execute("UPDATE tasks SET consecutive_failures=? WHERE id=?", (kbd.DEFAULT_FAILURE_LIMIT - 1, tid))
    conn.commit()
    _managed_run_killed(conn, store, ledger, tid, 1, cause=None, receipt=False)

    res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, dry_run=True, failure_limit=3)

    assert tid not in res.auto_blocked
    assert _row(conn, tid) == ("ready", kbd.DEFAULT_FAILURE_LIMIT)
    assert "gave_up" not in _kinds(conn, tid)
