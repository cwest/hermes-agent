"""Regression tests: a reviewer completing its OWN review lane must not be
re-routed back into review (the self-owned-review-lane respawn wedge).

## The wedge

A card claimed FROM the ``review`` lane keeps its reviewer as ``assignee`` and
records ``source_status: "review"`` on the run's ``claimed`` event. When that
reviewer completes the card, the author-lane redirect in ``complete_task`` fired
on the mere PRESENCE of a ``review`` owner in the owner map and MOVEd the card
BACK into ``review`` with the SAME assignee — a self-handoff with no next actor.
The dispatcher then re-claimed the ``review`` card and re-spawned the same
reviewer, looping forever (live ``t_09717828``, writing card
``{ready: lawrence, review: perkins, blocked-acceptance: casey}``, 2026-08-14).

The ``_RESEARCH_REVIEWERS`` exemption only covered the research cohort, so every
other cohort whose reviewer can also be the completer (writing, engineering)
still fell into the loop. This fix is the STRUCTURAL discriminator the cohort
allowlist stood in for: when the card was claimed FROM ``review`` AND the
completing assignee IS the card's own ``review`` owner, the review lane is
FINISHED, not pending — the redirect must NOT fire. The card is routed to its
correct terminal instead: the acceptance park (``blocked`` + the
``blocked-acceptance`` owner, sticky ``awaiting-casey-signoff`` reason), Casey's
human sign-off gate. Never ``done`` — ``done`` still means Casey merged.

## Directions pinned here

* self-owned review completion (claimed-from-review, assignee == review owner)
  → acceptance park, NOT a ``running -> review`` self-handoff. [the wedge]
* a FIRST author completion (claimed from ``ready``, assignee is the author, not
  the reviewer) STILL MOVEs to review — the ordinary handoff is unweakened.
* the same self-owned completion for the RESEARCH cohort still terminates at
  ``done`` (belt-and-braces: the structural test now covers the loop the
  ``_RESEARCH_REVIEWERS`` exemption was patched to fix).
"""

from __future__ import annotations

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


def _stamp_owner_map(conn, tid: str, owner_map: str, *, team: str = "engineering") -> None:
    """Record the card's submit-stage audit comment carrying ``state_owners``."""
    body = (
        "[audit] actor=hollis stage=submit ts=2026-08-14T15:13:08Z\n"
        f"notes: state_owners={{{owner_map}}} triager=hollis team={team}"
    )
    kb.add_comment(conn, tid, author="hollis", body=body)


def _to_review(conn, tid: str, reviewer: str, *, author: str = "") -> None:
    """Flip a card into ``review`` under its reviewer (the build->review hop the
    ``stage-pr-review`` MOVE / dispatcher performs). Mirrors the raw-UPDATE
    pattern the existing accept-task test uses — there is no ``move_card`` in
    ``kanban_db`` (that primitive lives in the onecard plugin)."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='review', assignee=?, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (reviewer, tid),
        )
        kb._append_event(
            conn, tid, "status_changed",
            {"from": "ready", "to": "review", "by": "onecard:move_card"},
        )


def _move_to_review_and_claim(conn, tid: str, reviewer: str) -> None:
    """Move a card into ``review`` under its reviewer, then claim it FROM review —
    reproducing exactly what the dispatcher does when it re-spawns the reviewer
    for a card sitting in the review lane.

    ``claim_review_task`` stamps ``source_status: "review"`` on the run's
    ``claimed`` event and keeps the reviewer as ``assignee`` — the two signals
    the self-handoff discriminator keys on.
    """
    _to_review(conn, tid, reviewer)
    task = kb.get_task(conn, tid)
    assert task is not None and task.status == "review" and task.assignee == reviewer
    claimed = kb.claim_review_task(conn, tid)
    assert claimed is not None, "the reviewer must be able to claim its review card"
    assert claimed.status == "running"
    assert claimed.assignee == reviewer, "a review claim keeps the reviewer assignee"


# ---------------------------------------------------------------------------
# RED 1 — the wedge: a reviewer completing its OWN review lane is parked for
# acceptance, NOT re-routed into review.
# ---------------------------------------------------------------------------


def test_self_owned_review_completion_parks_for_acceptance(kanban_home: Path) -> None:
    """The ``t_09717828`` shape: a card claimed FROM review, completed by its own
    review owner, must NOT be shunted ``running -> review`` (the forever-loop
    self-handoff). It lands on its correct terminal: the acceptance park
    (``blocked`` + the ``blocked-acceptance`` owner, sticky
    ``awaiting-casey-signoff``)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="draft the launch post", assignee="lawrence", detached=True)
        _stamp_owner_map(
            conn, tid,
            "ready: lawrence, review: perkins, blocked-acceptance: casey",
            team="writing",
        )
        kb.claim_task(conn, tid)  # first author run (from ready)
        kb.complete_task(conn, tid, summary="draft finished")  # -> review (ordinary)
        assert kb.get_task(conn, tid).status == "review"

        # The reviewer is spawned for the review card: claim FROM review.
        _move_to_review_and_claim(conn, tid, "perkins")  # already there; re-claim

        # The reviewer PASSes and completes its OWN review lane.
        ok = kb.complete_task(conn, tid, summary="PASS; awaiting sign-off")

        assert ok is True, "a self-owned review completion is a real transition"
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            "a reviewer completing its own review lane must be PARKED for "
            "acceptance, not re-routed into review"
        )
        assert task.assignee == "casey", (
            "the acceptance park is owned by the blocked-acceptance owner"
        )
        assert task.completed_at is None, "acceptance park is not done"


def test_self_owned_review_completion_emits_no_review_move(kanban_home: Path) -> None:
    """No ``status_changed {to: review, by: onecard:complete-task}`` self-handoff
    event lands, and the card is NOT re-dispatchable (not left in review)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="draft the post", assignee="lawrence", detached=True)
        _stamp_owner_map(
            conn, tid,
            "ready: lawrence, review: perkins, blocked-acceptance: casey",
            team="writing",
        )
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="draft finished")  # legit author->review
        _move_to_review_and_claim(conn, tid, "perkins")
        review_run_id = kb.get_task(conn, tid).current_run_id

        kb.complete_task(conn, tid, summary="PASS; awaiting sign-off")

        events = kb.list_events(conn, tid)
        # The load-bearing negative: the REVIEW-claimed run must not emit a
        # self-handoff back into review. (The one legitimate author->review move
        # from the FIRST run is expected and untouched.)
        review_self_handoffs = [
            e for e in events
            if e.kind == "status_changed"
            and (e.payload or {}).get("to") == "review"
            and e.run_id == review_run_id
        ]
        assert not review_self_handoffs, (
            "the reviewer must NOT be re-routed into review by its own completion"
        )
        # And the card is genuinely off the review lane (not re-dispatchable there).
        assert kb.get_task(conn, tid).status != "review"


def test_self_owned_review_park_reason_is_awaiting_signoff(kanban_home: Path) -> None:
    """The parked card carries a sticky ``awaiting-casey-signoff`` block reason so
    the acceptance guard holds it and the acceptance notification fires."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="draft the post", assignee="lawrence", detached=True)
        _stamp_owner_map(
            conn, tid,
            "ready: lawrence, review: perkins, blocked-acceptance: casey",
            team="writing",
        )
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="draft finished")
        _move_to_review_and_claim(conn, tid, "perkins")

        kb.complete_task(conn, tid, summary="PASS; awaiting sign-off")

        reason = kb._latest_sticky_block_reason(conn, tid)
        assert reason is not None
        assert reason.lstrip().lower().startswith(
            kb._ACCEPTANCE_SIGNOFF_REASON_PREFIX
        ), "the acceptance park reason must key on awaiting-casey-signoff"

        # And the acceptance guard now refuses a generic completer (done means
        # Casey merged; a parked card is not that).
        assert kb.complete_task(conn, tid, summary="try to force done") is False
        assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# RED 2 — the ordinary FIRST author handoff is unweakened: an author completing
# from ``ready`` (assignee is the author, NOT the reviewer) still MOVEs to review.
# ---------------------------------------------------------------------------


def test_first_author_completion_still_moves_to_review(kanban_home: Path) -> None:
    """A card claimed from ``ready`` and completed by its AUTHOR (assignee is the
    author, not the review owner) must STILL MOVE to review — the self-handoff
    discriminator must not fire on the ordinary author handoff."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="draft the post", assignee="lawrence", detached=True)
        _stamp_owner_map(
            conn, tid,
            "ready: lawrence, review: perkins, blocked-acceptance: casey",
            team="writing",
        )
        kb.claim_task(conn, tid)  # from ready; assignee is the author (lawrence)

        ok = kb.complete_task(conn, tid, summary="draft finished")

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "review", "the ordinary author handoff still MOVEs to review"
        assert task.assignee == "perkins"


def test_engineering_self_owned_review_completion_parks(kanban_home: Path) -> None:
    """The same wedge for the ENGINEERING cohort (review owner lamport): a card
    claimed from review and completed by lamport parks for acceptance, not a
    self-handoff into review."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="fix the kanban redirect", assignee="easley", detached=True)
        _stamp_owner_map(
            conn, tid,
            "ready: easley, review: lamport, blocked-acceptance: casey",
            team="engineering",
        )
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="implemented + tests")
        _move_to_review_and_claim(conn, tid, "lamport")

        ok = kb.complete_task(conn, tid, summary="PASS; awaiting sign-off")

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.assignee == "casey"


# ---------------------------------------------------------------------------
# RED 3 — research cohort: a self-owned review completion still terminates at
# ``done`` (the loop the _RESEARCH_REVIEWERS exemption fixed, now covered
# structurally too).
# ---------------------------------------------------------------------------


def test_research_self_owned_review_completion_still_done(kanban_home: Path) -> None:
    """A research card whose review owner (avram) is also the completer, claimed
    from review, still terminates at ``done`` — the research cohort never enters
    the acceptance park either; it publishes to the CI-gated KB directly."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="curate: write-time sweep", assignee="reddy",
            workspace_kind="scratch", detached=True,
        )
        _stamp_owner_map(conn, tid, "ready: reddy, review: avram", team="research")
        kb.claim_task(conn, tid)
        # A research sweep terminates at done on the first completion, so drive
        # the self-owned case directly: move to review under avram, claim, done.
        _to_review(conn, tid, "avram")
        assert kb.claim_review_task(conn, tid) is not None

        ok = kb.complete_task(conn, tid, summary="nothing actionable")

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "done", (
            "a research self-owned review completion still terminates at done"
        )
