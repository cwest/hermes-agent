"""The crash sweep clears a dead worker's claim on a card outside ``running``.

Lifecycle verbs clear the claim on every lane handoff, but an out-of-band move
(a raw ``UPDATE tasks SET status=...``) can leave a card in ``review`` (or any
other lane) still carrying the author's ``claim_lock`` and ``worker_pid``.
``claim_review_task`` requires ``claim_lock IS NULL`` and ``release_stale_claims``
scans ``running`` only, so the lane wedges. Once the worker is dead the claim is
cleared in place: no lane change, no failure booked.
"""

from __future__ import annotations

import subprocess
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
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    return home


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _raw_move_to_review(conn, *, claimer: str, pid: int) -> str:
    """A claimed card moved to ``review`` by a raw UPDATE that leaves the claim."""
    tid = kb.create_task(conn, title="raw move", assignee="author")
    kb.claim_task(conn, tid, claimer=claimer)
    kbd._set_worker_pid(conn, tid, pid)
    conn.execute(
        "UPDATE tasks SET status='review', assignee='reviewer', started_at=? WHERE id=?",
        (int(time.time()) - 3600, tid),
    )
    conn.execute(
        "UPDATE task_runs SET started_at=? WHERE task_id=?",
        (int(time.time()) - 3600, tid),
    )
    conn.commit()
    return tid


def test_dead_worker_claim_on_review_card_is_cleared_in_place(kanban_home):
    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = _raw_move_to_review(conn, claimer=f"{host}:dead", pid=_dead_pid())

        assert tid not in kbd.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task is not None
        assert (task.status, task.claim_lock, task.claim_expires, task.worker_pid) == (
            "review", None, None, None,
        )
        assert task.consecutive_failures == 0
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "stale_claim_cleared" in kinds
        assert "crashed" not in kinds
        assert kb.claim_review_task(conn, tid, claimer=f"{host}:reviewer") is not None


def test_live_worker_claim_on_review_card_is_kept(kanban_home):
    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        sleeper = subprocess.Popen(["sleep", "30"])
        try:
            tid = _raw_move_to_review(conn, claimer=f"{host}:live", pid=sleeper.pid)
            kbd.detect_crashed_workers(conn)
            task = kb.get_task(conn, tid)
            assert task is not None
            assert (task.status, task.claim_lock) == ("review", f"{host}:live")
        finally:
            sleeper.kill()
            sleeper.wait()


def test_foreign_host_claim_on_review_card_is_kept(kanban_home):
    with kbc.connect() as conn:
        tid = _raw_move_to_review(conn, claimer="some-other-host:w", pid=_dead_pid())
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.claim_lock == "some-other-host:w"
