"""The heartbeat-stale window that lets ``release_stale_claims`` reclaim a live worker.

A worker whose claim TTL has expired keeps its claim while its PID is alive,
unless ``last_heartbeat_at`` is older than the stale window: then it is treated
as wedged and reclaimed even though the process still exists. A healthy worker
refreshes ``last_heartbeat_at`` through the activity bridge at most every 60 s
(``tools.kanban_tools._AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS``), so a worker that
has been silent for 14 minutes is wedged, not slow.
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
    kb.init_db()
    return home


@pytest.fixture
def live_worker():
    proc = subprocess.Popen(["sleep", "60"])
    yield proc.pid
    proc.kill()
    proc.wait()


def _expired_claim_with_heartbeat_age(conn, pid: int, heartbeat_age: int) -> str:
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="slow or wedged", assignee="a")
    kb.claim_task(conn, tid, claimer=f"{host}:w")
    kbd._set_worker_pid(conn, tid, pid)
    now = int(time.time())
    conn.execute(
        "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? WHERE id = ?",
        (now - 1, now - heartbeat_age, tid),
    )
    conn.commit()
    return tid


def test_live_worker_silent_for_fourteen_minutes_is_reclaimed(kanban_home, live_worker):
    with kbc.connect() as conn:
        tid = _expired_claim_with_heartbeat_age(conn, live_worker, 14 * 60)

        assert kb.release_stale_claims(conn) == 1

        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "claim_extended" not in kinds
        assert "reclaimed" in kinds
        task = kb.get_task(conn, tid)
        assert task is not None and (task.status, task.claim_lock) == ("ready", None)


def test_live_worker_with_a_fresh_heartbeat_keeps_its_claim(kanban_home, live_worker):
    with kbc.connect() as conn:
        tid = _expired_claim_with_heartbeat_age(conn, live_worker, 60)

        assert kb.release_stale_claims(conn) == 0

        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "running"
        assert "claim_extended" in [e.kind for e in kb.list_events(conn, tid)]
