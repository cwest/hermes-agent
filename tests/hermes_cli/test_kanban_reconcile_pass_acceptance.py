"""Reconcile a PASS'd card into the atomic acceptance lane (``blocked`` + casey).

Card t_cc36de51. The PASS -> acceptance transition must be ATOMIC: a card Lamport
PASSes lands in Casey's lane as ``status=blocked`` AND ``assignee=casey`` together,
with a ``blocked`` event emitted (that event is what fires the acceptance
notification — ``gateway/kanban_watchers.py`` pings on ``kind == "blocked"``).

The root-cause bug this pins: on the PR path the reviewer was documented to
``kanban_block`` and let a (non-existent) orchestrator hop ``assign casey``. In
practice the OPPOSITE half fired — cards got ``assigned {to: casey}`` with NO
``block`` event, stranding them at ``review``/casey: Lamport's lane with Casey's
name on it. That state is contradictory AND silent (no ``blocked`` event -> the
acceptance ping never fires). The primary fix is the ``sdlc-review`` skill making
the reviewer do BOTH halves itself. THIS test pins the board-internal safety net:
a housekeeping reconciler repairs a stray ``review``/casey card to the atomic
``blocked``/casey acceptance state, emitting the ``blocked`` event — so the
invariant holds even if a reviewer (or a stale orchestrator hop) drops half the
transition.

The invariant, stated once: **a PASS'd card in Casey's lane is ``blocked`` AND
``casey``, atomically, with a ``blocked``/``awaiting-casey-signoff`` event.**
``assign casey`` without ``block`` = wrong lane, silent -> reconcile.
"""

from __future__ import annotations

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


_PR_URL = "https://github.com/cwest/hermes-agent/pull/71"
# The reviewer's PASS audit note carried on the acceptance transition.
_SIGNOFF_GIST = (
    f"awaiting-casey-signoff: reviewed PASS — {_PR_URL}; threads resolved; "
    "240 tests green. Ready to merge."
)


def _stage_review_with_reviewer(conn, *, author: str = "eckert",
                                reviewer: str = "lamport") -> str:
    """Reproduce the live flow up to the card sitting in ``review`` + reviewer.

    Author builds + opens PR -> card MOVES to ``review`` + reviewer (the
    ``assigned`` event carries ``from=author, to=reviewer``). Returns the card id
    in ``review`` (the lane, not yet claimed to running) — the state the stray-lane
    bug decorates with an ``assign casey`` that forgets the ``block``.
    """
    tid = kb.create_task(conn, title="feature work", assignee=author)
    kb.claim_task(conn, tid)
    kb.complete_task(conn, tid, result=f"PR opened: {_PR_URL}")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='review', assignee=? WHERE id=?", (reviewer, tid)
        )
        kb._append_event(
            conn, tid, "status_changed",
            {"from": "ready", "to": "review", "by": "onecard:move_card"},
        )
        kb._append_event(
            conn, tid, "assigned",
            {"from": author, "to": reviewer, "by": "onecard:move_card"},
        )
    task = kb.get_task(conn, tid)
    assert task.status == "review" and task.assignee == reviewer
    return tid


def _stage_wrong_lane_review_casey(conn, *, author: str = "eckert",
                                   reviewer: str = "lamport") -> str:
    """Reproduce the BUG state: a PASS that did ``assign casey`` WITHOUT ``block``.

    The card ends ``review``/casey — Lamport's lane with Casey's name — and NO
    ``blocked`` event ever fired, so the acceptance ping never triggered. This is
    the exact stranded state live cards hit this session. Returns the card id.
    """
    tid = _stage_review_with_reviewer(conn, author=author, reviewer=reviewer)
    # The reviewer PASS'd and (per the broken hop) reassigned to casey without a
    # status flip: status stays 'review', assignee becomes 'casey'. The PASS gist
    # is recorded as an audit comment (mirrors the §9.1 audit shape).
    kb.add_comment(conn, tid, author="lamport", body=f"[audit] status=PASS {_SIGNOFF_GIST}")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET assignee='casey' WHERE id=?", (tid,))
        kb._append_event(
            conn, tid, "assigned",
            {"from": reviewer, "to": "casey", "by": "reviewer:pass"},
        )
    task = kb.get_task(conn, tid)
    assert task.status == "review" and task.assignee == "casey", "staged the bug state"
    return tid


# ---------------------------------------------------------------------------
# RED 1 — a stray review/casey card is reconciled to atomic blocked/casey
# ---------------------------------------------------------------------------


def test_wrong_lane_review_casey_reconciled_to_blocked_casey(kanban_home: Path) -> None:
    """A card stranded at ``review``/casey (assign-casey WITHOUT block) must be
    repaired to ``blocked``/casey ATOMICALLY, emitting the ``blocked`` event that
    fires the acceptance ping."""
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 1, "exactly one stray card should reconcile"
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "card must land in the acceptance lane (blocked)"
        assert task.assignee == "casey", "card must stay with casey"
        # A ``blocked`` event MUST be emitted — that is what fires the acceptance
        # notification (kanban_watchers pings on kind == 'blocked').
        events = kb.list_events(conn, tid)
        blocked = [e for e in events if e.kind == "blocked"]
        assert blocked, "a 'blocked' event is required to fire the acceptance ping"
        # The sticky block carries the awaiting-casey-signoff reason.
        reason = kb._latest_sticky_block_reason(conn, tid)
        assert reason and "awaiting-casey-signoff" in reason, \
            "the acceptance block reason must be awaiting-casey-signoff"


def test_reconcile_emits_audit_comment_naming_pr(kanban_home: Path) -> None:
    """The reconcile must leave an audit trail naming the PR so the lane and the
    trail are honest."""
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)
        kb.reconcile_pass_acceptance(conn)
        comments = kb.list_comments(conn, tid)
        audit = [c for c in comments
                 if (c.body or "").lstrip().startswith("[audit]")
                 and "awaiting-casey-signoff" in (c.body or "")]
        assert audit, "an audit comment recording the acceptance reconcile is required"
        assert any("pull/71" in (c.body or "") for c in comments), \
            "the audit comment must name the PR"


# ---------------------------------------------------------------------------
# RED 2 — idempotency
# ---------------------------------------------------------------------------


def test_correct_blocked_casey_card_is_untouched(kanban_home: Path) -> None:
    """A card ALREADY correctly at ``blocked``/casey (the reviewer did both halves)
    must NOT be reconciled — it's already in the right lane."""
    with kb.connect() as conn:
        tid = _stage_review_with_reviewer(conn)
        # Reviewer claims the review card, then does it right: clean block THEN
        # assign casey (both halves).
        rt = kb.claim_review_task(conn, tid)
        assert rt is not None and rt.status == "running"
        assert kb.block_task(
            conn, tid, reason=_SIGNOFF_GIST,
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee='casey' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "assigned",
                             {"from": "lamport", "to": "casey", "by": "reviewer:pass"})
        assert kb.get_task(conn, tid).status == "blocked"

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 0, "a correct blocked/casey card needs no reconcile"
        task = kb.get_task(conn, tid)
        assert task.status == "blocked" and task.assignee == "casey"


def test_reconcile_is_idempotent_across_ticks(kanban_home: Path) -> None:
    """Two consecutive housekeeping ticks reconcile the stray card exactly once."""
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)

        first = kb.reconcile_pass_acceptance(conn)
        second = kb.reconcile_pass_acceptance(conn)

        assert first == 1
        assert second == 0, "second tick must not re-reconcile the fixed card"
        comments = kb.list_comments(conn, tid)
        audit = [c for c in comments
                 if (c.body or "").lstrip().startswith("[audit]")
                 and "awaiting-casey-signoff" in (c.body or "")
                 and "reconcile" in (c.body or "").lower()]
        assert len(audit) == 1, f"exactly one reconcile audit comment, got {len(audit)}"


# ---------------------------------------------------------------------------
# RED 3 — a genuine review card (still under review) must NOT be reconciled
# ---------------------------------------------------------------------------


def test_review_card_with_reviewer_is_untouched(kanban_home: Path) -> None:
    """A card genuinely under review (``review``/lamport, reviewer still holding it)
    must NOT be swept into acceptance — only the casey-owned stray is a bug."""
    with kb.connect() as conn:
        tid = _stage_review_with_reviewer(conn)  # review + lamport, still running
        assert kb.get_task(conn, tid).assignee == "lamport"

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 0, "an in-flight review card must stay put"
        task = kb.get_task(conn, tid)
        assert task.status == "review" and task.assignee == "lamport"


def test_review_card_with_reviewer_and_pass_comment_is_untouched(
    kanban_home: Path,
) -> None:
    """A card still owned by its reviewer (``review``/lamport) that ALSO carries an
    ``awaiting-casey-signoff`` PASS comment must NOT be reconciled to
    ``blocked``/lamport.

    This is the case the guard has to exclude: the reviewer posted the PASS gist
    but has not yet done the ``assign`` half, so the card is still legitimately in
    the reviewer's hands. Reconciling here would produce ``blocked``/lamport — a
    block with no acceptance owner, the exact wrong-lane state this reconciler
    exists to prevent. The owner resolution must key on a reassignment that moved
    the card OFF the reviewer (``from == reviewer``, ``to != reviewer``); with no
    such move, there is no acceptance owner and the card is left alone.
    """
    with kb.connect() as conn:
        tid = _stage_review_with_reviewer(conn)  # review + lamport
        # The PASS gist is recorded, but the card is STILL owned by the reviewer —
        # no assign-off-reviewer move has happened.
        kb.add_comment(
            conn, tid, author="lamport", body=f"[audit] status=PASS {_SIGNOFF_GIST}"
        )
        assert kb.get_task(conn, tid).assignee == "lamport"

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 0, (
            "a review card still owned by its reviewer must not be reconciled, "
            "even with a PASS comment present"
        )
        task = kb.get_task(conn, tid)
        assert task.status == "review" and task.assignee == "lamport", (
            "the card must stay review/lamport, never become blocked/lamport"
        )
        blocked = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"]
        assert not blocked, "no blocked event may fire for an in-flight review card"


# ---------------------------------------------------------------------------
# RED 4 — fires inside the real housekeeping tick
# ---------------------------------------------------------------------------


def test_reconcile_runs_via_dispatch_once(
    kanban_home: Path, all_assignees_spawnable
) -> None:
    """The reconcile must fire inside ``dispatch_once`` (the housekeeping tick),
    not only when called directly. After the tick the stray card is blocked/casey
    (a blocked card is not dispatchable, so it stays parked for Casey)."""
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)
        result = kb.dispatch_once(conn, spawn_fn=lambda *_: 4321, dry_run=False)
        assert getattr(result, "reconciled_pass_acceptance", 0) == 1
        task = kb.get_task(conn, tid)
        assert task.status == "blocked" and task.assignee == "casey"


# ---------------------------------------------------------------------------
# RED 5 — config toggle off disables the reconcile
# ---------------------------------------------------------------------------


def test_reconcile_disabled_by_flag(kanban_home: Path) -> None:
    """``kanban.reconcile_pass_acceptance: false`` leaves the stray card as-is."""
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)
        reconciled = kb.reconcile_pass_acceptance(conn, enabled=False)
        assert reconciled == 0
        task = kb.get_task(conn, tid)
        assert task.status == "review" and task.assignee == "casey"
