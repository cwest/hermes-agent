"""Owner-unavailable worker exits: requeued without spending the card's retry budget.

A managed Kanban worker books ``KANBAN_OWNER_UNAVAILABLE_EXIT_CODE`` when its profile owner (the
gateway) stopped answering its durable writes: a ``worker.persist`` receipt timed out, or no
owner endpoint answered. The card did not fail, its host did. One slow gateway turn fails every
managed worker in flight at once, so booked as ordinary crashes the identical errors read as a
systemic failure and every card on the board gave up together.

These runs are requeued without touching ``consecutive_failures``. They have their own bounded
streak (``OWNER_UNAVAILABLE_RETRY_LIMIT``), so an owner that stays down still parks the card for
an operator. Any other closed run breaks the streak, and ``rate_limited`` runs are neutral.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

# Consecutive owner-unavailable runs before the card is parked: enough to ride out a stalled
# gateway turn, few enough that an owner that never recovers still reaches an operator.
OWNER_UNAVAILABLE_RETRY_LIMIT = 3

# Closed runs walked when counting a streak; streaks trip at a handful anyway.
STREAK_SCAN_LIMIT = 50


def trailing_run_streak(conn: sqlite3.Connection, task_id: str, is_member: Callable[[sqlite3.Row], bool]) -> int:
    """Count the task's newest closed runs that satisfy ``is_member``, skipping ``rate_limited``."""
    streak = 0
    rows = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY id DESC LIMIT ?",
        (task_id, STREAK_SCAN_LIMIT),
    ).fetchall()
    for row in rows:
        if (row["outcome"] or "") == "rate_limited":
            continue
        if not is_member(row):
            break
        streak += 1
    return streak


def owner_unavailable_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """The task's trailing streak of owner-unavailable runs (the run-metadata marker)."""
    from hermes_cli import kanban_db as kb
    return trailing_run_streak(conn, task_id, lambda row: bool(kb._json_dict(row["metadata"]).get("owner_unavailable")))


def account_owner_unavailable(conn: sqlite3.Connection, task_id: str, pid: int, claimer, dead) -> bool:
    """Book one owner-unavailable run; True when it parked the card.

    Below the streak limit the reaper has already requeued the card with its error stamped, and
    nothing is charged. At the limit the card is parked sticky: the counter cannot judge an outage
    it never saw, so ``recompute_ready`` holds it for an operator.
    """
    streak = owner_unavailable_streak(conn, task_id)
    if streak < OWNER_UNAVAILABLE_RETRY_LIMIT:
        return False
    from hermes_cli.kanban_db_dispatch import _record_task_failure
    return _record_task_failure(
        conn, task_id, error=dead.error_text, outcome="crashed",
        failure_limit=OWNER_UNAVAILABLE_RETRY_LIMIT, force_trip=True,
        event_payload_extra={"pid": pid, "claimer": claimer, "owner_unavailable": True,
                             "owner_unavailable_runs": streak},
    )
