"""A test/harness path must not open the LIVE kanban DB.

Card t_35f9878e. On 2026-07-26 an E2E harness (``/tmp/e2e_promote.py`` &
siblings) imported the PR's ``kanban_db`` from a clone but never repointed
``HERMES_HOME`` / the DB path at a temp dir, so ``kb.connect()`` resolved to the
real ``~/.hermes/kanban.db`` and the harness drove ``create_task`` /
``claim_task`` / ``block_task`` against production rows — 36 junk cards on the
live board in a 113-second burst.

The durable fix is a guard at the single connection chokepoint (:func:`connect`):
when a test/harness context is detected (``PYTEST_CURRENT_TEST`` or an explicit
``HERMES_KANBAN_TEST``) but the resolved DB path is the real live install DB,
refuse loudly with the fix in the message ("point HERMES_HOME at a tmp dir").

A properly isolated test (the autouse hermetic fixture points ``HERMES_HOME`` at
a per-test tmp dir) resolves to a temp path, never the live one, so it is
unaffected. These tests pin exactly that boundary.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _real_live_db_path() -> Path:
    """The un-redirectable live install DB path the buggy harness hit.

    Deliberately computed from ``Path.home()`` (NOT ``HERMES_HOME``, which the
    hermetic fixture points at a tmp dir) so it matches the exact path a harness
    that forgot to isolate resolves to.
    """
    return Path.home() / ".hermes" / "kanban.db"


def test_connect_refuses_live_db_under_pytest_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce the harness bug: a test-context connect to the live DB is refused.

    ``PYTEST_CURRENT_TEST`` is already set by pytest for this test. We point the
    resolved DB path at the REAL live install DB (as the un-isolated harness
    did) and assert ``connect()`` raises rather than opening it.
    """
    live = _real_live_db_path()
    # Mimic the harness: the resolved path is the real live DB.
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live))
    assert os.environ.get("PYTEST_CURRENT_TEST"), "pytest must set this for the test"

    with pytest.raises(RuntimeError) as exc:
        kb.connect()

    msg = str(exc.value)
    assert "live" in msg.lower()
    assert "HERMES_HOME" in msg  # names the fix
    assert "tmp" in msg.lower()  # tells the author to point at a temp dir


def test_connect_refuses_live_db_under_explicit_test_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A standalone harness (no pytest) opts in via ``HERMES_KANBAN_TEST``.

    The ``/tmp/e2e_*.py`` scripts do not run under pytest, so the durable
    contract is that a harness sets ``HERMES_KANBAN_TEST=1``; the guard then
    refuses a live-DB connect exactly as under pytest.
    """
    live = _real_live_db_path()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_TEST", "1")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live))

    with pytest.raises(RuntimeError) as exc:
        kb.connect()
    assert "live" in str(exc.value).lower()


def test_connect_allows_isolated_temp_db_in_test_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A properly isolated test (temp DB) connects fine even in a test context.

    This is the correct harness shape (cf. ``scripts/e2e_sweep_wake_suppression``):
    pin ``HERMES_KANBAN_DB`` at a throwaway path BEFORE connecting.
    """
    monkeypatch.setenv("HERMES_KANBAN_TEST", "1")
    db = tmp_path / "board.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))

    conn = kb.connect()
    try:
        # A card can be created against the isolated store without tripping the guard.
        tid = kb.create_task(conn, title="isolated", assignee="worker", detached=True)
        assert tid
    finally:
        conn.close()
    assert db.exists()


def test_connect_allows_live_db_outside_test_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real gateway/CLI (no test signal) must still open the live DB.

    The guard keys on a test/harness signal; production has neither
    ``PYTEST_CURRENT_TEST`` nor ``HERMES_KANBAN_TEST`` set, so a live connect is
    allowed. We verify the guard does NOT raise for the live path when both
    signals are absent — without actually opening the real DB (an explicit
    ``db_path`` to a temp file keeps the resolved path off the live one while
    proving the signal gate, and a separate call proves the live path passes the
    guard check itself).
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TEST", raising=False)
    live = _real_live_db_path()
    # The guard function itself must not flag the live path when no test signal
    # is present. Assert via the internal predicate to avoid opening the real DB.
    assert kb._is_test_context() is False
    assert kb._would_write_live_db(live) is True  # it IS the live path...
    # ...but with no test signal, the connect guard is a no-op. Prove the guard
    # gate returns cleanly (does not raise) for this combination.
    kb._assert_not_test_write_to_live_db(live)  # must not raise
