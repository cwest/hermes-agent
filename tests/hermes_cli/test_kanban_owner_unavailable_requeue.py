"""A managed run that died because its profile owner stopped answering is requeued, not charged.

Regression for the stall that gave up six cards at once: the gateway loop blocked past every
managed worker's ``worker.persist`` receipt, each worker failed its turn, and the sweep booked
three identical exit-1 crashes in one pass as systemic, so every card tripped at a limit of one.
The worker now records ``KANBAN_OWNER_UNAVAILABLE_EXIT_CODE`` for that cause. The sweep requeues
it without spending ``consecutive_failures``; only a streak of them parks the card, and a real
failure with no owner-side cause still gives up exactly as before.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_owner_unavailable import OWNER_UNAVAILABLE_RETRY_LIMIT as RETRY_LIMIT
from hermes_cli.turn_exit import KANBAN_OWNER_UNAVAILABLE_EXIT_CODE as OWNER_UNAVAILABLE


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _managed_run_died(conn, tid: str, exit_code: int) -> int:
    """Claim ``tid`` and leave a dead managed run whose authority recorded ``exit_code``."""
    host = kb._claimer_id().split(":", 1)[0]
    task = kb.claim_task(conn, tid, claimer=f"{host}:owner")
    assert task is not None
    interpreter = _dead_pid()
    kbd._set_worker_pid(conn, tid, _dead_pid())
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "worker_bound", {"pid": interpreter, "claim_lock": task.claim_lock,
                                                     "started_at": "x"}, run_id=task.current_run_id)
        kb._append_event(conn, tid, "worker_result", {"pid": interpreter, "claim_lock": task.claim_lock,
                         "exit_code": exit_code, "last_output": ""}, run_id=task.current_run_id)
        kb._append_event(conn, tid, "worker_lost", {"reason": "managed_worker_failed", "claim_lock": task.claim_lock,
                         "run_id": task.current_run_id, "pid": interpreter, "bound": True},
                         run_id=task.current_run_id)
    return task.current_run_id


def _row(conn, tid):
    return conn.execute("SELECT status, consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()


def _kinds(conn, tid):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]


def test_an_owner_stall_across_the_board_requeues_every_card_without_a_gave_up(conn):
    tids = [kb.create_task(conn, title=f"card {i}", assignee="w") for i in range(4)]
    runs = [_managed_run_died(conn, tid, OWNER_UNAVAILABLE) for tid in tids]

    kbd.detect_crashed_workers(conn)

    assert kbd.detect_crashed_workers._last_auto_blocked == []
    for tid, run_id in zip(tids, runs):
        assert tuple(_row(conn, tid)) == ("ready", 0)
        assert "gave_up" not in _kinds(conn, tid)
        meta = json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()[0])
        assert meta["owner_unavailable"] is True
    # The owner-side outcome is named, not folded into a bare crash, and never into the
    # systemic-crash fingerprint (four identical errors in one sweep would read as one).
    error = conn.execute("SELECT last_failure_error FROM tasks WHERE id=?", (tids[0],)).fetchone()[0]
    assert "owner-unavailable" in error


def test_a_real_nonzero_exit_with_no_owner_cause_still_gives_up_at_the_limit(conn):
    tid = kb.create_task(conn, title="really failing", assignee="w")
    conn.execute("UPDATE tasks SET consecutive_failures=? WHERE id=?", (kbd.DEFAULT_FAILURE_LIMIT - 1, tid))
    conn.commit()
    _managed_run_died(conn, tid, 1)

    kbd.detect_crashed_workers(conn)

    assert tuple(_row(conn, tid)) == ("blocked", kbd.DEFAULT_FAILURE_LIMIT)
    assert "gave_up" in _kinds(conn, tid)


def test_an_owner_that_stays_unavailable_parks_the_card_after_a_bounded_streak(conn):
    tid = kb.create_task(conn, title="owner keeps failing", assignee="w")
    for attempt in range(1, RETRY_LIMIT + 1):
        _managed_run_died(conn, tid, OWNER_UNAVAILABLE)
        kbd.detect_crashed_workers(conn)
        if attempt < RETRY_LIMIT:
            assert tuple(_row(conn, tid)) == ("ready", 0)
    assert _row(conn, tid)[0] == "blocked"
    gave_up = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()
    assert json.loads(gave_up[0])["sticky"] is True


def test_a_real_crash_between_owner_stalls_restarts_the_owner_streak(conn):
    tid = kb.create_task(conn, title="mixed", assignee="w")
    for _ in range(RETRY_LIMIT - 1):
        _managed_run_died(conn, tid, OWNER_UNAVAILABLE)
        kbd.detect_crashed_workers(conn)
    _managed_run_died(conn, tid, 1)
    kbd.detect_crashed_workers(conn)
    _managed_run_died(conn, tid, OWNER_UNAVAILABLE)
    kbd.detect_crashed_workers(conn)
    assert tuple(_row(conn, tid)) == ("ready", 1)
