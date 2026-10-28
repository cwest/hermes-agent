"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue.
Interrupted attempts become ``unknown`` only after their exact owner process is
proved gone. Terminal states are immutable.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

logger = logging.getLogger("cron.executions")

EXECUTIONS_FILE = get_hermes_home().resolve() / "cron" / "executions.db"
MAX_TERMINAL_EXECUTIONS = 1000
_TERMINAL_STATES = ("completed", "failed", "unknown")
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex


def _connect() -> sqlite3.Connection:
    EXECUTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(EXECUTIONS_FILE, timeout=5)


def _apply_delete_journal_mode(conn: sqlite3.Connection) -> None:
    """Pin the execution ledger to DELETE (rollback-journal) mode — never WAL.

    Root cause of the recurring ``executions.db`` corruption: every quarantined
    corrupt copy was WAL-mode (on-disk header bytes 18/19 == 2) while the only
    never-corrupted file is DELETE-mode (bytes == 1). The torn-WAL / WAL-reset
    b-tree corruption class (``Rowid out of order``, ``2nd reference to page``,
    ``wrong # of entries in index``) only reaches a WAL database, and the
    ``~/.hermes`` backup excludes the ``.db-wal`` sidecar — so a stale main-file
    restore over a live WAL tears the page tree. The partial WAL-reset-bug fix
    (#69784) only *refuses* WAL on fresh DBs for vulnerable SQLite builds and
    deliberately leaves an existing WAL DB in WAL ("no live downgrade under
    concurrent openers"), so a legacy WAL ledger kept corrupting until it
    happened to be recreated in DELETE mode.

    This ledger is a tiny, ``synchronous=FULL``, single-writer-dominant audit
    store opened via short-lived connections under ``_lock`` — it gains nothing
    from WAL. Pinning DELETE unconditionally retires the entire WAL-corruption
    class for this file AND one-time-downgrades any legacy WAL ledger on open
    (safe here precisely because connections are short-lived and serialized,
    unlike the shared state.db/kanban.db the generic helper must protect).
    """
    conn.execute("PRAGMA journal_mode=DELETE")


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    _apply_delete_journal_mode(conn)
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, always close.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back
    the transaction; it does not close the connection. Relying on that alone
    leaks a connection (and its WAL/SHM file descriptors) on every call,
    since closing then depends on the garbage collector. Schema init runs
    inside the ``try`` too, so a PRAGMA/DDL failure after a successful
    ``connect()`` still closes the connection instead of leaking it.
    """
    with _lock:
        conn = _connect()
        try:
            _initialize_schema(conn)
            with conn:
                yield conn
        finally:
            conn.close()


def _record(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_is_live(pid: int, started_at: Optional[int]) -> bool:
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return False
    except Exception:
        return True  # fail safe: inability to prove death must not rewrite state
    if started_at is None:
        return pid == os.getpid()
    current = _process_start_time(pid)
    return current is not None and current == started_at


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    limit = max(0, int(MAX_TERMINAL_EXECUTIONS))
    conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT id FROM executions
             WHERE status IN ('completed','failed','unknown')
             ORDER BY claimed_at DESC, id DESC LIMIT -1 OFFSET ?
           )""",
        (limit,),
    )


def create_execution(job_id: str, *, source: str) -> Dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    with _transaction() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at)
               VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?)""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             _process_start_time(pid), now),
        )
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone()
    return _record(row)  # type: ignore[return-value]


def mark_execution_running(execution_id: str) -> Optional[Dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET status='running', started_at=?
               WHERE id=? AND status='claimed'""",
            (now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        return _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten."""
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET status=?, finished_at=?, error=?
               WHERE id=? AND status IN ('claimed','running')""",
            (status, now, detail, execution_id),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        return _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())


def recover_interrupted_executions() -> int:
    """Mark provably abandoned attempts unknown without scheduling retries."""
    now = _hermes_now().isoformat()
    changed = 0
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, process_id, pid, process_started_at FROM executions
               WHERE status IN ('claimed','running')"""
        ).fetchall()
        for row in rows:
            if row["process_id"] == _PROCESS_ID:
                continue
            if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                continue
            cur = conn.execute(
                """UPDATE executions SET status='unknown', finished_at=?, error=?
                   WHERE id=? AND status IN ('claimed','running')""",
                (now,
                 "Scheduler restarted after this execution's owner exited before a durable "
                 "terminal state; whether side effects ran is unknown.",
                 row["id"]),
            )
            changed += cur.rowcount
        if changed:
            _prune_unlocked(conn)
    return changed


def _quarantine_corrupt_store() -> Optional[Path]:
    """Rename a corrupt ``executions.db`` (plus any WAL/SHM/journal sidecars)
    aside as ``executions.db.corrupt-<ts>`` so the next open recreates a fresh,
    empty schema. Returns the quarantined main-file path, or None if there was
    nothing to move. Best-effort: never raises into the caller."""
    import time

    if not EXECUTIONS_FILE.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    target = EXECUTIONS_FILE.with_name(f"{EXECUTIONS_FILE.name}.corrupt-{stamp}")
    # Avoid clobbering an existing quarantine from the same second.
    suffix = 1
    while target.exists():
        target = EXECUTIONS_FILE.with_name(
            f"{EXECUTIONS_FILE.name}.corrupt-{stamp}-{suffix}"
        )
        suffix += 1
    try:
        EXECUTIONS_FILE.rename(target)
    except OSError as exc:
        logger.error("Could not quarantine corrupt executions.db: %s", exc)
        return None
    # SQLite names sidecars <dbfile>-wal / -shm / -journal. Move them too so a
    # stale sidecar can't poison the freshly recreated store.
    for ext in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(EXECUTIONS_FILE) + ext)
        if sidecar.exists():
            try:
                sidecar.rename(Path(str(target) + ext))
            except OSError:
                pass
    return target


def recover_interrupted_executions_resilient() -> Dict[str, Any]:
    """Scheduler-lifecycle recovery that SURVIVES a corrupt execution store.

    The raw :func:`recover_interrupted_executions` keeps its fail-closed
    contract (it raises ``sqlite3.DatabaseError`` on a corrupt store and never
    overwrites the file). But the scheduler thread must not die when that
    happens: a corrupt ``executions.db`` at gateway startup used to propagate
    out of ``provider.start()``, killing the daemon thread silently and
    freezing every scheduled job while the gateway looked healthy (the ~20-day
    freeze this fix targets).

    This wrapper catches that corruption, LOUDLY logs it, quarantines the bad
    file as ``executions.db.corrupt-<ts>``, recreates an empty schema, and
    returns so the scheduler keeps running. Losing run-history audit must never
    stop every job.

    Returns a dict: ``{"recovered": int, "quarantined": bool,
    "quarantined_path": Optional[str]}``.
    """
    try:
        recovered = recover_interrupted_executions()
        return {"recovered": recovered, "quarantined": False, "quarantined_path": None}
    except sqlite3.DatabaseError as exc:
        logger.error(
            "CRON SCHEDULER: execution ledger at %s is CORRUPT (%s). "
            "Quarantining it and recreating an empty store so scheduling "
            "continues — run history before now is lost. This is the recurring "
            "executions.db corruption; the store is now pinned to DELETE "
            "journal mode to retire the WAL-corruption class.",
            EXECUTIONS_FILE, exc,
        )
        quarantined = _quarantine_corrupt_store()
        # Recreate a fresh, usable schema (DELETE mode) so later writes succeed.
        try:
            with _transaction():
                pass
        except sqlite3.DatabaseError as exc2:
            logger.error(
                "CRON SCHEDULER: failed to recreate execution ledger after "
                "quarantine: %s. Scheduling continues; the ledger stays empty.",
                exc2,
            )
        return {
            "recovered": 0,
            "quarantined": quarantined is not None,
            "quarantined_path": str(quarantined) if quarantined else None,
        }


def list_executions(
    *, job_id: Optional[str] = None, limit: int = 50,
    before_claimed_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination."""
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        clauses.append("claimed_at < ?")
        params.append(str(before_claimed_at))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions" + where
            + " ORDER BY claimed_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def latest_execution(job_id: str) -> Optional[Dict[str, Any]]:
    rows = list_executions(job_id=job_id, limit=1)
    return rows[0] if rows else None


def latest_executions(job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load latest execution for many jobs in one indexed query."""
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e
                WHERE e.job_id IN ({placeholders})
                  AND e.id=(SELECT e2.id FROM executions e2
                            WHERE e2.job_id=e.job_id
                            ORDER BY e2.claimed_at DESC, e2.id DESC LIMIT 1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}
