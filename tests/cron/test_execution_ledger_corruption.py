"""A corrupt cron/executions.db must not stop every scheduled job.

Upstream already keeps the ticker THREAD alive through a startup error (#111010), but the ledger
itself stayed corrupt: every later ``create_execution`` (the first step of ``claim_fire``) raised
``sqlite3.DatabaseError``, so no job could fire until someone deleted the file by hand. Each
quarantined corrupt copy seen in the field was a WAL-mode file (header bytes 18/19 == 2). The
torn-WAL / WAL-reset b-tree class only reaches WAL databases, and the home backup omits the
``-wal`` sidecar, so a restore over a live WAL tears the page tree.

Contract:
- the ledger is a rollback-journal (DELETE) file, and a legacy WAL ledger is downgraded on open;
- provider recovery quarantines a corrupt store as ``executions.db.corrupt-<ts>`` (with
  sidecars), recreates an empty schema and carries on. The raw ``recover_interrupted_executions``
  keeps its fail-closed contract;
- a healthy store is untouched.
"""

import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_GARBAGE = b"not a sqlite database at all" * 64


def _point_ledger(monkeypatch, tmp_path):
    from cron import executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


def _journal_mode(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        return conn.execute("PRAGMA journal_mode").fetchone()[0].lower()


def test_ledger_is_a_rollback_journal_file(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.create_execution("mode-check", source="builtin")

    assert _journal_mode(executions.EXECUTIONS_FILE) == "delete"
    head = executions.EXECUTIONS_FILE.read_bytes()[:20]
    assert (head[18], head[19]) == (1, 1)


def test_legacy_wal_ledger_is_downgraded_on_open(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.EXECUTIONS_FILE.parent.mkdir(parents=True)
    with sqlite3.connect(executions.EXECUTIONS_FILE) as seed:
        assert seed.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    seed.close()

    executions.list_executions(limit=1)

    assert _journal_mode(executions.EXECUTIONS_FILE) == "delete"


def test_incident_table_opener_keeps_the_shared_file_in_delete_mode(monkeypatch, tmp_path):
    """cron_incidents lives in the SAME executions.db; an opener that re-applies WAL flips the
    file back and races the ledger's own DELETE pragma into ``database is locked``."""
    from cron import incidents

    executions = _point_ledger(monkeypatch, tmp_path)
    executions.create_execution("first", source="builtin")
    conn = incidents._connect()
    conn.close()

    assert _journal_mode(executions.EXECUTIONS_FILE) == "delete"


def test_raw_recovery_stays_fail_closed(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.EXECUTIONS_FILE.parent.mkdir(parents=True)
    executions.EXECUTIONS_FILE.write_bytes(_GARBAGE)

    with pytest.raises(sqlite3.DatabaseError):
        executions.recover_interrupted_executions()
    assert executions.EXECUTIONS_FILE.read_bytes() == _GARBAGE


def test_provider_recovery_quarantines_and_jobs_can_fire_again(monkeypatch, tmp_path):
    from cron.scheduler_provider import InProcessCronScheduler

    executions = _point_ledger(monkeypatch, tmp_path)
    cron_dir = executions.EXECUTIONS_FILE.parent
    cron_dir.mkdir(parents=True)
    executions.EXECUTIONS_FILE.write_bytes(_GARBAGE)
    Path(str(executions.EXECUTIONS_FILE) + "-wal").write_bytes(b"stale wal")

    assert InProcessCronScheduler().recover_interrupted() == 0

    quarantined = [p for p in cron_dir.iterdir() if p.name.startswith("executions.db.corrupt-")]
    main = [p for p in quarantined if not p.name.endswith(("-wal", "-shm", "-journal"))]
    assert len(main) == 1 and main[0].read_bytes() == _GARBAGE
    # SQLite may already have dropped the orphaned -wal when the failed open closed (3.53 does,
    # older builds keep it); a sidecar that survived must travel with the quarantined file.
    moved_wal = Path(str(main[0]) + "-wal")
    assert not moved_wal.exists() or moved_wal.read_bytes() == b"stale wal"
    assert not Path(str(executions.EXECUTIONS_FILE) + "-wal").exists()
    assert executions.create_execution("after", source="builtin")["status"] == "claimed"
    assert _journal_mode(executions.EXECUTIONS_FILE) == "delete"


def test_quarantine_moves_surviving_sidecars_with_the_ledger(monkeypatch, tmp_path):
    from cron.executions_recovery import quarantine_corrupt_ledger

    executions = _point_ledger(monkeypatch, tmp_path)
    executions.EXECUTIONS_FILE.parent.mkdir(parents=True)
    executions.EXECUTIONS_FILE.write_bytes(_GARBAGE)
    for ext in ("-wal", "-shm", "-journal"):
        Path(f"{executions.EXECUTIONS_FILE}{ext}").write_bytes(ext.encode())

    target = quarantine_corrupt_ledger()

    assert target is not None and target.read_bytes() == _GARBAGE and ".corrupt-" in target.name
    for ext in ("-wal", "-shm", "-journal"):
        assert Path(f"{target}{ext}").read_bytes() == ext.encode()
        assert not Path(f"{executions.EXECUTIONS_FILE}{ext}").exists()
    assert not executions.EXECUTIONS_FILE.exists()
    assert quarantine_corrupt_ledger() is None


def test_provider_recovery_leaves_a_healthy_store_alone(monkeypatch, tmp_path):
    from cron.scheduler_provider import InProcessCronScheduler

    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("healthy", source="builtin")
    executions.mark_execution_running(record["id"])

    assert InProcessCronScheduler().recover_interrupted() == 0
    assert not [p for p in executions.EXECUTIONS_FILE.parent.iterdir() if ".corrupt-" in p.name]
    assert executions.latest_execution("healthy")["status"] == "running"


def _quarantined(executions):
    return [p for p in executions.EXECUTIONS_FILE.parent.iterdir() if ".corrupt-" in p.name]


def test_locked_healthy_ledger_is_not_quarantined(monkeypatch, tmp_path):
    """A writer holding the lock past busy_timeout is contention, not corruption. Quarantining
    would strand the holder's in-flight claims in the renamed inode and drop the
    ``scheduled_instant`` rows that block a second fire of the same slot."""
    from cron.executions_recovery import recover_interrupted_executions_resilient

    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("held", source="builtin")
    holder = sqlite3.connect(executions.EXECUTIONS_FILE, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            recover_interrupted_executions_resilient()
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert not _quarantined(executions)
    assert executions.latest_execution("held")["id"] == record["id"]


@pytest.mark.parametrize("message", ["disk I/O error", "unable to open database file"])
def test_non_corruption_database_errors_propagate_untouched(monkeypatch, tmp_path, message):
    from cron.executions_recovery import recover_interrupted_executions_resilient

    executions = _point_ledger(monkeypatch, tmp_path)
    executions.create_execution("kept", source="builtin")
    monkeypatch.setattr(executions, "recover_interrupted_executions",
                        lambda: (_ for _ in ()).throw(sqlite3.OperationalError(message)))

    with pytest.raises(sqlite3.OperationalError, match=message):
        recover_interrupted_executions_resilient()
    assert not _quarantined(executions)
    assert executions.latest_execution("kept")["status"] == "claimed"


def test_ticker_start_on_a_corrupt_ledger_leaves_a_usable_store(monkeypatch, tmp_path):
    from cron.scheduler_provider import InProcessCronScheduler

    executions = _point_ledger(monkeypatch, tmp_path)
    executions.EXECUTIONS_FILE.parent.mkdir(parents=True)
    executions.EXECUTIONS_FILE.write_bytes(_GARBAGE)
    ticks, stop = [], threading.Event()

    with patch("cron.scheduler.tick", side_effect=lambda *a, **k: ticks.append(k) or 0), \
         patch("cron.jobs.record_ticker_heartbeat"):
        worker = threading.Thread(target=InProcessCronScheduler().start, args=(stop,),
                                  kwargs={"interval": 0}, daemon=True)
        worker.start()
        deadline = time.monotonic() + 10
        while not ticks and time.monotonic() < deadline:
            stop.wait(0.01)
        stop.set()
        worker.join(timeout=5)

    assert ticks and not worker.is_alive()
    assert executions.create_execution("fires", source="builtin")["status"] == "claimed"
