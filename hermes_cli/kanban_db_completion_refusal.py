"""Why a ``complete_task`` call returned ``False``.

``complete_task`` refuses for several distinct reasons and returns a bare
``False`` for all of them; callers used to print one guess ("unknown id or
already terminal") that was wrong for most. Read the cause from the card
instead. Call this only on the ``False`` path.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

_COMPLETABLE = ("running", "ready", "blocked", "review")
# kanban_pr_acceptance_store.record_acceptance stamps a failed receipt with this.
# Any other last_failure_error is an earlier run's crash, not this refusal.
_ACCEPTANCE_PREFIX = "PR acceptance "


def explain_completion_refusal(
    conn: sqlite3.Connection, task_id: str, *, expected_run_id: Optional[int] = None,
) -> str:
    row = conn.execute(
        "SELECT status, current_run_id, last_failure_error FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None:
        return f"no task {task_id} on this board"
    status = row["status"]
    if status not in _COMPLETABLE:
        return (f"{task_id} is already {status!r}" if status in ("done", "archived")
                else f"{task_id} is in {status!r}; only running, ready, blocked or review cards complete")
    blockers = _kb.unsatisfied_parents(conn, task_id)
    if blockers:
        detail = ", ".join(f"{pid} ({state})" for pid, state in blockers)
        return f"{task_id} has unsatisfied parent dependencies: {detail}; complete the parents first"
    current = row["current_run_id"]
    # A reclaimed card waiting in ready has no current run at all; that is still superseded.
    if expected_run_id is not None and (current is None or int(current) != int(expected_run_id)):
        owner = f"run {current} does" if current is not None else "no run owns it until the next claim"
        return (f"{task_id} was reclaimed: your run {expected_run_id} no longer owns it, {owner}."
                " This completion is superseded; do not retry with the old run.")
    error = row["last_failure_error"] or ""
    if error.startswith(_ACCEPTANCE_PREFIX):  # written by record_acceptance on this refusal
        return f"{task_id} was refused: {error}"
    return f"{task_id} ({status!r}) changed while completing; re-read the card before retrying"


from hermes_cli import kanban_db as _kb
