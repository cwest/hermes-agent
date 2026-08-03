"""An acceptance park must be sticky: accept_card counts, not just move_card."""
import json
import sqlite3
import pytest
from hermes_cli import kanban_db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    c = kanban_db.connect(tmp_path / "kanban.db")
    yield c
    c.close()


def _park(c, task_id, by):
    """Emit the acceptance park: status blocked + a status_changed event."""
    c.execute("UPDATE tasks SET status='blocked', assignee='casey' WHERE id=?", (task_id,))
    kanban_db._append_event(
        c, task_id, "status_changed", {"from": "review", "to": "blocked", "by": by}
    )
    c.commit()


def test_accept_card_park_is_sticky(conn):
    """accept_card is the real acceptance verb; its park must not auto-recover."""
    t = kanban_db.create_task(conn, title="accepted card")
    _park(conn, t, "onecard:accept_card")
    assert kanban_db._has_sticky_block(conn, t) is True


def test_move_card_park_still_sticky(conn):
    """Regression guard: the original move_card path keeps working."""
    t = kanban_db.create_task(conn, title="moved card")
    _park(conn, t, "onecard:move_card")
    assert kanban_db._has_sticky_block(conn, t) is True


def test_move_back_to_ready_clears_stickiness(conn):
    """A deliberate move out of blocked still clears it."""
    t = kanban_db.create_task(conn, title="unparked card")
    _park(conn, t, "onecard:accept_card")
    kanban_db._append_event(
        conn, t, "status_changed", {"to": "ready", "by": "onecard:move_card"}
    )
    conn.commit()
    assert kanban_db._has_sticky_block(conn, t) is False
