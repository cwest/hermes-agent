"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking.
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-cause re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``unblock_task`` deliberately does NOT reset ``block_recurrences`` (the
  amnesia that let the loop run unbounded).
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    """Create a task and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------


def test_first_typed_block_lands_in_blocked(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="which key?", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind == "needs_input"
        assert t.block_recurrences == 1


def test_unblock_does_not_reset_recurrence_counter(kanban_home: Path) -> None:
    """The crux of the fix: unblock must preserve the loop counter."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="needs_input")
        assert kb.get_task(conn, tid).block_recurrences == 1
        assert kb.unblock_task(conn, tid)
        t = kb.get_task(conn, tid)
        assert t.status == "ready"
        assert t.block_recurrences == 1  # NOT reset to 0
        assert t.block_kind == "needs_input"  # kind preserved for comparison


def test_same_cause_reblock_routes_to_triage(kanban_home: Path) -> None:
    """Dale's loop: block → unblock → re-block same kind → triage."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="need creds", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="still need creds", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "triage"
        assert t.block_recurrences == 2


def test_untyped_block_loop_also_protected(kanban_home: Path) -> None:
    """Legacy un-typed blocks (kind=None) still trip the breaker."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="a")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="a again")
        assert kb.get_task(conn, tid).status == "triage"


def test_different_kinds_do_not_compound(kanban_home: Path) -> None:
    """A re-block for a DIFFERENT reason resets the counter to 1."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="a", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="b", kind="capability")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_recurrences == 1


def test_block_loop_detected_event_emitted(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
        assert events, "expected a block_loop_detected event"
        payload = events[-1].payload or {}
        assert payload.get("recurrences") == 2
        assert payload.get("kind") == "capability"


# ---------------------------------------------------------------------------
# Review-bounce cycles must not false-trip the loop breaker
# ---------------------------------------------------------------------------
#
# A healthy multi-round review cycle (CHANGES-REQUESTED -> rework -> a NEW,
# different finding -> CHANGES-REQUESTED again) is blocked with the SAME
# ``kind`` every round, so the pure ``prev_kind == kind`` classifier counted
# each healthy round as "the same failure repeating" and routed the card to
# ``triage`` at the limit -- stranding it off the ``auto_route_review_bounce``
# path. The fix: a ``review-changes-requested`` re-block whose reason MATERIALLY
# DIFFERS from the prior round is progress, not a loop, and must not increment
# (it resets to 1). A same-reason review bounce IS a real loop and still trips.


def test_review_bounce_distinct_findings_do_not_route_to_triage(
    kanban_home: Path,
) -> None:
    """Round 2 with a DIFFERENT finding is progress, not a loop."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(
            conn, tid,
            reason="review-changes-requested: fix the null deref in foo()",
            kind="needs_input",
        )
        assert kb.get_task(conn, tid).block_recurrences == 1
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        # Distinct finding on the second round.
        kb.block_task(
            conn, tid,
            reason="review-changes-requested: add a test for the retry path",
            kind="needs_input",
        )
        t = kb.get_task(conn, tid)
        assert t.status == "blocked", "distinct-finding review bounce must stay blocked"
        assert t.block_recurrences == 1, "distinct finding resets the loop counter"


def test_review_bounce_three_distinct_rounds_never_triage(
    kanban_home: Path,
) -> None:
    """A 3-round review cycle with distinct findings never escalates."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        findings = [
            "review-changes-requested: finding A",
            "review-changes-requested: finding B",
            "review-changes-requested: finding C",
        ]
        for i, reason in enumerate(findings):
            if i > 0:
                _make_running_again(conn, tid)
            kb.block_task(conn, tid, reason=reason, kind="needs_input")
            t = kb.get_task(conn, tid)
            assert t.status == "blocked", f"round {i} must stay blocked"
            assert t.block_recurrences == 1
            kb.unblock_task(conn, tid)


def test_review_bounce_same_finding_still_escalates(kanban_home: Path) -> None:
    """A reviewer bouncing the IDENTICAL unfixed finding IS a real loop."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        reason = "review-changes-requested: fix the null deref in foo()"
        kb.block_task(conn, tid, reason=reason, kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason=reason, kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "triage", "identical-finding repeat is a genuine loop"
        assert t.block_recurrences == 2


def test_review_bounce_same_finding_whitespace_insensitive(
    kanban_home: Path,
) -> None:
    """Trivial whitespace/case differences are NOT a material change."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(
            conn, tid,
            reason="review-changes-requested: Fix the null deref",
            kind="needs_input",
        )
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        # Same finding, only whitespace/case jitter -> still the same cause.
        kb.block_task(
            conn, tid,
            reason="review-changes-requested:   fix the null   deref  ",
            kind="needs_input",
        )
        assert kb.get_task(conn, tid).status == "triage"


def test_review_bounce_distinct_then_same_escalates(kanban_home: Path) -> None:
    """Progress resets the counter; a later repeat of that finding trips."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(
            conn, tid,
            reason="review-changes-requested: finding A",
            kind="needs_input",
        )
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        # Distinct finding B -> reset to 1, stays blocked.
        kb.block_task(
            conn, tid,
            reason="review-changes-requested: finding B",
            kind="needs_input",
        )
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.get_task(conn, tid).block_recurrences == 1
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        # Finding B repeats unfixed -> now a real loop.
        kb.block_task(
            conn, tid,
            reason="review-changes-requested: finding B",
            kind="needs_input",
        )
        t = kb.get_task(conn, tid)
        assert t.status == "triage"
        assert t.block_recurrences == 2


def test_non_review_same_reason_loop_still_escalates(kanban_home: Path) -> None:
    """Regression guard: a non-review needs_input loop still trips even when
    the reason text changes -- the material-difference reset is review-only."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="need the API key", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        # A NON-review re-block with a DIFFERENT reason but the same kind must
        # still count -- only review bounces get the distinct-finding reset.
        kb.block_task(conn, tid, reason="need a different secret", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "triage"
        assert t.block_recurrences == 2


# ---------------------------------------------------------------------------
# Dependency routing
# ---------------------------------------------------------------------------


def test_dependency_block_routes_to_todo(kanban_home: Path) -> None:
    """Dependency waits never enter the human 'blocked' bucket."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="need X first", kind="dependency")
        t = kb.get_task(conn, tid)
        assert t.status == "todo"
        assert t.block_kind == "dependency"


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


def test_completion_clears_block_memory(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).block_recurrences == 1
        kb.complete_task(conn, tid, result="done")
        t = kb.get_task(conn, tid)
        assert t.status == "done"
        assert t.block_recurrences == 0
        assert t.block_kind is None


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------


def test_invalid_kind_rejected(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        with pytest.raises(ValueError):
            kb.block_task(conn, tid, reason="x", kind="bogus")


def test_block_without_kind_is_backward_compatible(kanban_home: Path) -> None:
    """Existing callers that pass no kind keep the old single-block behaviour."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="legacy")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind is None
