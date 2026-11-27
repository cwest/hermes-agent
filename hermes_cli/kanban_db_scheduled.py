"""The ``scheduled`` lane's due time: park a card with an optional wake time and
let the dispatcher tick release it once that time has passed.

``scheduled_until`` (epoch seconds, NULL = no due time) comes from the explicit
``until`` argument or, for back-compat with workers that wrote it into free text,
a leading ``SCHEDULED_UNTIL=<iso8601>`` in the reason. A card with no due time
stays parked until someone unblocks it. ``kanban_db`` is reached late-bound
(import-cycle breaking).
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone, UTC
from typing import Any, Optional

_REASON_PREFIX_RE = re.compile(r"^\s*SCHEDULED_UNTIL=(\S+)")


def parse_due_time(value: float | str | None) -> Optional[int]:
    """Epoch seconds from an epoch number or an ISO-8601 string; a naive
    timestamp is read as UTC so the result never depends on the host zone.
    None for empty or unparseable input."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = value.strip()
    if not text:
        return None
    if text.lstrip("-").isdigit():
        return int(text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def due_time_from_reason(reason: Optional[str]) -> Optional[int]:
    """The leading ``SCHEDULED_UNTIL=<iso8601>`` marker of a schedule reason."""
    match = _REASON_PREFIX_RE.match(reason or "")
    return parse_due_time(match.group(1)) if match else None


def schedule_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    expected_run_id: Optional[int] = None, until: float | str | None = None,
) -> bool:
    """Park in ``scheduled`` (waiting on time, not a human; not dispatchable)
    until its due time passes or ``unblock_task`` re-gates it. An explicit
    ``until`` wins over the reason's ``SCHEDULED_UNTIL=`` marker; with neither
    the card has no due time."""
    from hermes_cli import kanban_db as _kb

    due = parse_due_time(until) if until is not None else due_time_from_reason(reason)
    with _kb.write_txn(conn):
        params: list[Any] = [due, task_id]
        sql = """
            UPDATE tasks
               SET status          = 'scheduled',
                   scheduled_until = ?,
                   claim_lock      = NULL,
                   claim_expires   = NULL,
                   worker_pid      = NULL
             WHERE id = ?
               AND status IN ('todo', 'ready', 'running', 'blocked')
        """
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return False
        run_id = _kb._end_or_synthesize_run(
            conn, task_id, outcome="scheduled", status="scheduled", summary=reason, synthesize=bool(reason),
        )
        _kb._append_event(conn, task_id, "scheduled", {"reason": reason}, run_id=run_id)
        return True


def wake_due_scheduled(conn: sqlite3.Connection, now: Optional[int] = None) -> int:
    """Release every ``scheduled`` card whose due time has passed through
    ``unblock_task`` (parent re-gate + ``unblocked`` event); returns the count.
    Call OUTSIDE any write transaction: each release opens its own."""
    from hermes_cli import kanban_db as _kb

    cutoff = int(time.time()) if now is None else int(now)
    due = [row[0] for row in conn.execute(
        "SELECT id FROM tasks WHERE status = 'scheduled' AND scheduled_until IS NOT NULL "
        "AND scheduled_until <= ? ORDER BY scheduled_until, id", (cutoff,),
    )]
    return sum(1 for task_id in due if _kb.unblock_task(conn, task_id))
