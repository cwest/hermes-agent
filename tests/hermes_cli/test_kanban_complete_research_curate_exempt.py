"""Regression tests: a NO-PR RESEARCH card (a write-time curation sweep) must
complete straight to ``done`` — it must NOT be shunted into the ``review`` lane.

The research cohort publishes directly to the CI-gated knowledge base and never
enters the tri-state review contract (``sdlc-review``'s ``research-excluded-lane``
rule). A write-time curation-sweep card (``curate: write-time sweep @ <sha>``,
team=research) is filed with the research owner map ``{ready: reddy, review:
avram}`` — which names a ``review`` owner. When such a sweep finds nothing
actionable it completes with NO PR (an ``okf-curation`` hard rule, and the write-
time loop guard).

Before this fix, the author-lane redirect in ``complete_task`` saw the resolvable
``review: avram`` owner and MOVEd the no-PR sweep to ``review`` instead of
``done``. The dispatcher then respawned an ``sdlc-review`` worker, which correctly
found NO PR, could emit no tri-state verdict, and re-completed → back to
``review`` → respawn: an infinite review-respawn loop (observed live 2026-08-02 on
``t_2c92b707``, which went through 3 review spawns before being blocked to break
it).

The fix is a NARROW completion-side exemption: an author-lane completion whose
resolved review owner is a RESEARCH reviewer (avram) AND which carries NO PR
artifact is review-EXEMPT — it falls through to ``done`` in one call. The
exemption is doubly scoped so it cannot leak:

* by REVIEWER — only a research reviewer (``_RESEARCH_REVIEWERS``) is exempt; a
  code card (review: lamport) or writing card (review: perkins) with no PR is
  unaffected and still MOVEs to review; and
* by ARTIFACT — a research card that DOES open a stewardship PR still MOVEs to
  review normally (the PR-open webhook path is unbroken), because the exemption
  only applies when ``_card_has_pr_artifact`` is False.

These tests pin the contract on the real ``complete_task`` path.
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


# The research owner map a curation-sweep card carries (no blocked-acceptance —
# research is excluded from the acceptance lifecycle).
_RESEARCH_MAP = "ready: reddy, review: avram"
# The canonical code/writing maps, used to prove the exemption does NOT leak to
# the other teams.
_CODE_MAP = "ready: eckert, review: lamport, blocked-acceptance: casey"
_WRITING_MAP = "ready: lawrence, review: perkins, blocked-acceptance: casey"


def _stamp_submit_owner_map(conn, tid: str, owner_map: str, *, team: str) -> None:
    """Record the card's submit-stage audit comment carrying ``state_owners``.

    Mirrors ``submit_card``'s §9.1 audit comment shape — the ONE comment
    ``_owner_from_owner_map`` treats as authoritative for the strict form.
    """
    kb.add_comment(
        conn, tid, author="hollis",
        body=(
            "[audit] actor=hollis stage=submit ts=2026-08-02T00:00:00Z\n"
            f"notes: state_owners={{{owner_map}}} triager=hollis team={team}"
        ),
    )


def _running_card(conn, *, owner_map: str, team: str, assignee: str) -> str:
    """A running author-lane card stamped with a submit-stage owner map."""
    tid = kb.create_task(conn, title="curate: write-time sweep @ abc123", assignee=assignee)
    _stamp_submit_owner_map(conn, tid, owner_map, team=team)
    kb.claim_task(conn, tid)
    assert kb.get_task(conn, tid).status == "running"
    return tid


# ---------------------------------------------------------------------------
# RED 1 — a no-PR research sweep completes straight to ``done`` (the loop fix)
# ---------------------------------------------------------------------------


def test_no_pr_research_sweep_completes_to_done(kanban_home: Path) -> None:
    """The ``t_2c92b707`` shape: a team=research curate sweep, review owner avram,
    NO PR. The author-lane completion must land it in ``done`` in ONE call — not
    MOVE it to the review lane (which respawns an sdlc-review worker forever)."""
    with kb.connect() as conn:
        tid = _running_card(
            conn, owner_map=_RESEARCH_MAP, team="research", assignee="avram"
        )

        ok = kb.complete_task(conn, tid, summary="nothing actionable this cycle, no PR")

        assert ok is True, "a no-PR research sweep must complete (to done)"
        task = kb.get_task(conn, tid)
        assert task.status == "done", (
            f"a no-PR research sweep must complete to done, got {task.status!r} "
            "(the review-respawn loop)"
        )
        assert task.completed_at is not None, "a real completion has a timestamp"


def test_no_pr_research_sweep_does_not_move_to_review(kanban_home: Path) -> None:
    """No ``status_changed -> review`` event lands for a no-PR research sweep —
    the redirect that produced the respawn loop must not fire."""
    with kb.connect() as conn:
        tid = _running_card(
            conn, owner_map=_RESEARCH_MAP, team="research", assignee="avram"
        )
        kb.complete_task(conn, tid, summary="no actionable findings, no PR")

        events = kb.list_events(conn, tid)
        assert not [
            e for e in events
            if e.kind == "status_changed" and (e.payload or {}).get("to") == "review"
        ], "a no-PR research sweep must NOT be moved to the review lane"
        assert not [
            e for e in events if e.kind == "completion_redirect_unresolved"
        ], "a research-exempt sweep is not a refusal — it completes to done"


# ---------------------------------------------------------------------------
# RED 2 — a research card that DOES open a stewardship PR still MOVEs to review
# ---------------------------------------------------------------------------


def test_research_card_with_pr_still_moves_to_review(kanban_home: Path) -> None:
    """The regression guard: a research card that legitimately opened a
    stewardship PR carries a PR artifact, so the exemption does NOT apply — its
    author completion MOVEs it to review + avram, exactly like any PR-bearing
    card (the PR-open webhook path stays unbroken)."""
    with kb.connect() as conn:
        tid = _running_card(
            conn, owner_map=_RESEARCH_MAP, team="research", assignee="reddy"
        )
        # A stewardship PR was opened — the ready-for-review handoff comment.
        kb.add_comment(
            conn, tid, author="reddy",
            body=(
                "PR opened (draft): https://github.com/cwest/knowledge-base/pull/77 "
                "head=deadbeef"
            ),
        )

        ok = kb.complete_task(conn, tid, summary="opened a stewardship PR")

        assert ok is True, "a PR-bearing research completion redirects (a move)"
        task = kb.get_task(conn, tid)
        assert task.status == "review", (
            f"a research card WITH a PR must still MOVE to review, got {task.status!r}"
        )
        assert task.assignee == "avram", "assignee is the card's review owner"
        assert task.completed_at is None, "a move to review is not a completion"


# ---------------------------------------------------------------------------
# RED 3 — the exemption does NOT leak: a no-PR CODE / WRITING card is unaffected
# ---------------------------------------------------------------------------


def test_no_pr_code_card_still_redirects_to_review(kanban_home: Path) -> None:
    """A code card (review owner lamport, no PR) is NOT research-exempt — the
    author-lane redirect must still fire (MOVE to review), never fall to done.
    The exemption is scoped to research reviewers only."""
    with kb.connect() as conn:
        tid = _running_card(
            conn, owner_map=_CODE_MAP, team="engineering", assignee="eckert"
        )

        ok = kb.complete_task(conn, tid, summary="edit in place, no PR")

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "review", (
            "a no-PR CODE card must still MOVE to review — the research exemption "
            "must not leak to engineering"
        )
        assert task.assignee == "lamport"


def test_no_pr_writing_card_still_redirects_to_review(kanban_home: Path) -> None:
    """A writing card (review owner perkins, no PR) is NOT research-exempt — a
    board-driven writing review still fires. Belt-and-suspenders that the
    exemption keys on the RESEARCH reviewer, not merely on 'no PR'."""
    with kb.connect() as conn:
        tid = _running_card(
            conn, owner_map=_WRITING_MAP, team="writing", assignee="lawrence"
        )

        ok = kb.complete_task(conn, tid, summary="draft finished, no PR")

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "review", (
            "a no-PR WRITING card must still MOVE to review — the research "
            "exemption must not leak to writing"
        )
        assert task.assignee == "perkins"


# ---------------------------------------------------------------------------
# RED 4 — Casey's merge override is unchanged (regression)
# ---------------------------------------------------------------------------


def test_merge_override_still_reaches_done_for_research(kanban_home: Path) -> None:
    """``allow_acceptance_complete=True`` (Casey's merge) lands a research card in
    ``done`` regardless — the exemption path shares the same override bypass."""
    with kb.connect() as conn:
        tid = _running_card(
            conn, owner_map=_RESEARCH_MAP, team="research", assignee="avram"
        )

        ok = kb.complete_task(
            conn, tid, summary="accepted by Casey", allow_acceptance_complete=True
        )

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert task.completed_at is not None
