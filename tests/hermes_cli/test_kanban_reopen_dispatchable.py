"""Regression: ``reopen_task`` into a dispatchable lane must leave the card
genuinely dispatchable.

Card t_44b5ad38. ``reopen_task`` is the sanctioned un-done path (walk a
false-``done`` card back to a live lane). It was used on 2026-08-24 to reconcile
``t_16a493a3`` after a webhook wrongly completed it: the reopen landed the card
in ``ready`` with the right assignee, but the card then refused to dispatch,
emitting ``respawn_guarded: active_pr`` on every tick because the card carried
an open PR.

The cause is the same wedge ``route_feedback_to_author`` documents: a transition
into ``ready`` that does NOT emit an ``unblocked`` event leaves
``check_respawn_guard`` returning ``active_pr`` (the ``pr_cutoff`` is only raised
past a prior PR-handoff comment by an ``unblocked`` event), so the dispatcher
never spawns the author. The manual recovery — push the card back through
``block_task`` then ``route_feedback_to_author`` — is exactly the hand-rolled
guard-clearing dance the sanctioned primitives exist to prevent.

These tests pin the fix: reopening into a dispatchable lane (``ready`` / ``todo``)
emits the ``unblocked`` cutoff event and resets ``block_recurrences``, so a
false-``done`` card with an open PR dispatches on the next tick. Reopening into a
non-dispatchable lane (``blocked`` / ``review``) is unchanged, and the clean-no-op
contract (a non-``done`` card is a no-op) still holds.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


_PR_URL = "https://github.com/cwest/hermes-agent/pull/123"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB.

    ``_resolve_pr_state`` is stubbed to ``"open"`` so the open-PR card these
    tests stage is treated as genuinely active work without a live ``gh`` call
    (a non-existent fixture PR URL would otherwise resolve to ``not_found`` and
    clear the very guard under test).
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_resolve_pr_state", lambda url: "open")
    kb.init_db()
    return home


def _false_done_open_pr_card(conn, *, author: str = "eckert") -> str:
    """Stage a card that reached ``done`` while carrying an OPEN PR — the
    ``t_16a493a3`` shape.

    The author builds, posts the PR-URL handoff comment, and the card is
    (wrongly) completed to ``done``. The PR-handoff comment and completed run
    are back-dated so a subsequent reopen's ``unblocked`` event is causally
    LATER than the PR URL (the ``active_pr`` cutoff is second-granular).
    """
    tid = kb.create_task(
        conn, title="false-done carrying an open PR", assignee=author, detached=True
    )
    kb.claim_task(conn, tid)
    kb.add_comment(
        conn, tid, author=author,
        body=f"[audit] actor={author} stage=implement pr={_PR_URL}\n"
             f"notes: draft PR opened; ready for review.",
    )
    kb.complete_task(conn, tid, result=f"PR opened: {_PR_URL}",
                     allow_acceptance_complete=True)
    assert kb.get_task(conn, tid).status == "done"
    # Back-date the PR-handoff comment + completed run so the reopen unblock is
    # causally LATER than the PR URL.
    past = int(time.time()) - 600
    with kb.write_txn(conn):
        conn.execute("UPDATE task_comments SET created_at=? WHERE task_id=?", (past, tid))
        conn.execute(
            "UPDATE task_runs SET ended_at=? WHERE task_id=? AND ended_at IS NOT NULL",
            (past, tid),
        )
    return tid


# ---------------------------------------------------------------------------
# RED 1 — reopening a false-done open-PR card into ``ready`` clears the guard
# ---------------------------------------------------------------------------


def test_reopen_to_ready_open_pr_card_dispatches(kanban_home: Path) -> None:
    """The t_16a493a3 sequence: a false-``done`` card carrying an OPEN PR,
    reopened into ``ready``, must be respawn-guarded BEFORE the reopen and
    dispatchable (guard clears) AFTER."""
    with kb.connect() as conn:
        tid = _false_done_open_pr_card(conn, author="eckert")

        ok = kb.reopen_task(
            conn, tid, reason="false-done: webhook wrongly completed t_16a493a3",
            to_status="ready", assignee="eckert",
        )
        assert ok is True

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.assignee == "eckert"
        # The guard must clear — the dispatcher spawns the author on the next tick.
        assert kb.check_respawn_guard(conn, tid) is None, \
            "reopen into a dispatchable lane must clear the active_pr respawn guard"


def test_reopen_to_ready_emits_unblocked_event(kanban_home: Path) -> None:
    """Reopening into a dispatchable lane emits the ``unblocked`` cutoff event
    (that event is what raises the active_pr ``pr_cutoff`` past the PR-handoff
    comment)."""
    with kb.connect() as conn:
        tid = _false_done_open_pr_card(conn)
        kb.reopen_task(conn, tid, reason="false-done reconcile", to_status="ready")

        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "unblocked" in kinds, "a dispatchable reopen must emit an unblocked event"


def test_reopen_to_ready_resets_block_recurrences(kanban_home: Path) -> None:
    """Reopening into a dispatchable lane resets an inflated ``block_recurrences``
    so the loop breaker does not later escalate the card to ``triage``."""
    with kb.connect() as conn:
        tid = _false_done_open_pr_card(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET block_recurrences=? WHERE id=?",
                (kb.BLOCK_RECURRENCE_LIMIT, tid),
            )

        kb.reopen_task(conn, tid, reason="false-done reconcile", to_status="ready")

        row = conn.execute(
            "SELECT block_recurrences FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert int(row["block_recurrences"] or 0) == 0, \
            "block_recurrences must be reset on a dispatchable reopen"


def test_reopen_to_todo_open_pr_card_dispatches(kanban_home: Path) -> None:
    """``todo`` is also a dispatchable lane (recompute_ready promotes a
    parent-free todo to ready, then the same active_pr guard applies). Reopening
    into ``todo`` must therefore also clear the guard and emit ``unblocked``."""
    with kb.connect() as conn:
        tid = _false_done_open_pr_card(conn)
        kb.reopen_task(conn, tid, reason="false-done reconcile", to_status="todo")

        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "unblocked" in kinds
        assert kb.check_respawn_guard(conn, tid) is None


# ---------------------------------------------------------------------------
# RED 2 — reopening into a NON-dispatchable lane is unchanged
# ---------------------------------------------------------------------------


def test_reopen_to_blocked_does_not_unblock(kanban_home: Path) -> None:
    """Reopening into ``blocked`` (a non-dispatchable target) must NOT emit an
    ``unblocked`` event or reset the loop counter — the guard-clearing bounce is
    scoped to the dispatchable targets (``ready`` / ``todo``). The card's final
    lane is governed by the pre-existing ``recompute_ready`` auto-promotion and
    is deliberately not asserted here; the invariant is that the reopen itself
    emitted no ``unblocked`` and left the recurrence counter untouched."""
    with kb.connect() as conn:
        tid = _false_done_open_pr_card(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET block_recurrences=? WHERE id=?", (2, tid)
            )

        ok = kb.reopen_task(conn, tid, reason="park for triage", to_status="blocked")
        assert ok is True

        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "unblocked" not in kinds, \
            "reopening into a non-dispatchable target must not emit unblocked"
        row = conn.execute(
            "SELECT block_recurrences FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert int(row["block_recurrences"] or 0) == 2, \
            "reopening into a non-dispatchable target must not reset block_recurrences"


def test_reopen_to_review_does_not_unblock(kanban_home: Path) -> None:
    """Reopening straight to ``review`` (route back to the skipped reviewer) is a
    non-dispatchable target: no ``unblocked`` bounce."""
    with kb.connect() as conn:
        tid = _false_done_open_pr_card(conn)
        ok = kb.reopen_task(
            conn, tid, reason="route back to reviewer",
            to_status="review", assignee="lamport",
        )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        assert task.assignee == "lamport"
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "unblocked" not in kinds


# ---------------------------------------------------------------------------
# RED 3 — the clean-no-op contract still holds
# ---------------------------------------------------------------------------


def test_reopen_non_done_card_is_still_a_no_op(kanban_home: Path) -> None:
    """A non-``done`` card is a clean no-op even for a dispatchable target:
    False, no status change, no ``unblocked`` event."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="live card", assignee="eckert", detached=True)
        kb.claim_task(conn, tid)
        assert kb.get_task(conn, tid).status == "running"

        ok = kb.reopen_task(conn, tid, reason="should not apply", to_status="ready")
        assert ok is False
        assert kb.get_task(conn, tid).status == "running"
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "unblocked" not in kinds
        assert "reopened" not in kinds
