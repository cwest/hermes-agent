"""Keep the cron scheduler usable through a corrupt ``cron/executions.db``.

``recover_interrupted_executions`` is fail-closed: on a corrupt ledger it raises and never touches
the file. The ticker survives that (#111010), but every later ``create_execution`` (the first step
of a fire claim) raised the same error, so no job could fire again until the file was deleted by
hand. This is the provider-lifecycle entry point instead. It moves the corrupt file (and its
SQLite sidecars) aside as ``executions.db.corrupt-<ts>``, recreates an empty ledger, and returns,
so losing run history never stops every job.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cron.executions")

_SIDECARS = ("-wal", "-shm", "-journal")


def _ledger_path() -> Path:
    from cron import executions
    from hermes_constants import get_hermes_home

    return executions.EXECUTIONS_FILE or (get_hermes_home().resolve() / "cron" / "executions.db")


def quarantine_corrupt_ledger() -> Optional[Path]:
    """Rename the ledger and its sidecars to ``<name>.corrupt-<ts>[-n]``; the quarantined main
    file, or None when there was nothing to move or the rename failed (logged)."""
    path = _ledger_path()
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target, n = path.with_name(f"{path.name}.corrupt-{stamp}"), 1
    while target.exists():
        target, n = path.with_name(f"{path.name}.corrupt-{stamp}-{n}"), n + 1
    try:
        path.rename(target)
    except OSError as exc:
        logger.error("Could not quarantine the corrupt cron execution ledger %s: %s", path, exc)
        return None
    for ext in _SIDECARS:
        sidecar = Path(f"{path}{ext}")
        if sidecar.exists():
            try:
                sidecar.rename(Path(f"{target}{ext}"))
            except OSError as exc:
                logger.error("Could not move %s aside with the corrupt ledger: %s", sidecar, exc)
    return target


def recover_interrupted_executions_resilient() -> int:
    """``recover_interrupted_executions``, except a corrupt ledger is quarantined and recreated
    (returning 0) instead of raising, so the scheduler keeps firing jobs."""
    from cron import executions

    try:
        return executions.recover_interrupted_executions()
    except sqlite3.DatabaseError as exc:
        quarantined = quarantine_corrupt_ledger()
        logger.error(
            "Cron execution ledger %s is corrupt (%s); moved it to %s and started an empty one. "
            "Scheduling continues; run history before now is in the quarantined file.",
            _ledger_path(), exc, quarantined or "<not moved>")
        with executions._transaction():
            pass  # recreate the schema now, so the next claim does not meet the error first
        return 0
