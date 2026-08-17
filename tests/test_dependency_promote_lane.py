"""A dependency-cleared card must resume its OWN lane, not be forced to `ready`.

Root cause (kanban_db.recompute_ready): when a card blocked on a parent
dependency has that parent complete, the promote path unconditionally sets
status='ready'. For a card blocked while sitting in `review`, that discards the
review lane and hands it back to the author; the card re-enters review, blocks
again, and the cycle repeats. t_b6a0a903 wedged four times this way.

Fix: restore the lane the card was blocked FROM.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db


@pytest.fixture()
def conn(tmp_path):
    c = kanban_db.connect(tmp_path / "kanban.db")
    yield c
    c.close()


def _status(c, task_id):
    return c.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


def _blocked_child_in(c, lane, *, suffix):
    """Parent done, child sitting in `lane`, then blocked on the dependency."""
    parent = kanban_db.create_task(c, title=f"parent {suffix}")
    c.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
    child = kanban_db.create_task(c, title=f"child {suffix}", assignee="lamport")
    kanban_db.link_tasks(c, parent_id=parent, child_id=child)
    c.execute("UPDATE tasks SET status = ? WHERE id = ?", (lane, child))
    c.commit()
    kanban_db.block_task(c, child, reason="dependency_wait: parent")
    c.commit()
    return child


def test_card_blocked_from_review_returns_to_review(conn):
    """The regression: a review-lane card must NOT be dropped back to ready."""
    child = _blocked_child_in(conn, "review", suffix="a")

    kanban_db.recompute_ready(conn)
    conn.commit()

    assert _status(conn, child) == "review", (
        "a card blocked out of the review lane must resume review, not ready"
    )


def test_card_blocked_from_todo_still_promotes_to_ready(conn):
    """The normal path must be unchanged."""
    child = _blocked_child_in(conn, "todo", suffix="b")

    kanban_db.recompute_ready(conn)
    conn.commit()

    assert _status(conn, child) == "ready"
