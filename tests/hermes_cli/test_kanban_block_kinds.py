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
# Acceptance (awaiting-casey-signoff) parks must NOT count toward the loop
# ---------------------------------------------------------------------------
#
# A clean bounce -> rework -> PASS -> acceptance cycle re-blocks the card with
# the SAME ``kind`` as the earlier review bounce, but the acceptance block is a
# human sign-off park (``awaiting-casey-signoff: …``), NOT a failure repeating.
# The pure ``prev_kind == kind`` classifier counted the acceptance park as
# "the same failure again" and escalated a cleanly-accepted card to phantom
# ``triage`` at the limit. The fix: an ``awaiting-casey-signoff`` block never
# increments the loop counter (it resets to 1 and always lands in ``blocked``),
# while genuine failure/rework blocks still escalate.


def test_acceptance_after_bounce_does_not_route_to_triage(
    kanban_home: Path,
) -> None:
    """bounce -> rework -> PASS -> acceptance must stay blocked, never triage."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        # Round 1: a genuine review bounce (counter -> 1).
        kb.block_task(
            conn, tid,
            reason="review-changes-requested: fix the null deref in foo()",
            kind="needs_input",
        )
        assert kb.get_task(conn, tid).block_recurrences == 1
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        # PASS: park for Casey's sign-off with the SAME kind as the bounce.
        kb.block_task(
            conn, tid,
            reason="awaiting-casey-signoff: PR #12 PASS'd; awaiting Casey merge.",
            kind="needs_input",
        )
        t = kb.get_task(conn, tid)
        assert t.status == "blocked", (
            "a clean acceptance park must never escalate to triage"
        )
        assert t.block_recurrences == 1, (
            "an acceptance sign-off block must not count toward the loop"
        )


def test_repeated_acceptance_parks_never_escalate(kanban_home: Path) -> None:
    """Multiple acceptance parks in a row (outer feedback laps) never triage."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        for i in range(3):
            if i > 0:
                _make_running_again(conn, tid)
            kb.block_task(
                conn, tid,
                reason=f"awaiting-casey-signoff: PR PASS'd, lap {i}.",
                kind="needs_input",
            )
            t = kb.get_task(conn, tid)
            assert t.status == "blocked", f"acceptance lap {i} must stay blocked"
            assert t.block_recurrences == 1, (
                f"acceptance lap {i} must not accumulate loop count"
            )
            kb.unblock_task(conn, tid)


def test_acceptance_park_does_not_reset_a_prior_failure_loop_to_escalation(
    kanban_home: Path,
) -> None:
    """An acceptance park between failure blocks does not itself cause triage,
    and a genuine failure re-block AFTER it still escalates on its own merits."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        # Genuine failure block (counter -> 1).
        kb.block_task(conn, tid, reason="need creds", kind="needs_input")
        assert kb.get_task(conn, tid).block_recurrences == 1
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        # Acceptance park in between must NOT trip the breaker.
        kb.block_task(
            conn, tid,
            reason="awaiting-casey-signoff: PR PASS'd.",
            kind="needs_input",
        )
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.get_task(conn, tid).block_recurrences == 1
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        # A real same-cause failure re-block escalates on its own (counter -> 2).
        kb.block_task(conn, tid, reason="still need creds", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "triage", (
            "a genuine same-cause failure loop must still escalate"
        )
        assert t.block_recurrences == 2


def test_genuine_failure_loop_still_escalates_regression(
    kanban_home: Path,
) -> None:
    """Regression guard: the acceptance carve-out must not weaken the breaker
    for genuine repeated failure blocks."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="need the API key", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="still no API key", kind="capability")
        t = kb.get_task(conn, tid)
        assert t.status == "triage"
        assert t.block_recurrences == 2


# ---------------------------------------------------------------------------
# Review-required handoffs must NOT count toward the loop
# ---------------------------------------------------------------------------
#
# A ``review-required`` block is the sanctioned no-PR / re-review HANDOFF token
# (routed by ``auto_promote_no_pr_review`` into the review lane), NOT a failure
# repeating. A card that legitimately hands off twice (round-1 review, then a
# round-2 re-review after a rework) re-blocks with the SAME ``kind`` each time,
# so the pure ``prev_kind == kind`` classifier counted the second, entirely
# normal handoff as "the same failure again" and escalated a healthy card to
# phantom ``triage`` at the limit -- stranding it off the promoter (which only
# scans ``status = 'blocked'``). The fix mirrors the acceptance carve-out: a
# ``review-required`` block never increments the loop counter (it resets to 1
# and always lands in ``blocked``), while genuine failure/rework blocks and the
# reviewer's ``review-changes-requested`` bounce still escalate.


def test_review_required_second_handoff_does_not_route_to_triage(
    kanban_home: Path,
) -> None:
    """round-1 review -> rework -> round-2 re-review must stay blocked, never
    triage. This is the RED case: before the fix the second handoff escalated."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        # Round 1: a no-PR review handoff (counter -> 1).
        kb.block_task(
            conn, tid,
            reason="review-required: no-PR change ready for review round 1",
            kind="needs_input",
        )
        assert kb.get_task(conn, tid).block_recurrences == 1
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        # Round 2: a re-review handoff with the SAME kind as round 1.
        kb.block_task(
            conn, tid,
            reason="review-required (re-review round 2): PR #444 reworked",
            kind="needs_input",
        )
        t = kb.get_task(conn, tid)
        assert t.status == "blocked", (
            "a clean review-required handoff must never escalate to triage"
        )
        assert t.block_recurrences == 1, (
            "a review-required handoff must not count toward the loop"
        )


def test_repeated_review_required_handoffs_never_escalate(
    kanban_home: Path,
) -> None:
    """Multiple review-required handoffs in a row (rework laps) never triage."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        for i in range(3):
            if i > 0:
                _make_running_again(conn, tid)
            kb.block_task(
                conn, tid,
                reason=f"review-required (re-review round {i}): reworked",
                kind="needs_input",
            )
            t = kb.get_task(conn, tid)
            assert t.status == "blocked", f"review lap {i} must stay blocked"
            assert t.block_recurrences == 1, (
                f"review lap {i} must not accumulate loop count"
            )
            kb.unblock_task(conn, tid)


def test_review_changes_requested_still_escalates_regression(
    kanban_home: Path,
) -> None:
    """The regression that matters most: ``review-changes-requested`` (the
    reviewer's bounce) must KEEP participating in loop detection so a genuinely
    looping rework on the IDENTICAL finding still escalates. The review-required
    carve-out must not widen to the bounce prefix."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(
            conn, tid,
            reason="review-changes-requested: fix the null deref in foo()",
            kind="needs_input",
        )
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(
            conn, tid,
            reason="review-changes-requested: fix the null deref in foo()",
            kind="needs_input",
        )
        t = kb.get_task(conn, tid)
        assert t.status == "triage", (
            "an identical-finding review bounce is a genuine loop worth escalating"
        )
        assert t.block_recurrences == 2


def test_arbitrary_needs_input_still_escalates_regression(
    kanban_home: Path,
) -> None:
    """A stuck worker's free-text ``needs_input`` reason must still escalate --
    the carve-out must not weaken the loop breaker's actual purpose."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="which config key?", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="still which config key?", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "triage"
        assert t.block_recurrences == 2


def test_review_requiredness_does_not_match_word_boundary(
    kanban_home: Path,
) -> None:
    """Word-boundary coverage: a longer token that merely STARTS with the prefix
    (``review-requiredness``) must NOT be exempted -- it escalates like any other
    same-cause loop."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(
            conn, tid, reason="review-requiredness is a made-up word",
            kind="needs_input",
        )
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(
            conn, tid, reason="review-requiredness is a made-up word",
            kind="needs_input",
        )
        t = kb.get_task(conn, tid)
        assert t.status == "triage", (
            "review-requiredness must not match the review-required carve-out"
        )
        assert t.block_recurrences == 2


# ---------------------------------------------------------------------------
# Rework-complete handoffs must NOT count toward the loop
# ---------------------------------------------------------------------------
#
# A ``rework-complete`` block is the clean handoff a worker emits when it
# finishes a SUCCESSFUL rework round (review thread resolved, ready for
# re-review), NOT a failure repeating. A clean bounce -> rework -> rework-complete
# cycle re-blocks with the SAME ``kind`` as the earlier review bounce, so the pure
# ``prev_kind == kind`` classifier counted the clean rework handoff as "the same
# failure again" and escalated a healthy card to phantom ``triage`` at the limit.
# This is the same bug class already fixed for ``awaiting-casey-signoff`` and
# ``review-required``: a clean lifecycle signal must never trip the loop breaker,
# while genuine failure/rework blocks and the reviewer's ``review-changes-requested``
# bounce still escalate.


def test_rework_complete_after_bounce_does_not_route_to_triage(
    kanban_home: Path,
) -> None:
    """bounce -> rework -> rework-complete must stay blocked, never triage.

    This is the RED case: before the fix the clean rework handoff escalated.
    """
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        # Round 1: a genuine review bounce (counter -> 1).
        kb.block_task(
            conn, tid,
            reason="review-changes-requested: fix the snippet in the docs",
            kind="needs_input",
        )
        assert kb.get_task(conn, tid).block_recurrences == 1
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        # Clean rework handoff with the SAME kind as the bounce.
        kb.block_task(
            conn, tid,
            reason="rework-complete: PR #89 review thread resolved; ready for re-review.",
            kind="needs_input",
        )
        t = kb.get_task(conn, tid)
        assert t.status == "blocked", (
            "a clean rework-complete handoff must never escalate to triage"
        )
        assert t.block_recurrences == 1, (
            "a rework-complete handoff must not count toward the loop"
        )


def test_repeated_rework_complete_handoffs_never_escalate(
    kanban_home: Path,
) -> None:
    """Multiple rework-complete handoffs in a row (rework laps) never triage."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        for i in range(3):
            if i > 0:
                _make_running_again(conn, tid)
            kb.block_task(
                conn, tid,
                reason=f"rework-complete: lap {i} reworked; ready for re-review.",
                kind="needs_input",
            )
            t = kb.get_task(conn, tid)
            assert t.status == "blocked", f"rework lap {i} must stay blocked"
            assert t.block_recurrences == 1, (
                f"rework lap {i} must not accumulate loop count"
            )
            kb.unblock_task(conn, tid)


# ---------------------------------------------------------------------------
# Clean-lifecycle prefixes are defined in a SINGLE location
# ---------------------------------------------------------------------------


def test_clean_lifecycle_prefixes_single_source() -> None:
    """The exempt clean-lifecycle prefixes are defined in one place and cover,
    at minimum, the three known clean signals. This is the class-fix contract:
    a future clean prefix is added here once, not re-implemented as another
    one-off ``same_cause = False`` branch."""
    prefixes = set(kb._CLEAN_LIFECYCLE_REASON_PREFIXES)
    assert {
        "awaiting-casey-signoff",
        "review-required",
        "rework-complete",
    } <= prefixes


def test_is_clean_lifecycle_reason_matches_all_prefixes() -> None:
    """The single predicate exempts every clean-lifecycle prefix (whitespace
    tolerated, separator or end-of-string after the token) and nothing else."""
    for prefix in ("awaiting-casey-signoff", "review-required", "rework-complete"):
        assert kb._is_clean_lifecycle_reason(f"{prefix}: some detail")
        assert kb._is_clean_lifecycle_reason(f"  {prefix} some detail")
    # Failure reasons and word-boundary lookalikes are NOT clean lifecycle.
    assert not kb._is_clean_lifecycle_reason("need creds")
    assert not kb._is_clean_lifecycle_reason("review-changes-requested: fix x")
    assert not kb._is_clean_lifecycle_reason("review-requiredness is made up")
    assert not kb._is_clean_lifecycle_reason("rework-completeness matters")
    assert not kb._is_clean_lifecycle_reason(None)
    assert not kb._is_clean_lifecycle_reason("")


def test_rework_completeness_does_not_match_word_boundary(
    kanban_home: Path,
) -> None:
    """Word-boundary coverage: a longer token that merely STARTS with the prefix
    (``rework-completeness``) must NOT be exempted -- it escalates like any other
    same-cause loop."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(
            conn, tid, reason="rework-completeness is a made-up word",
            kind="needs_input",
        )
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(
            conn, tid, reason="rework-completeness is a made-up word",
            kind="needs_input",
        )
        t = kb.get_task(conn, tid)
        assert t.status == "triage", (
            "rework-completeness must not match the rework-complete carve-out"
        )
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
