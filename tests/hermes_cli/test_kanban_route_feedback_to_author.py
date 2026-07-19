"""The OUTER feedback loop: route an accepted card back to the author, in code.

Card t_2ea0aa35. The INNER review-bounce loop self-heals via
``auto_route_review_bounce`` (reviewer CHANGES -> author). The OUTER feedback
loop -- a human's (Casey's) feedback on an *accepted* card, routed back to the
author for a revision -- has NO code path that clears the ``active_pr`` respawn
guard, so it wedges every time and requires a hand-run block->unblock->reassign
dance. A raw ``move_card`` blocked/acceptance -> ready+author does NOT emit the
``unblocked`` cutoff event, so ``check_respawn_guard`` stays ``active_pr`` (the PR
is genuinely open) and the dispatcher refuses to spawn the author every tick.
Repeated churn inflates ``block_recurrences`` past ``BLOCK_RECURRENCE_LIMIT`` and
escalates the card to ``triage``.

These tests pin the sanctioned ``route_feedback_to_author`` primitive: routing an
open-PR card back to its author with feedback atomically resets an inflated loop
counter, reassigns the author, and emits the ``unblocked`` cutoff event AFTER the
PR-URL comment timestamp -- so ``check_respawn_guard`` clears and the next
``dispatch`` spawns the author, from ``blocked``, acceptance (``blocked``+casey),
AND ``triage``, every time, with no hand intervention. The INNER auto-router and
the reviewer-PASS -> acceptance / Casey-merge -> done paths stay untouched.
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


_PR_URL = "https://github.com/cwest/hermes-agent/pull/99"
# A human's outer-loop feedback reason -- NOT a review-changes-requested bounce
# and NOT an awaiting-casey-signoff park.
_FEEDBACK = "Casey feedback: tighten the intro and add a benchmark table before re-accepting."


def _stage_open_pr_card(
    conn,
    *,
    author: str = "eckert",
    acceptance_owner: str = "casey",
    status: str = "blocked",
    inflate_recurrences: int = 0,
) -> str:
    """Stage a card carrying an OPEN PR, parked in the acceptance/blocked/triage lane.

    Author builds + opens PR -> card MOVES to review + reviewer -> reviewer PASSes
    into the acceptance lane (``blocked`` + acceptance_owner). The PR-URL handoff
    comment and the completed build run are back-dated so a subsequent routing
    unblock is causally LATER (models the real timeline, not a same-second test
    artifact). ``inflate_recurrences`` pre-loads ``block_recurrences`` to model a
    card that churned; ``status`` sets the parked lane (``blocked`` / ``triage``).
    """
    tid = kb.create_task(conn, title="accepted work needing a revision", assignee=author)
    kb.claim_task(conn, tid)
    # The implementer posts the PR-URL handoff as a COMMENT (this is exactly what
    # check_respawn_guard's active_pr scan reads -- complete_task stores the result
    # on the task row, not as a comment, so the comment is the load-bearing signal).
    kb.add_comment(conn, tid, author=author,
                   body=f"[audit] actor={author} stage=implement pr={_PR_URL}\n"
                        f"notes: draft PR opened; ready for review.")
    kb.complete_task(conn, tid, result=f"PR opened: {_PR_URL}")
    # Card MOVES to review + reviewer (mirrors onecard move_card on PR-open).
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='review', assignee='lamport' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "status_changed",
                         {"from": "ready", "to": "review", "by": "onecard:move_card"})
        kb._append_event(conn, tid, "assigned",
                         {"from": author, "to": "lamport", "by": "onecard:move_card"})
    # Reviewer PASSes into the acceptance lane (blocked + acceptance_owner).
    assert kb.accept_task(conn, tid, acceptance_owner=acceptance_owner,
                          reason="reviewed PASS; ready to merge")
    task = kb.get_task(conn, tid)
    assert task.status == "blocked" and task.assignee == acceptance_owner
    # Back-date the PR-handoff comment + completed run so the routing unblock is
    # causally LATER than the PR URL (the active_pr cutoff is second-granular).
    past = int(time.time()) - 600
    with kb.write_txn(conn):
        conn.execute("UPDATE task_comments SET created_at=? WHERE task_id=?", (past, tid))
        conn.execute("UPDATE task_runs SET ended_at=? WHERE task_id=? AND ended_at IS NOT NULL",
                     (past, tid))
        if inflate_recurrences:
            conn.execute("UPDATE tasks SET block_recurrences=? WHERE id=?",
                         (inflate_recurrences, tid))
        if status != "blocked":
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
            kb._append_event(conn, tid, "status_changed",
                             {"from": "blocked", "to": status, "by": "loop-breaker"})
    return tid


# ---------------------------------------------------------------------------
# RED 1 — the core outer-loop route from the acceptance lane
# ---------------------------------------------------------------------------


def test_route_feedback_from_acceptance_clears_guard(kanban_home: Path) -> None:
    """Routing an accepted (``blocked``+casey) open-PR card back to the author must
    reassign the author, land it in a spawnable status, and emit the ``unblocked``
    cutoff event so ``check_respawn_guard`` clears -- every time, in code."""
    with kb.connect() as conn:
        tid = _stage_open_pr_card(conn, author="eckert", acceptance_owner="casey")

        # Guard is ACTIVE before the route: the card carries an open-PR comment
        # (active_pr) and a recent completed build run (recent_success), with no
        # trailing unblock cutoff. check_respawn_guard returns the first-matched
        # reason in priority order; either way the dispatcher would defer the spawn.
        assert kb.check_respawn_guard(conn, tid) in ("active_pr", "recent_success"), \
            "precondition: the open-PR card must be respawn-guarded"

        ok = kb.route_feedback_to_author(conn, tid, author="eckert", reason=_FEEDBACK)

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.assignee == "eckert", "card must be reassigned to the author"
        assert task.status in ("ready", "todo"), "card must land in a spawnable status"
        assert kb.check_respawn_guard(conn, tid) is None, \
            "the active_pr guard must be cleared by the emitted unblock cutoff"


# ---------------------------------------------------------------------------
# RED 2 — the routed card is genuinely dispatchable
# ---------------------------------------------------------------------------


def test_routed_card_is_dispatchable(kanban_home: Path, all_assignees_spawnable) -> None:
    """After the route, the dispatcher must actually spawn the author on the next
    tick -- assert via the real ``dispatch_once`` spawn path, not a live process."""
    with kb.connect() as conn:
        tid = _stage_open_pr_card(conn, author="eckert", acceptance_owner="casey")
        kb.route_feedback_to_author(conn, tid, author="eckert", reason=_FEEDBACK)

        spawned: list[str] = []
        kb.dispatch_once(conn, spawn_fn=lambda task, *a: spawned.append(task.id) or 4321)
        assert tid in spawned, "routed card must be spawned by the dispatcher"


# ---------------------------------------------------------------------------
# RED 3 — recovery from triage (churned card), counter reset
# ---------------------------------------------------------------------------


def test_route_feedback_recovers_triage_card(kanban_home: Path) -> None:
    """A card churned into ``triage`` with an inflated ``block_recurrences`` must be
    recovered by the primitive: counter reset, reassigned, guard cleared, spawnable."""
    with kb.connect() as conn:
        tid = _stage_open_pr_card(
            conn, author="eckert", acceptance_owner="casey",
            status="triage", inflate_recurrences=kb.BLOCK_RECURRENCE_LIMIT + 3,
        )

        ok = kb.route_feedback_to_author(conn, tid, author="eckert", reason=_FEEDBACK)

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status in ("ready", "todo"), "triage card must be recovered to a spawnable status"
        assert task.assignee == "eckert"
        # The inflated loop counter must be reset so the card does not immediately
        # re-escalate to triage on the next same-cause block.
        row = conn.execute(
            "SELECT block_recurrences FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert (row["block_recurrences"] or 0) == 0, "inflated loop counter must be reset"
        assert kb.check_respawn_guard(conn, tid) is None


# ---------------------------------------------------------------------------
# RED 4 — recovery from a plain blocked lane
# ---------------------------------------------------------------------------


def test_route_feedback_from_plain_blocked(kanban_home: Path) -> None:
    """A card parked in a plain ``blocked`` lane (not acceptance, not triage) also
    routes back to the author with the guard cleared."""
    with kb.connect() as conn:
        tid = _stage_open_pr_card(conn, author="eckert", acceptance_owner="casey",
                                  status="blocked")
        ok = kb.route_feedback_to_author(conn, tid, author="eckert", reason=_FEEDBACK)
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.assignee == "eckert"
        assert task.status in ("ready", "todo")
        assert kb.check_respawn_guard(conn, tid) is None


# ---------------------------------------------------------------------------
# RED 5 — an audit comment records the route, naming the PR + feedback
# ---------------------------------------------------------------------------


def test_route_feedback_writes_audit_comment(kanban_home: Path) -> None:
    """The route must leave an audit comment naming the PR and the feedback gist so
    the board trail is traceable."""
    with kb.connect() as conn:
        tid = _stage_open_pr_card(conn, author="eckert", acceptance_owner="casey")
        kb.route_feedback_to_author(conn, tid, author="eckert", reason=_FEEDBACK)

        comments = kb.list_comments(conn, tid)
        audit = [c for c in comments
                 if (c.body or "").lstrip().startswith("[audit]")
                 and "feedback" in (c.body or "").lower()]
        assert audit, "an audit comment recording the outer-loop route is required"
        assert any("pull/99" in (c.body or "") for c in comments), \
            "the audit comment must name the PR"


# ---------------------------------------------------------------------------
# RED 6 — idempotency: a second call on an already-routed card is a clean no-op
# ---------------------------------------------------------------------------


def test_route_feedback_is_idempotent(kanban_home: Path) -> None:
    """Once the card is routed off the blocked/triage lane, a second call must be a
    clean no-op (returns False, no duplicate audit comment) -- the card is already
    in the author's hands."""
    with kb.connect() as conn:
        tid = _stage_open_pr_card(conn, author="eckert", acceptance_owner="casey")

        first = kb.route_feedback_to_author(conn, tid, author="eckert", reason=_FEEDBACK)
        second = kb.route_feedback_to_author(conn, tid, author="eckert", reason=_FEEDBACK)

        assert first is True
        assert second is False, "a card already in a non-transitionable lane must not re-route"
        comments = kb.list_comments(conn, tid)
        audit = [c for c in comments
                 if (c.body or "").lstrip().startswith("[audit]")
                 and "feedback" in (c.body or "").lower()]
        assert len(audit) == 1, f"exactly one route audit comment expected, got {len(audit)}"


# ---------------------------------------------------------------------------
# RED 7 — the primitive requires an author (never guesses)
# ---------------------------------------------------------------------------


def test_route_feedback_requires_author(kanban_home: Path) -> None:
    """A blank author is a caller error, not a guess -- the primitive raises rather
    than routing a card into limbo."""
    with kb.connect() as conn:
        tid = _stage_open_pr_card(conn, author="eckert", acceptance_owner="casey")
        with pytest.raises(ValueError):
            kb.route_feedback_to_author(conn, tid, author="   ", reason=_FEEDBACK)


# ---------------------------------------------------------------------------
# RED 8 — a missing card is a clean no-op, not a crash
# ---------------------------------------------------------------------------


def test_route_feedback_missing_card_returns_false(kanban_home: Path) -> None:
    """Routing a non-existent card returns False (mirrors the other recovery
    primitives), never raises."""
    with kb.connect() as conn:
        assert kb.route_feedback_to_author(conn, "t_nope", author="eckert",
                                           reason=_FEEDBACK) is False


# ---------------------------------------------------------------------------
# Regression — the INNER auto-route path is untouched by this primitive
# ---------------------------------------------------------------------------


def test_inner_auto_route_still_works(kanban_home: Path) -> None:
    """Adding the outer-loop primitive must not change the INNER
    ``auto_route_review_bounce`` behavior: a review-changes-requested block still
    auto-routes to the author on the housekeeping tick."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="feature work", assignee="eckert")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result=f"PR opened: {_PR_URL}")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review', assignee='lamport' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "assigned",
                             {"from": "eckert", "to": "lamport", "by": "onecard:move_card"})
        rt = kb.claim_review_task(conn, tid)
        assert rt is not None
        kb.block_task(
            conn, tid,
            reason=f"review-changes-requested: fix the regex; see {_PR_URL}.",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        past = int(time.time()) - 300
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET created_at=? WHERE task_id=?", (past, tid))

        routed = kb.auto_route_review_bounce(conn)
        assert routed == 1
        assert kb.get_task(conn, tid).assignee == "eckert"
        assert kb.get_task(conn, tid).status == "ready"
