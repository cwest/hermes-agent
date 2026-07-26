"""Auto-promote a no-PR ``review-required`` block into the review lane.

Card t_cefbb7b3. An edit-in-place (no-PR) card has no path out of its review
handoff. When a worker on a ``~/.hermes`` / ``cwest/hermes-config`` card finishes
and correctly calls ``kanban_block(reason="review-required: ...")``, the block is
recorded ``kind=needs_input`` — "truly blocked" — and NOTHING in the dispatcher
looks at the ``review-required:`` reason prefix, so no reviewer is ever spawned
and the card parks in ``blocked`` indefinitely, assigned to its author.

This is the edit-in-place analog of the PR-backed ``running -> review`` handoff
(which a ``github-prs`` webhook fires). The no-PR path has no equivalent trigger.

These tests pin the board-internal fix: on the housekeeping tick, a ``blocked``
card whose most-recent sticky block carries the ``review-required`` prefix, whose
assignee is the author, and whose author worker pid is DEAD, is MOVED
``blocked -> review`` + assignee = the card's owner-map reviewer, with an audit
comment, exactly once. An acceptance ``awaiting-casey-signoff`` block, a
``review-changes-requested`` bounce, a genuine ``needs_input`` decision hold, a
card with a LIVE author pid, a card already in ``review``, and a card with the
feature toggled off must all stay put.
"""

from __future__ import annotations

import os
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


# Mirrors the live worker contract emitted on a no-PR review handoff:
#   kanban_block(reason="review-required: <gist>", kind="needs_input")
_REVIEW_REQUIRED_REASON = (
    "review-required: edited ~/.hermes/skills/homestead/foo/SKILL.md; "
    "self-verified green (12/12 tests, ruff clean). No PR — edit-in-place card. "
    "Ready for review."
)
# The acceptance (PASS) block — parked for Casey, must NOT promote to review.
_SIGNOFF_REASON = (
    "awaiting-casey-signoff: reviewed PASS — threads resolved; 240 tests green. "
    "Ready to merge."
)
# The reviewer's changes-requested bounce — already routed by
# ``auto_route_review_bounce``, must NOT be touched by this handler.
_BOUNCE_REASON = (
    "review-changes-requested: fix the edge case on line 42; re-review after."
)
# A genuine human-decision hold — carries a decision fork, NOT ``review-required``.
_DECISION_REASON = (
    "needs_input: two viable approaches (A vs B) — which should I take?"
)


def _stamp_owner_map(conn, tid: str, *, ready: str = "eckert",
                     review: str = "lamport", acceptance: str = "casey") -> None:
    """Stamp the card's submit-stage owner map, mirroring the live hollis audit.

    The per-lane owner map lives in the ``stage=submit`` §9.1 audit comment
    (there is no ``state_owners`` column). The one-card lane resolvers read
    ``state_owners[<lane>]`` from here.
    """
    body = (
        "[audit] actor=hollis stage=submit ts=2026-07-26T13:55:30Z\n"
        f"notes: state_owners={{ready: {ready}, review: {review}, "
        f"blocked-acceptance: {acceptance}}} triager=hollis team=engineering"
    )
    kb.add_comment(conn, tid, author="hollis", body=body)


def _stage_no_pr_review_block(
    conn, *, author: str = "eckert", reviewer: str = "lamport",
    reason: str = _REVIEW_REQUIRED_REASON, kind: str = "needs_input",
    stamp_owner_map: bool = True, dead_pid: bool = True,
) -> str:
    """Reproduce a no-PR card up to (and including) the worker's review handoff.

    Author claims + works the edit-in-place card, then emits the clean
    ``kanban_block(reason="review-required: ...")`` handoff. Returns the card id
    left in ``blocked`` + author, exactly as the dispatcher would see it.

    ``block_task`` clears ``tasks.worker_pid``, so the author's spawned pid lives
    only in the ``spawned`` event. We stamp a dead pid there by default so the
    handler's liveness guard (author-pid must be DEAD) passes; ``dead_pid=False``
    stamps THIS process's live pid to exercise the live-pid guard.
    """
    tid = kb.create_task(conn, title="edit-in-place fix", assignee=author)
    if stamp_owner_map:
        _stamp_owner_map(conn, tid, ready=author, review=reviewer)
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    # Record the author's spawned pid (a pid the dispatcher would have set via
    # _set_worker_pid). A pid of 2^31-1 is guaranteed absent; this process's own
    # pid models a still-alive author worker.
    pid = os.getpid() if not dead_pid else 2_147_483_646
    kb._set_worker_pid(conn, tid, pid)
    # The worker's clean no-PR review handoff.
    assert kb.block_task(
        conn, tid, reason=reason, kind=kind,
        expected_run_id=kb.get_task(conn, tid).current_run_id,
    )
    assert kb.get_task(conn, tid).status == "blocked"
    return tid


# ---------------------------------------------------------------------------
# RED 1 — the core auto-promotion
# ---------------------------------------------------------------------------


def test_review_required_block_auto_promotes_to_review(kanban_home: Path) -> None:
    """A ``review-required`` block must MOVE the SAME card ``blocked -> review`` +
    assignee = the owner-map reviewer on the housekeeping tick, with an audit
    comment. Fails today: the card stays ``blocked`` + author forever."""
    with kb.connect() as conn:
        tid = _stage_no_pr_review_block(conn)

        promoted = kb.auto_promote_no_pr_review(conn)

        assert promoted == 1, "exactly one card should auto-promote"
        task = kb.get_task(conn, tid)
        assert task.status == "review", "card must land in the review lane"
        assert task.assignee == "lamport", "assignee must be the owner-map reviewer"
        # Audit comment authored as the dispatcher naming the no-PR handoff.
        bodies = [c.body or "" for c in kb.list_comments(conn, tid)]
        assert any(
            "actor=dispatcher" in b and "review-required" in b.lower()
            for b in bodies
        ), "an audit comment must record the dispatcher's no-PR review promotion"


def test_promotion_emits_assigned_event_to_reviewer(kanban_home: Path) -> None:
    """The move must emit an ``assigned`` event carrying ``from=author,
    to=reviewer`` so downstream review-lane machinery can resolve the hop."""
    with kb.connect() as conn:
        tid = _stage_no_pr_review_block(conn)
        kb.auto_promote_no_pr_review(conn)
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'assigned' "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchall()
        assert rows, "an assigned event must be emitted"
        import json
        payload = json.loads(rows[0]["payload"])
        assert payload.get("from") == "eckert"
        assert payload.get("to") == "lamport"


# ---------------------------------------------------------------------------
# RED 2 — the guard rails (must NOT promote)
# ---------------------------------------------------------------------------


def test_acceptance_signoff_block_is_not_promoted(kanban_home: Path) -> None:
    """A ``awaiting-casey-signoff`` block is the acceptance gate — Casey's lane.
    It must NEVER be promoted to review."""
    with kb.connect() as conn:
        tid = _stage_no_pr_review_block(conn, reason=_SIGNOFF_REASON)
        promoted = kb.auto_promote_no_pr_review(conn)
        assert promoted == 0
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "acceptance park must stay blocked"


def test_review_changes_requested_block_is_not_promoted(kanban_home: Path) -> None:
    """A ``review-changes-requested`` bounce is routed by ``auto_route_review_bounce``,
    NOT this handler. This handler must leave it alone."""
    with kb.connect() as conn:
        tid = _stage_no_pr_review_block(conn, reason=_BOUNCE_REASON)
        promoted = kb.auto_promote_no_pr_review(conn)
        assert promoted == 0
        assert kb.get_task(conn, tid).status == "blocked"


def test_generic_needs_input_hold_is_not_promoted(kanban_home: Path) -> None:
    """A genuine human-decision ``needs_input`` hold (no ``review-required`` prefix)
    must stay blocked for a human — it is NOT a review handoff."""
    with kb.connect() as conn:
        tid = _stage_no_pr_review_block(conn, reason=_DECISION_REASON)
        promoted = kb.auto_promote_no_pr_review(conn)
        assert promoted == 0
        assert kb.get_task(conn, tid).status == "blocked"


def test_live_author_pid_is_not_promoted(kanban_home: Path) -> None:
    """A ``review-required`` block whose author worker pid is STILL ALIVE means the
    worker is mid-wrapup; promoting under it is the stale-lock wedge. Must NOT
    promote while the pid is alive."""
    with kb.connect() as conn:
        tid = _stage_no_pr_review_block(conn, dead_pid=False)
        promoted = kb.auto_promote_no_pr_review(conn)
        assert promoted == 0, "must not promote under a live author pid"
        assert kb.get_task(conn, tid).status == "blocked"


def test_unresolvable_reviewer_is_left_for_a_human(kanban_home: Path) -> None:
    """When the card has no owner-map reviewer, the handler must leave it blocked
    for a human rather than guessing a reviewer."""
    with kb.connect() as conn:
        tid = _stage_no_pr_review_block(conn, stamp_owner_map=False)
        promoted = kb.auto_promote_no_pr_review(conn)
        assert promoted == 0
        assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# RED 3 — idempotency + toggle
# ---------------------------------------------------------------------------


def test_promotion_is_idempotent(kanban_home: Path) -> None:
    """Two consecutive ticks produce exactly one promotion and one audit comment;
    a card already in ``review`` is untouched."""
    with kb.connect() as conn:
        tid = _stage_no_pr_review_block(conn)
        first = kb.auto_promote_no_pr_review(conn)
        second = kb.auto_promote_no_pr_review(conn)
        assert first == 1
        assert second == 0, "second tick must be a no-op"
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        promo_comments = [
            c for c in kb.list_comments(conn, tid)
            if "actor=dispatcher" in (c.body or "")
            and "review-required" in (c.body or "").lower()
        ]
        assert len(promo_comments) == 1, "exactly one promotion audit comment"


def test_disabled_toggle_is_a_no_op(kanban_home: Path) -> None:
    """``enabled=False`` (config ``kanban.auto_promote_no_pr_review: false``) makes
    the handler a no-op — the card stays blocked."""
    with kb.connect() as conn:
        tid = _stage_no_pr_review_block(conn)
        promoted = kb.auto_promote_no_pr_review(conn, enabled=False)
        assert promoted == 0
        assert kb.get_task(conn, tid).status == "blocked"


def test_dispatch_once_runs_promotion(kanban_home: Path) -> None:
    """A full ``dispatch_once`` tick must run the promotion and report it on the
    DispatchResult (integration through the real dispatch pass, not just the unit
    function)."""
    with kb.connect() as conn:
        tid = _stage_no_pr_review_block(conn)
        # No-op spawn so the tick doesn't try to launch a real worker.
        result = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, dry_run=True)
        assert result.promoted_no_pr_review == 1
        assert kb.get_task(conn, tid).status == "review"
