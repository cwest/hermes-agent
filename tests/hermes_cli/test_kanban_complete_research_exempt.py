"""Regression tests: a RESEARCH-cohort card is exempt from the author-lane
review redirect and terminates at ``done``, never in the review lane.

``done`` on the board means "the work is finished." For most cards an author's
``complete_task`` MOVEs the card to the ``review`` lane instead of ``done`` (the
reviewer, then Casey's acceptance, own the terminal). The research cohort
(reddy/avram, repo ``cwest/knowledge-base``) is the documented exception: it
publishes directly to the CI-gated knowledge base and **never enters the review
lane or the tri-state contract** (``sdlc-review`` ``research-excluded-lane``).

The live defect (card ``t_f91cf0ee``, a write-time curation sweep filed by
``stage-curation-sweep`` with owner map ``{ready: reddy, review: avram}``):
because the redirect fired on the mere PRESENCE of a ``review`` owner in the
owner map, every no-PR curation sweep was shunted ``running -> review`` instead
of ``-> done``. The dispatcher then re-claimed the ``review`` card and re-spawned
avram (who is BOTH the sweep worker AND the review owner), looping forever on
already-finished work.

The fix: the author-lane redirect is EXEMPT for a card whose review owner is a
research-cohort reviewer (``_RESEARCH_REVIEWERS``) — such a card completes
straight to ``done``, EVEN though its owner map names a review owner. The
discriminator is the review COHORT (does this card enter the review lane at all),
not the workspace kind — because a research curation sweep and an engineering
edit-in-place card (``t_baaa247f``) are BOTH ``scratch``/no-PR and only their
cohort tells them apart.

These tests pin the contract on the real ``complete_task`` path, in BOTH
directions:

* a research card (review owner avram) with NO PR completes ``running -> done``
  — the curation-sweep case (RED before the fix: it redirected to review).
* an engineering edit-in-place card (review owner lamport), same scratch/no-PR
  shape, STILL MOVEs ``running -> review`` — the exemption is cohort-scoped and
  must not weaken the ``t_baaa247f`` behavior.
* a writing card (review owner perkins) STILL MOVEs to review — unchanged.
* a worktree PR-owing card STILL MOVEs to review — unchanged.
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
    """Record the card's submit-stage audit comment carrying ``state_owners``.

    Mirrors the ``[audit] ... stage=submit`` comment ``submit_card`` writes at
    creation — the free-text ``notes:`` line holds the materialized owner map.
    """
    body = (
        "[audit] actor=hollis stage=submit ts=2026-08-02T18:29:21Z\n"
        f"notes: state_owners={{{owner_map}}} triager=hollis team={team}"
    )
    kb.add_comment(conn, tid, author="hollis", body=body)


# ---------------------------------------------------------------------------
# RED 1 — a research curation-sweep card completes running -> done, not review
# ---------------------------------------------------------------------------


def test_research_curation_sweep_completes_to_done(kanban_home: Path) -> None:
    """The ``t_f91cf0ee`` shape: a ``scratch`` no-PR curation sweep whose owner
    map is ``{ready: reddy, review: avram}``. An author-lane completion must
    complete it to ``done`` — the research cohort never enters the review lane —
    NOT shunt it ``running -> review`` (the forever-loop defect)."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="curate: write-time sweep @ aefa5a1a",
            assignee="avram",
            workspace_kind="scratch", detached=True,
        )
        _stamp_owner_map(conn, tid, "ready: reddy, review: avram", team="research")
        kb.claim_task(conn, tid)
        assert kb.get_task(conn, tid).status == "running"

        ok = kb.complete_task(conn, tid, summary="no action warranted, no PR")

        assert ok is True, "a research card completes (terminates at done)"
        task = kb.get_task(conn, tid)
        assert task.status == "done", (
            "a research curation sweep must terminate at done, "
            "not be redirected into the review lane"
        )
        assert task.completed_at is not None, "a done completion stamps completed_at"


def test_research_sweep_emits_no_review_move(kanban_home: Path) -> None:
    """No ``status_changed -> review`` event lands for a research card."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="curate: write-time sweep @ deadbeef",
            assignee="avram",
            workspace_kind="scratch", detached=True,
        )
        _stamp_owner_map(conn, tid, "ready: reddy, review: avram", team="research")
        kb.claim_task(conn, tid)

        kb.complete_task(conn, tid, summary="nothing actionable this cycle")

        events = kb.list_events(conn, tid)
        assert not [
            e for e in events
            if e.kind == "status_changed" and (e.payload or {}).get("to") == "review"
        ], "a research card must not be moved into the review lane"


def test_research_reviewers_set_contains_avram(kanban_home: Path) -> None:
    """The research review cohort is named so the redirect can exempt it."""
    assert "avram" in kb._RESEARCH_REVIEWERS


# ---------------------------------------------------------------------------
# RED 2 — the exemption is COHORT-scoped: an engineering edit-in-place card
# (same scratch/no-PR shape) STILL moves to review (t_baaa247f unweakened).
# ---------------------------------------------------------------------------


def test_engineering_scratch_card_still_moves_to_review(kanban_home: Path) -> None:
    """The ``t_baaa247f`` shape: a ``scratch`` no-PR card whose review owner is
    the ENGINEERING reviewer (lamport). It must STILL MOVE to review — the
    research exemption must be scoped to the research cohort only, and must not
    release an engineering edit-in-place card straight to done."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="stamp SOUL block in place",
            assignee="eckert",
            workspace_kind="scratch", detached=True,
        )
        _stamp_owner_map(
            conn, tid,
            "ready: eckert, review: lamport, blocked-acceptance: casey",
            team="engineering",
        )
        kb.claim_task(conn, tid)

        ok = kb.complete_task(conn, tid, summary="edited in place, no PR")

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "review", (
            "an engineering scratch card must still MOVE to review "
            "(the research exemption is cohort-scoped, not workspace-scoped)"
        )
        assert task.assignee == "lamport"
        assert task.completed_at is None


def test_writing_scratch_card_still_moves_to_review(kanban_home: Path) -> None:
    """A writing card (review owner perkins) is not research and still MOVEs to
    review — the exemption must not leak to the writing cohort."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="draft the post", assignee="lawrence", detached=True)
        _stamp_owner_map(
            conn, tid,
            "ready: lawrence, review: perkins, blocked-acceptance: casey",
            team="writing",
        )
        kb.claim_task(conn, tid)

        ok = kb.complete_task(conn, tid, summary="draft finished")

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "review", "a writing card must still MOVE to review"
        assert task.assignee == "perkins"


# ---------------------------------------------------------------------------
# RED 3 — a worktree PR-owing card still redirects to review (guard unweakened)
# ---------------------------------------------------------------------------


def test_worktree_pr_card_still_moves_to_review(kanban_home: Path) -> None:
    """A git-worktree PR-owing code card WITH a review owner and a PR artifact
    still redirects ``running -> review`` (the code case) — the research
    exemption must not weaken the redirect for PR-owing cards."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="fix the widget",
            assignee="eckert",
            workspace_kind="worktree",
            workspace_path="/Users/x/src/repo/.worktrees/t_abc", detached=True,
        )
        _stamp_owner_map(
            conn, tid,
            "ready: eckert, review: lamport, blocked-acceptance: casey",
            team="engineering",
        )
        # A resolvable PR artifact (the ready-for-review handoff comment).
        kb.add_comment(
            conn, tid, author="eckert",
            body="draft PR open: https://github.com/cwest/hermes-agent/pull/999",
        )
        kb.claim_task(conn, tid)

        ok = kb.complete_task(conn, tid, summary="implemented + tests")

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "review", "a PR-owing code card must still MOVE to review"
        assert task.assignee == "lamport"


# ---------------------------------------------------------------------------
# RED 4 — a worktree PR-owing card with NO PR still hits the missing-PR guard
# ---------------------------------------------------------------------------


def test_worktree_card_without_pr_still_refused_missing_pr(kanban_home: Path) -> None:
    """A git-worktree card with a review owner but NO PR artifact still hits the
    ``completion_refused_missing_pr`` guard (unchanged) — the research exemption
    must not let a PR-owing card slip past that guard."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="fix the widget",
            assignee="eckert",
            workspace_kind="worktree",
            workspace_path="/Users/x/src/repo/.worktrees/t_def", detached=True,
        )
        _stamp_owner_map(
            conn, tid,
            "ready: eckert, review: lamport, blocked-acceptance: casey",
            team="engineering",
        )
        kb.claim_task(conn, tid)

        ok = kb.complete_task(conn, tid, summary="forgot to open the PR")

        assert ok is False, "a PR-owing card with no PR must be refused"
        task = kb.get_task(conn, tid)
        assert task.status == "running", "a refused completion does not move the card"
        assert [
            e for e in kb.list_events(conn, tid)
            if e.kind == "completion_refused_missing_pr"
        ], "the missing-PR guard must still fire (unchanged)"


# ---------------------------------------------------------------------------
# RED 5 — an un-stamped / no-review-owner card still completes to done
# ---------------------------------------------------------------------------


def test_unstamped_card_still_completes_to_done(kanban_home: Path) -> None:
    """A plain scratch card with no owner map anywhere and no PR is genuinely
    review-EXEMPT and completes to ``done`` exactly as before — the research
    exemption is additive and must not change this."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="bookkeeping tick", assignee="salton", detached=True)
        kb.claim_task(conn, tid)

        assert kb.complete_task(conn, tid, summary="done, no review needed") is True
        assert kb.get_task(conn, tid).status == "done"
