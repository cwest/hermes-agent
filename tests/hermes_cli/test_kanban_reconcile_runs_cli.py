"""CLI-layer tests for ``hermes kanban reconcile-runs [--apply]``.

The reconcile itself (:func:`kanban_db.reconcile_dead_running_runs`) is proven by
``test_kanban_db.py::test_reconcile_dead_running_runs_dry_run_then_apply``. THIS
file pins the thin CLI shim around it:

* the default (no ``--apply``) is a READ-ONLY dry run that prints the count of
  dead orphan runs and writes nothing;
* ``--apply`` closes the dead runs and prints the closed count;
* the verb is idempotent — a second ``--apply`` closes zero.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _run(capsys, *, apply: bool) -> tuple[int, str, str]:
    """Invoke the CLI handler directly and return (exit_code, stdout, stderr)."""
    args = argparse.Namespace(apply=apply, board=None)
    rc = kc._cmd_reconcile_runs(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _seed_dead_run(conn) -> str:
    """Create a card with a run stuck status=running on a dead pid."""
    tid = kb.create_task(conn, title="stuck", assignee="a", detached=True)
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w")
    kb._set_worker_pid(conn, tid, 999999)  # a dead pid
    return tid


def test_reconcile_runs_dry_run_reports_without_writing(kanban_home, capsys):
    with kb.connect() as conn:
        _seed_dead_run(conn)

    rc, out, err = _run(capsys, apply=False)
    assert rc == 0
    assert "dry run" in out
    assert "1 dead run(s) would be closed" in out
    assert "Pass --apply" in out

    # Nothing written: the dead run is still open.
    with kb.connect() as conn:
        open_dead = conn.execute(
            "SELECT COUNT(*) AS n FROM task_runs "
            "WHERE status = 'running' AND ended_at IS NULL"
        ).fetchone()["n"]
    assert open_dead == 1


def test_reconcile_runs_apply_closes_dead_runs(kanban_home, capsys):
    with kb.connect() as conn:
        _seed_dead_run(conn)

    rc, out, err = _run(capsys, apply=True)
    assert rc == 0
    assert "closed 1 dead run(s)" in out

    with kb.connect() as conn:
        open_dead = conn.execute(
            "SELECT COUNT(*) AS n FROM task_runs "
            "WHERE status = 'running' AND ended_at IS NULL"
        ).fetchone()["n"]
    assert open_dead == 0


def test_reconcile_runs_apply_is_idempotent(kanban_home, capsys):
    with kb.connect() as conn:
        _seed_dead_run(conn)

    first_rc, first_out, _ = _run(capsys, apply=True)
    assert first_rc == 0
    assert "closed 1 dead run(s)" in first_out

    second_rc, second_out, _ = _run(capsys, apply=True)
    assert second_rc == 0
    assert "closed 0 dead run(s)" in second_out


def test_reconcile_runs_keeps_genuinely_live_run(kanban_home, capsys):
    """A card whose current run has a LIVE pid is never closed."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="live", assignee="a", detached=True)
        host = kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, tid, claimer=f"{host}:w")
        kb._set_worker_pid(conn, tid, os.getpid())  # this process — alive

    rc, out, err = _run(capsys, apply=True)
    assert rc == 0
    assert "closed 0 dead run(s)" in out
    assert "kept 1 live" in out

    with kb.connect() as conn:
        still_open = conn.execute(
            "SELECT ended_at FROM task_runs WHERE task_id = ?", (tid,)
        ).fetchone()["ended_at"]
    assert still_open is None
