"""A worker signalling `dependency_wait` for REVIEW must reach the reviewer.

Root cause: `block_task(kind="dependency")` parks the card in `todo`. The
`recompute_ready` sweep then promotes any parentless `todo` to `ready`, and the
dispatcher respawns the *author* on a `ready` card. A worker that finished its
lane and asked to hand off therefore gets handed back to itself, forever.

Observed on t_b6a0a903: dependency_wait -> promoted -> claimed -> spawned,
three consecutive times, each respawning easley on already-complete work.

The fix: a dependency wait whose reason names a review/handoff must land in the
review lane, not back in the author's queue.
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


def _running_card(c, title):
    task_id = kanban_db.create_task(c, title=title, assignee="easley")
    c.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
    c.commit()
    return task_id


def test_review_handoff_does_not_bounce_back_to_author(conn):
    """The regression: rework-complete + awaiting-review must not re-promote."""
    task_id = _running_card(conn, "rework then hand off")

    kanban_db.block_task(
        conn, task_id,
        reason="Rework complete and committed (9b3eb63); awaiting re-review",
        kind="dependency",
    )
    conn.commit()

    kanban_db.recompute_ready(conn)
    conn.commit()

    assert _status(conn, task_id) != "ready", (
        "a card awaiting re-review must not be promoted back into the "
        "author's ready queue — that is the respawn loop"
    )


def test_genuine_dependency_wait_still_promotes(conn):
    """A real parent-dependency wait must still resume normally."""
    task_id = _running_card(conn, "waiting on a real parent")

    kanban_db.block_task(
        conn, task_id, reason="waiting on parent build", kind="dependency",
    )
    conn.commit()

    kanban_db.recompute_ready(conn)
    conn.commit()

    assert _status(conn, task_id) == "ready"
