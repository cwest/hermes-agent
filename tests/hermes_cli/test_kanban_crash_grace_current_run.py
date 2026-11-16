"""The crash sweep's launch-window grace is measured per spawn.

``tasks.started_at`` is pinned to a card's first-ever start (``COALESCE`` on
every claim). The grace in ``_reclaim_dead_workers`` protects a freshly spawned
worker until its PID is visible, so it must be measured from the active
``task_runs.started_at``. Measured from the pinned task start, a card re-claimed
for its next lane inherits an expired grace and its new worker is reaped as
crashed before it finishes initialising.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "300")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kb.init_db()
    return home


def _reclaim_for_next_lane(conn, tid: str, host: str, *, first_start: int) -> None:
    kb.claim_task(conn, tid, claimer=f"{host}:first")
    conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (first_start, tid))
    conn.execute("UPDATE task_runs SET started_at = ? WHERE task_id = ?", (first_start, tid))
    # The first worker exits without a terminal verb; the card goes back to ready.
    conn.execute(
        "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
        "worker_pid=NULL WHERE id=?",
        (tid,),
    )
    conn.commit()
    kb.claim_task(conn, tid, claimer=f"{host}:second")
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (99999, tid))
    conn.commit()


def test_reclaimed_worker_gets_grace_from_its_own_run_start(kanban_home):
    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="reclaim grace", assignee="a")
        _reclaim_for_next_lane(conn, tid, host, first_start=int(time.time()) - 3600)

        assert tid not in kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).status == "running"


def test_worker_past_grace_on_its_own_run_is_still_reaped(kanban_home):
    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="old run", assignee="a")
        _reclaim_for_next_lane(conn, tid, host, first_start=int(time.time()) - 3600)
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (int(time.time()) - 3600, tid),
        )
        conn.commit()

        assert tid in kbd.detect_crashed_workers(conn)
