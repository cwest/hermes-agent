"""A ``scheduled`` card with a due time wakes on the dispatcher tick that follows it."""

from __future__ import annotations

import time
from datetime import datetime, UTC
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_scheduled import parse_due_time


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _scheduled(conn, **kw) -> str:
    # Unassigned: the tick promotes it but has no profile to spawn.
    tid = kb.create_task(conn, title="waits on a background job", assignee=None)
    assert kb.schedule_task(conn, tid, **kw) is True
    return tid


def _kinds(conn, tid) -> list[str]:
    return [e.kind for e in kb.list_events(conn, tid)]


def test_past_due_time_is_promoted_on_next_tick_and_emits_unblocked(kanban_home):
    with kbc.connect() as conn:
        tid = _scheduled(conn, reason="replay running", until=int(time.time()) - 60)
        kbd.dispatch_once(conn)
        assert kb.get_task(conn, tid).status == "ready"
        last = kb.list_events(conn, tid)[-1]
        assert last.kind == "unblocked" and last.payload == {"source": "scheduled_until"}


def test_future_due_time_is_left_scheduled(kanban_home):
    with kbc.connect() as conn:
        tid = _scheduled(conn, reason="replay running", until=int(time.time()) + 3600)
        kbd.dispatch_once(conn)
        assert kb.get_task(conn, tid).status == "scheduled"
        assert "unblocked" not in _kinds(conn, tid)


def test_no_due_time_is_left_scheduled(kanban_home):
    with kbc.connect() as conn:
        tid = _scheduled(conn, reason="run next week")
        kbd.dispatch_once(conn)
        assert kb.get_task(conn, tid).status == "scheduled"
        assert kb.get_task(conn, tid).scheduled_until is None
        assert "unblocked" not in _kinds(conn, tid)


def test_scheduled_until_reason_prefix_sets_the_due_time(kanban_home):
    past, future = int(time.time()) - 60, int(time.time()) + 3600
    with kbc.connect() as conn:
        due = _scheduled(conn, reason=f"SCHEDULED_UNTIL={_iso(past)} — replay finishing")
        later = _scheduled(conn, reason=f"SCHEDULED_UNTIL={_iso(future)} — replay finishing")
        assert kb.get_task(conn, due).scheduled_until == past
        assert kb.get_task(conn, later).scheduled_until == future
        kbd.dispatch_once(conn)
        assert kb.get_task(conn, due).status == "ready"
        assert kb.get_task(conn, later).status == "scheduled"


@pytest.mark.parametrize("value", ["--123", "-", "nan", "inf", "-inf", float("nan"), float("inf")])
def test_malformed_due_time_parses_to_none(value):
    assert parse_due_time(value) is None


def test_unblock_clears_the_due_time(kanban_home):
    with kbc.connect() as conn:
        tid = _scheduled(conn, reason="replay running", until=int(time.time()) + 3600)
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).scheduled_until is None
