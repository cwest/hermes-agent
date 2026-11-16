"""Dead-worker reaper: the in-transaction half of ``detect_crashed_workers``.

Split out of ``hermes_cli.kanban_db_dispatch``. Dispatcher helpers are reached
late-bound through ``_kbd`` so monkeypatching ``kanban_db_dispatch.<name>``
(``_worker_alive``, ``_classify_dead_worker``) keeps working; board helpers
through ``_kb`` likewise.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Optional


@dataclass
class _CrashSweep:
    """Everything ``detect_crashed_workers`` collects inside its reclaim txn."""

    crashed: list[str] = field(default_factory=list)
    rate_limited: list[str] = field(default_factory=list)
    # ``(task_id, pid, claimer, dead_worker)``: accounted after the txn via
    # ``_record_task_failure`` (needs its own write_txn).
    crash_details: list[tuple[str, int, str, Any]] = field(default_factory=list)
    # Worker-exit observer payloads, fired only after every reclaim/accounting
    # txn has committed.
    exited_hook_payloads: list[dict] = field(default_factory=list)


def _reclaim_dead_workers(conn: sqlite3.Connection, board: Optional[str] = None) -> _CrashSweep:
    """Release every host-local ``running`` task whose worker PID is dead."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd
    from hermes_cli.kanban_owner_recovery import apply_attempt_note, managed_run_exit_code, owner_reclaim_blocked

    sweep = _CrashSweep()
    with _kb.write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, worker_started_at, claim_lock, started_at, assignee "
            "FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = _kb._host_prefix()
        for row in rows:
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            # Launch-window grace so a freshly-spawned worker isn't reclaimed
            # before its PID is visible on /proc.
            started_at = _kb._to_epoch(_kb._row_get(row, "started_at"))
            if started_at is not None and time.time() - started_at < _kb._resolve_crash_grace_seconds():
                continue
            if _kbd._worker_alive(row["worker_pid"], _kb._row_get(row, "worker_started_at")):
                continue
            # An ambiguous receipt holds the card while the attempt may still run; once nothing
            # can be executing it (a dead bound interpreter, or a run that never bound one): crash.
            if owner_reclaim_blocked(conn, row["id"], crash_sweep=True):
                continue

            pid = int(row["worker_pid"])
            # A managed run's authority-recorded exit (its bound interpreter's, under this claim).
            exit_code = managed_run_exit_code(conn, row["id"], pid, row["claim_lock"])
            dead = _kbd._classify_dead_worker(pid, row["claim_lock"], exit_code, task_id=row["id"], board=board)
            apply_attempt_note(conn, row["id"], pid, dead)
            retry_status = _kb._retry_status_for_run(conn, row["id"])
            dead.event_payload["retry_status"] = retry_status
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, row["id"], pid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue
            run_id = _kb._end_run(
                conn, row["id"],
                outcome=dead.run_outcome, status=dead.run_outcome,
                error=dead.error_text,
                metadata=dict(dead.event_payload),
            )
            _kb._append_event(conn, row["id"], dead.event_kind, dead.event_payload, run_id=run_id)
            sweep.exited_hook_payloads.append({
                "task_id": row["id"],
                "assignee": row["assignee"],
                "run_id": run_id,
                "worker_pid": pid,
                "exit_kind": dead.kind,
                "exit_code": dead.code,
                "outcome": dead.run_outcome,
                "retry_status": retry_status,
            })
            if dead.rate_limited or dead.protocol_violation:
                # Stamp last_failure_error WITHOUT touching ``consecutive_failures``:
                # a rate-limited requeue must show ``check_respawn_guard`` a quota
                # blocker; a below-budget protocol violation never reaches
                # ``_record_task_failure`` (which stamps this column), yet the
                # board UI and retry worker need the corrective message.
                conn.execute(
                    "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                    (dead.error_text[:500], row["id"]),
                )
            if dead.rate_limited:
                sweep.rate_limited.append(row["id"])
            else:
                sweep.crashed.append(row["id"])
                sweep.crash_details.append((row["id"], pid, row["claim_lock"], dead))
    return sweep
