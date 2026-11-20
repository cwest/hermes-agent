"""The ``recent_success`` respawn guard and which ``task_events`` count as a
deliberate re-queue that lifts it (plus the handoff predicate ``active_pr`` shares).

Split out of ``hermes_cli.kanban_db_dispatch``; ``_json_or`` and ``_rows_since``
are reached late-bound (import-cycle breaking).
"""

from __future__ import annotations

import sqlite3
from typing import Optional

REQUEUE_KINDS = (
    "status", "promoted", "promoted_manual", "unblocked", "reclaimed",
    "changes_requested", "review_reopened", "assigned", "status_changed",
)
REQUEUE_KINDS_SQL = ", ".join(f"'{k}'" for k in REQUEUE_KINDS)


def held_by_recent_success(conn: sqlite3.Connection, task_id: str, now: int, window: int) -> bool:
    """A completed run within ``window`` with no deliberate re-queue after it.
    A re-queue (done→ready drag, promotion, unblock, reclaim, a review bounce,
    a handoff to another profile) means "run it again"; without the exemption
    a rework bounce would sit silently held until the window elapses."""
    from hermes_cli.kanban_db_dispatch import _rows_since

    success = max((t for t, _ in _rows_since(
        conn, "task_runs", task_id, now - window, "AND outcome = 'completed'")), default=None)
    return success is not None and not any(is_requeue_event(e["kind"], e["payload"]) for _, e in _rows_since(
        conn, "task_events", task_id, success, f"AND kind IN ({REQUEUE_KINDS_SQL})", cols=", kind, payload"))


def is_requeue_event(kind: str, payload: Optional[str]) -> bool:
    """A deliberate move that lifts ``recent_success``. ``assigned`` counts only
    as a real handoff to another profile; ``status_changed`` (written by
    out-of-tree board tooling) only when it lands the card in ``ready``."""
    if kind == "assigned":
        return is_handoff_event(kind, payload)
    if kind == "status_changed":
        from hermes_cli.kanban_db import _json_or
        data = _json_or(payload, {})
        return isinstance(data, dict) and data.get("to") == "ready"
    return True


def is_handoff_event(kind: str, payload: Optional[str]) -> bool:
    """Only an ``assigned`` event that moves the card to a DIFFERENT profile is
    a handoff. A no-op re-assign (dev→dev via CLI/dashboard/``reassign
    --reclaim``), an unassign, or the dispatcher's own
    ``kanban.default_assignee`` write would otherwise lift ``active_pr`` for
    the very implementer that opened the PR. Events without ``from`` (written
    before it was recorded) are not trusted as handoffs — fail closed."""
    if kind != "assigned":
        return True
    from hermes_cli.kanban_db import _json_or
    data = _json_or(payload, {})
    if not isinstance(data, dict) or data.get("source") == "kanban.default_assignee":
        return False
    to = data.get("assignee")
    return bool(to) and "from" in data and data["from"] != to
