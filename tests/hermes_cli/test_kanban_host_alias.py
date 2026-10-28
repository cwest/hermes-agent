"""Host-identity normalization for Kanban claim locks.

A claim lock is a ``host:pid`` string. The host part comes from
``socket.gethostname()``, which on macOS flips between the mDNS ``.local`` name
and the DHCP/router ``.lan`` name for the SAME machine depending on the active
network. The liveness gates (``release_stale_claims`` /
``detect_crashed_workers`` / ``enforce_max_runtime`` / the reconcile) decide
"is this claim local to me?" by comparing the lock's host prefix to the current
hostname — so a claim taken as ``home-m2u-01.local:<pid>`` becomes un-reapable
the moment the box starts reporting ``home-m2u-01.lan``, leaving the run stuck
``status=running`` forever (the live rot this fixes: 135 such rows).

These tests pin the normalization that treats ``.local`` / ``.lan`` / any DNS
suffix on the same stem as the same machine, while keeping a genuinely different
host foreign.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Unit: _machine_id strips the DNS/mDNS suffix so aliases collapse to one stem.
# ---------------------------------------------------------------------------

def test_machine_id_strips_local_and_lan_suffix():
    # The two mDNS/DHCP faces of the same box collapse to the same stem.
    assert kb._machine_id("home-m2u-01.local") == kb._machine_id("home-m2u-01.lan")
    assert kb._machine_id("home-m2u-01.local") == "home-m2u-01"
    assert kb._machine_id("home-m2u-01.lan") == "home-m2u-01"


def test_machine_id_strips_arbitrary_domain_suffix():
    # A corp/VPN domain suffix is also just an alias of the same stem.
    assert kb._machine_id("home-m2u-01.corp.example.com") == "home-m2u-01"
    assert kb._machine_id("home-m2u-01") == "home-m2u-01"


def test_machine_id_accepts_a_full_lock_or_bare_host():
    # Works whether handed a bare host or a full ``host:pid`` claim lock.
    assert kb._machine_id("home-m2u-01.local:12345") == "home-m2u-01"
    assert kb._machine_id("home-m2u-01.lan:999") == "home-m2u-01"


def test_machine_id_is_case_insensitive():
    assert kb._machine_id("Home-M2U-01.LOCAL") == kb._machine_id("home-m2u-01.lan")


def test_machine_id_distinct_hosts_stay_distinct():
    assert kb._machine_id("home-m2u-01.local") != kb._machine_id("other-box.lan")


def test_machine_id_handles_empty_and_none():
    assert kb._machine_id("") == ""
    assert kb._machine_id(None) == ""


# ---------------------------------------------------------------------------
# Unit: _lock_is_host_local treats .local/.lan aliases as the same host.
# ---------------------------------------------------------------------------

def test_lock_is_host_local_across_local_lan_alias(monkeypatch):
    # Current box reports ``.lan``; a claim written under ``.local`` is still
    # host-local (and vice versa).
    monkeypatch.setattr(kb, "_claimer_id", lambda: "home-m2u-01.lan:876")
    assert kb._lock_is_host_local("home-m2u-01.local:1234") is True
    assert kb._lock_is_host_local("home-m2u-01.lan:1234") is True

    monkeypatch.setattr(kb, "_claimer_id", lambda: "home-m2u-01.local:876")
    assert kb._lock_is_host_local("home-m2u-01.lan:1234") is True
    assert kb._lock_is_host_local("home-m2u-01.local:1234") is True


def test_lock_is_host_local_rejects_a_different_host(monkeypatch):
    monkeypatch.setattr(kb, "_claimer_id", lambda: "home-m2u-01.lan:876")
    assert kb._lock_is_host_local("some-other-box.local:1234") is False
    assert kb._lock_is_host_local("some-other-box.lan:1234") is False


def test_lock_is_host_local_empty_lock_is_local(monkeypatch):
    # An orphaned run with no lock can only be reaped here, so a NULL/empty lock
    # is treated as host-local (matches the reconcile's documented contract).
    monkeypatch.setattr(kb, "_claimer_id", lambda: "home-m2u-01.lan:876")
    assert kb._lock_is_host_local("") is True
    assert kb._lock_is_host_local(None) is True


# ---------------------------------------------------------------------------
# Integration: the reconcile reaps a dead run claimed under the host alias.
# ---------------------------------------------------------------------------

def test_reconcile_reaps_dead_run_under_host_alias(kanban_home, monkeypatch):
    """A dead run whose claim_lock host is this machine's OTHER alias is reaped,
    not skipped as foreign. Mirrors the 135 live ``.local`` rows while the box
    now reports ``.lan``.
    """
    dead_pid = 999999

    # Pin the current identity to the ``.lan`` face of this box.
    monkeypatch.setattr(kb, "_claimer_id", lambda: "home-m2u-01.lan:876")

    with kb.connect() as conn:
        t_alias = kb.create_task(conn, title="alias", assignee="a", detached=True)
        # Dead run claimed under the OTHER alias (``.local``).
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, worker_pid, "
            "claim_lock, started_at) "
            "VALUES (?, 'a', 'running', ?, 'home-m2u-01.local:5555', ?)",
            (t_alias, dead_pid, int(time.time())),
        )
        alias_run_id = conn.execute(
            "SELECT last_insert_rowid() AS id"
        ).fetchone()["id"]

        dry = kb.reconcile_dead_running_runs(conn, apply=False)
        # The alias-host dead run is judged dead, NOT reported foreign.
        assert dry["foreign_host"] == 0
        assert alias_run_id in dry["dead_run_ids"]

        applied = kb.reconcile_dead_running_runs(conn, apply=True)
        assert applied["closed"] == 1
        row = conn.execute(
            "SELECT status, ended_at FROM task_runs WHERE id = ?", (alias_run_id,)
        ).fetchone()
        assert row["status"] == "superseded"
        assert row["ended_at"] is not None


def test_reconcile_still_skips_a_genuinely_foreign_host(kanban_home, monkeypatch):
    """A dead run on a genuinely different machine is still left foreign."""
    dead_pid = 999999
    monkeypatch.setattr(kb, "_claimer_id", lambda: "home-m2u-01.lan:876")

    with kb.connect() as conn:
        t_foreign = kb.create_task(conn, title="foreign", assignee="a", detached=True)
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, worker_pid, "
            "claim_lock, started_at) "
            "VALUES (?, 'a', 'running', ?, 'other-box.lan:5555', ?)",
            (t_foreign, dead_pid, int(time.time())),
        )
        foreign_run_id = conn.execute(
            "SELECT last_insert_rowid() AS id"
        ).fetchone()["id"]

        dry = kb.reconcile_dead_running_runs(conn, apply=False)
        assert dry["foreign_host"] == 1
        assert foreign_run_id not in dry["dead_run_ids"]

        applied = kb.reconcile_dead_running_runs(conn, apply=True)
        assert applied["closed"] == 0
        assert conn.execute(
            "SELECT ended_at FROM task_runs WHERE id = ?", (foreign_run_id,)
        ).fetchone()["ended_at"] is None
