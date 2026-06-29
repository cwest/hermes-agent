"""Auto-route a review-bounce block back to the author (close the reviewer->author hop).

Card t_cec2251c. The review loop is asymmetric: the author advances his own card
into ``review`` + reviewer, but the reviewer's only sanctioned terminal action on a
bounce is a clean ``kanban_block(reason="review-changes-requested: ...")`` -- he is
forbidden from reassigning (that historically corrupted the lane). The intended
automation that closes the gap (the ``bounce-review-to-author`` GitHub webhook)
cannot fire when reviewer and author share one GitHub identity (a ``COMMENT`` event,
not ``changes_requested``, so the router never bounces). The card then sits
``blocked`` until a human hand-routes it.

These tests pin the board-internal fix: on the housekeeping tick, a ``blocked`` card
whose most-recent sticky block carries the ``review-changes-requested`` contract is
auto-routed ``blocked -> ready`` + assignee = the original author, with an audit
comment, exactly once, and genuinely dispatchable (respawn guards cleared). The
acceptance block (``awaiting-casey-signoff``), a non-review block, and a card with
the feature toggled off must all stay put.
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


# Mirrors the live reviewer contract emitted by the sdlc-review skill on a bounce:
#   kanban_block(reason="review-changes-requested: <gist>; see PR <url>. ...")
_BOUNCE_REASON = (
    "review-changes-requested: PATCHES.md:75 row bucketed upstream-pending but "
    "should be permanent-local; see https://github.com/cwest/hermes-agent/pull/71. "
    "Author to rework on the same branch/PR and resolve every open thread before "
    "re-review."
)
# The acceptance (PASS) block -- parked for Casey, must NOT route to the author.
_SIGNOFF_REASON = (
    "awaiting-casey-signoff: reviewed PASS — "
    "https://github.com/cwest/hermes-agent/pull/71; threads resolved; 240 tests "
    "green. Ready to merge."
)


def _stage_review_bounce(conn, *, author: str = "eckert", reviewer: str = "lamport",
                         reason: str = _BOUNCE_REASON) -> str:
    """Reproduce the live flow up to (and including) the reviewer's terminal block.

    Author builds + opens PR -> card MOVES to ``review`` + reviewer (the ``assigned``
    event carries ``from=author, to=reviewer``) -> reviewer claims (``source_status:
    review``) -> reviewer emits the clean bounce ``kanban_block``. Returns the card id
    left in ``blocked`` + reviewer, exactly as the dispatcher would see it.
    """
    tid = kb.create_task(conn, title="feature work", assignee=author)
    # Author's build run.
    kb.claim_task(conn, tid)
    kb.complete_task(conn, tid, result="PR opened: https://github.com/cwest/hermes-agent/pull/71")
    # Card MOVES to review + reviewer (mirrors onecard move_card on PR-open).
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='review', assignee=? WHERE id=?", (reviewer, tid))
        kb._append_event(conn, tid, "status_changed",
                         {"from": "ready", "to": "review", "by": "onecard:move_card"})
        kb._append_event(conn, tid, "assigned",
                         {"from": author, "to": reviewer, "by": "onecard:move_card"})
    # Reviewer claims the review card, then bounces with a clean block.
    rt = kb.claim_review_task(conn, tid)
    assert rt is not None and rt.status == "running"
    assert kb.block_task(conn, tid, reason=reason,
                         expected_run_id=kb.get_task(conn, tid).current_run_id)
    assert kb.get_task(conn, tid).status == "blocked"
    # In production the build + review happen minutes before the housekeeping
    # tick that routes the bounce. Back-date the build's PR-handoff comment and
    # completed run so the routing unblock is causally LATER (not the same
    # second), modelling the real timeline rather than a compressed-test artifact.
    past = int(time.time()) - 300
    with kb.write_txn(conn):
        conn.execute("UPDATE task_comments SET created_at=? WHERE task_id=?", (past, tid))
        conn.execute("UPDATE task_runs SET ended_at=? WHERE task_id=? AND ended_at IS NOT NULL",
                     (past, tid))
    return tid


# ---------------------------------------------------------------------------
# RED 1 — the core auto-route
# ---------------------------------------------------------------------------


def test_review_bounce_block_auto_routes_to_author(kanban_home: Path) -> None:
    """A ``review-changes-requested`` block must auto-route the SAME card
    ``blocked -> ready`` + assignee = the original author on the housekeeping tick,
    with an audit comment naming the PR. Fails today: the card stays ``blocked``."""
    with kb.connect() as conn:
        tid = _stage_review_bounce(conn)

        routed = kb.auto_route_review_bounce(conn)

        assert routed == 1, "exactly one card should auto-route"
        task = kb.get_task(conn, tid)
        assert task.status == "ready", "card must be routed back to ready"
        assert task.assignee == "eckert", "card must be reassigned to the author"
        # An audit comment recording the auto-route, naming the PR.
        comments = kb.list_comments(conn, tid)
        audit = [c for c in comments if "review-changes-requested" in (c.body or "")
                 or "auto-route" in (c.body or "").lower()]
        assert audit, "an audit comment recording the auto-route is required"
        assert any("pull/71" in (c.body or "") for c in comments), \
            "the audit comment must name the PR"


def test_review_bounce_routed_via_dispatch_once(
    kanban_home: Path, all_assignees_spawnable
) -> None:
    """The auto-route must fire inside the real housekeeping tick (``dispatch_once``),
    not only when called directly."""
    with kb.connect() as conn:
        tid = _stage_review_bounce(conn)
        kb.dispatch_once(conn, spawn_fn=lambda *_: 4321, dry_run=False)
        task = kb.get_task(conn, tid)
        # Routed AND dispatchable: the tick reassigns to the author and the card
        # leaves the blocked lane (it is claimed to running by the same tick's
        # spawn, since the respawn guards are cleared).
        assert task.status in ("ready", "running")
        assert task.assignee == "eckert"


# ---------------------------------------------------------------------------
# RED 2 — acceptance (PASS) block must NOT route
# ---------------------------------------------------------------------------


def test_awaiting_casey_signoff_block_does_not_route(kanban_home: Path) -> None:
    """The PASS / acceptance block (``awaiting-casey-signoff``) is parked for Casey
    and must STAY ``blocked`` -- never routed to the author."""
    with kb.connect() as conn:
        tid = _stage_review_bounce(conn, reviewer="lamport", reason=_SIGNOFF_REASON)
        # The acceptance lane assigns casey; mirror that the reviewer parked it.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee='casey' WHERE id=?", (tid,))

        routed = kb.auto_route_review_bounce(conn)

        assert routed == 0
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "acceptance block must stay blocked"
        assert task.assignee == "casey", "acceptance block must stay with casey"


# ---------------------------------------------------------------------------
# RED 3 — idempotency: two ticks -> one route, one comment
# ---------------------------------------------------------------------------


def test_auto_route_is_idempotent_across_ticks(kanban_home: Path) -> None:
    """The housekeeping tick runs repeatedly. The auto-route must fire EXACTLY ONCE
    per block: two consecutive ticks produce one route and one audit comment."""
    with kb.connect() as conn:
        tid = _stage_review_bounce(conn)

        first = kb.auto_route_review_bounce(conn)
        second = kb.auto_route_review_bounce(conn)

        assert first == 1
        assert second == 0, "second tick must not re-route the already-routed card"
        # Exactly one auto-route audit comment.
        comments = kb.list_comments(conn, tid)
        audit = [c for c in comments
                 if (c.body or "").lstrip().startswith("[audit]")
                 and "rework" in (c.body or "")]
        assert len(audit) == 1, f"exactly one audit comment expected, got {len(audit)}"


# ---------------------------------------------------------------------------
# RED 4 — routed card is genuinely dispatchable (respawn guard cleared)
# ---------------------------------------------------------------------------


def test_routed_card_is_dispatchable(
    kanban_home: Path, all_assignees_spawnable
) -> None:
    """Clearing to ``ready`` + author must clear the ``active_pr`` / ``recent_success``
    respawn guards (the card carries a PR-URL comment + a recent completed build run),
    so the dispatcher actually spawns the author on the next tick -- not a ``ready``
    card the guard silently skips."""
    with kb.connect() as conn:
        tid = _stage_review_bounce(conn)
        kb.auto_route_review_bounce(conn)

        # The guard must NOT veto the respawn of the routed card.
        assert kb.check_respawn_guard(conn, tid) is None, \
            "routed card must be free of respawn guards"

        # And the dispatcher actually spawns it.
        spawned: list[str] = []
        kb.dispatch_once(conn, spawn_fn=lambda task, *a: spawned.append(task.id) or 4321)
        assert tid in spawned, "routed card must be spawned by the dispatcher"


# ---------------------------------------------------------------------------
# RED 5 — a non-review block (circuit-breaker / arbitrary) must NOT route
# ---------------------------------------------------------------------------


def test_non_review_block_does_not_route(kanban_home: Path) -> None:
    """A block whose reason is NOT a review-changes-requested bounce (e.g. a generic
    operator block) must be left untouched by the auto-router."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="needs human", assignee="eckert")
        kb.claim_task(conn, tid)
        kb.block_task(conn, tid, reason="review-required: please verify ACL change",
                      expected_run_id=kb.get_task(conn, tid).current_run_id)

        routed = kb.auto_route_review_bounce(conn)

        assert routed == 0
        assert kb.get_task(conn, tid).status == "blocked"


def test_circuit_breaker_block_does_not_route(kanban_home: Path) -> None:
    """A circuit-breaker block (``gave_up`` event, no ``blocked`` event) must not be
    treated as a review bounce."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="flaky", assignee="eckert")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'gave_up', NULL, ?)", (tid, int(time.time())))

        routed = kb.auto_route_review_bounce(conn)

        assert routed == 0
        assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# RED 6 — config toggle off disables the auto-route
# ---------------------------------------------------------------------------


def test_auto_route_disabled_by_flag(kanban_home: Path) -> None:
    """``kanban.auto_route_review_bounce: false`` must leave the bounce block parked."""
    with kb.connect() as conn:
        tid = _stage_review_bounce(conn)
        routed = kb.auto_route_review_bounce(conn, enabled=False)
        assert routed == 0
        assert kb.get_task(conn, tid).status == "blocked"
