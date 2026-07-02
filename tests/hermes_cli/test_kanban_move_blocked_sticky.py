"""Regression tests: a ``move_card``-driven acceptance transition to
``status='blocked'`` must be STICKY across ``recompute_ready``.

The bug: the one-card MOVE primitive (``onecard.move_card``, used by the
reviewer for the atomic PASS→acceptance transition) sets
``status='blocked'`` and emits a ``status_changed`` event carrying
``{"to": "blocked", "by": "onecard:move_card"}`` — but it does NOT emit a
``blocked`` event. ``_has_sticky_block`` only keyed on the most recent
``{blocked, unblocked}`` event, so a move-driven blocked returned False and
``recompute_ready`` auto-promoted the acceptance card ``blocked → ready``,
landing it in the reviewer's lane with the acceptance owner's name and
violating the "waiting on a human, do NOT auto-recover" semantics.

The fix makes ``_has_sticky_block`` also recognize a ``move_card``-driven
``status_changed`` → blocked as a sticky signal, while leaving the
circuit-breaker (``gave_up``) and raw-DB-write paths auto-recoverable.

These tests simulate the exact event trail ``onecard.move_card`` writes,
so they pin the emit *contract* the two systems share, not an internal
implementation detail.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _simulate_move_card_to_blocked(
    conn, task_id: str, *, from_status: str, from_assignee: str,
    to_assignee: str, reason: str | None = None,
) -> None:
    """Reproduce the exact DB writes ``onecard.move_card(status='blocked')``
    performs: an atomic status/assignee UPDATE plus a ``status_changed``
    event marked ``by=onecard:move_card`` and an ``assigned`` event. It
    deliberately emits NO ``blocked`` event — that omission is the bug this
    test guards against.
    """
    payload = {"from": from_status, "to": "blocked", "by": "onecard:move_card"}
    if reason is not None:
        payload["reason"] = reason
    conn.execute(
        "UPDATE tasks SET status='blocked', assignee=? WHERE id=?",
        (to_assignee, task_id),
    )
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, 'status_changed', ?, ?)",
        (task_id, json.dumps(payload), int(time.time())),
    )
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, 'assigned', ?, ?)",
        (
            task_id,
            json.dumps(
                {"from": from_assignee, "to": to_assignee, "by": "onecard:move_card"}
            ),
            int(time.time()),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# The core bug: acceptance card reached via move_card must stay blocked
# ---------------------------------------------------------------------------


def test_move_card_acceptance_block_is_not_auto_promoted(kanban_home: Path) -> None:
    """A card moved to ``blocked``+casey via the atomic PASS→acceptance
    transition (``move_card``) must stay blocked across dispatcher ticks —
    it is waiting on a human and must NOT auto-recover."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="pr passed, awaiting sign-off")
        kb.claim_task(conn, tid)
        # Reviewer's atomic PASS→acceptance move: review → blocked + casey.
        _simulate_move_card_to_blocked(
            conn, tid,
            from_status="review", from_assignee="lamport", to_assignee="casey",
            reason="awaiting-casey-signoff: PR PASS'd; SHA==head",
        )
        assert kb.get_task(conn, tid).status == "blocked"

        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, "move_card acceptance block must not auto-promote"
            assert kb.get_task(conn, tid).status == "blocked"
            assert kb.get_task(conn, tid).assignee == "casey"


def test_move_card_block_makes_has_sticky_block_true(kanban_home: Path) -> None:
    """``_has_sticky_block`` must recognize the move-driven blocked
    transition as sticky (the explicit ``by=onecard:move_card`` marker),
    not just an emitted ``blocked`` event."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="acceptance card")
        kb.claim_task(conn, tid)
        _simulate_move_card_to_blocked(
            conn, tid,
            from_status="review", from_assignee="lamport", to_assignee="casey",
            reason="awaiting-casey-signoff: ready for merge",
        )
        assert kb._has_sticky_block(conn, tid) is True


def test_move_card_acceptance_block_sticky_even_with_done_parents(
    kanban_home: Path,
) -> None:
    """The parent-completion path is exactly what ``recompute_ready`` was
    built for, so it's the most dangerous false-positive: even with every
    parent done, a move_card acceptance block must stay blocked."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.complete_task(conn, parent, result="parent ok")

        kb.claim_task(conn, child)
        _simulate_move_card_to_blocked(
            conn, child,
            from_status="review", from_assignee="lamport", to_assignee="casey",
            reason="awaiting-casey-signoff: child ready",
        )
        assert kb.get_task(conn, child).status == "blocked"

        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "blocked"


# ---------------------------------------------------------------------------
# Regressions: the fix must NOT make other blocked paths sticky
# ---------------------------------------------------------------------------


def test_circuit_breaker_gave_up_still_auto_recovers(kanban_home: Path) -> None:
    """The circuit-breaker emits ``gave_up`` (not a move_card
    ``status_changed``), so the fix must leave it auto-recoverable —
    otherwise transient crashes would wedge forever."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.complete_task(conn, parent, result="ok")

        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (child,))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'gave_up', NULL, ?)",
            (child, int(time.time())),
        )
        conn.commit()

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        assert kb.get_task(conn, child).status == "ready"


def test_move_card_to_ready_does_not_make_sticky(kanban_home: Path) -> None:
    """Only a move to ``blocked`` is sticky. A ``status_changed`` to a
    non-blocked status carrying the same ``by=onecard:move_card`` marker
    (e.g. a bounce back to ``ready``) must NOT trip the sticky guard."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.complete_task(conn, parent, result="ok")

        # move_card review → ready + eckert (a bounce), then a later
        # transient status flip to blocked (circuit-breaker style).
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (child,))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'status_changed', ?, ?)",
            (
                child,
                json.dumps({"from": "review", "to": "ready", "by": "onecard:move_card"}),
                int(time.time()),
            ),
        )
        conn.commit()

        assert kb._has_sticky_block(conn, child) is False
        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        assert kb.get_task(conn, child).status == "ready"


def test_unblock_after_move_card_block_clears_sticky(kanban_home: Path) -> None:
    """An explicit ``unblock`` after a move_card acceptance block clears the
    sticky state — the ``unblocked`` event is the most recent signal and
    wins over the earlier move-driven blocked."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t")
        kb.claim_task(conn, tid)
        _simulate_move_card_to_blocked(
            conn, tid,
            from_status="review", from_assignee="lamport", to_assignee="casey",
            reason="awaiting-casey-signoff: x",
        )
        assert kb._has_sticky_block(conn, tid) is True

        assert kb.unblock_task(conn, tid)
        assert kb._has_sticky_block(conn, tid) is False
        assert kb.get_task(conn, tid).status == "ready"


def test_gave_up_after_move_card_block_stays_sticky(kanban_home: Path) -> None:
    """If a spurious ``gave_up`` (or crash) is recorded AFTER a move_card
    acceptance block, the acceptance block must still win — a circuit-breaker
    event must never silently override a deliberate human-gated acceptance.
    This mirrors the #28712 loop guard for the move_card path."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="acceptance then spurious gave_up")
        kb.claim_task(conn, tid)
        _simulate_move_card_to_blocked(
            conn, tid,
            from_status="review", from_assignee="lamport", to_assignee="casey",
            reason="awaiting-casey-signoff: y",
        )
        # A later spurious gave_up (no intervening unblock).
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'gave_up', NULL, ?)",
            (tid, int(time.time()) + 1),
        )
        conn.commit()

        assert kb._has_sticky_block(conn, tid) is True
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, tid).status == "blocked"
