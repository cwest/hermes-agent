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


# ---------------------------------------------------------------------------
# RED 4 — defect (b): a self-owned review completion on a card whose linked PR
# is ALREADY MERGED must land ``done``, not re-park for acceptance.
#
# The live shape (t_d23132c7 / cwest/okfctl#174, 2026-09-26): a PR was PASS'd
# and parked for acceptance, then a merge-only branch catch-up fired a
# ``synchronize`` that pulled the card back into review and re-spawned the
# reviewer. By the time that reviewer's run finished, Casey had ALREADY merged
# the PR — yet the self-review completion path re-parked the card
# ``blocked`` + casey ("PR #174 was MERGED" per its own verdict), stranding a
# merged card in the acceptance lane until ``reconcile-acceptance`` recovered
# it. ``done`` means "Casey merged"; a proven merge at completion time IS that,
# so the completion must terminate at ``done`` with no reconcile round-trip.
#
# The merge is proven from GitHub GROUND TRUTH (the same
# ``_resolve_pr_merge_commit`` gate ``reconcile_merged_acceptance`` trusts),
# never caller assertion — so the fix cannot re-open the acceptance guard's
# hole. Only ``state == merged`` with a non-null oid routes to ``done``;
# everything else parks exactly as before (the controls below).
# ---------------------------------------------------------------------------


_MERGED_PR_URL = "https://github.com/cwest/okfctl/pull/174"
_MERGED_OID = "895f0ec1234567890abcdef1234567890abcdef0"


def _link_pr(conn, tid: str, pr_url: str) -> None:
    """Link a PR URL on the card the way the implementer's ready-for-review
    handoff comment does (``_card_newest_pr_url`` reads it from comments)."""
    kb.add_comment(
        conn, tid, author="easley",
        body=f"Draft PR opened: {pr_url} @ head 895f0ec. 240 tests green.",
    )


def test_self_owned_review_completion_on_merged_pr_lands_done(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The t_d23132c7 shape: a card claimed FROM review whose linked PR is
    verifiably MERGED at completion time terminates at ``done`` — NOT the
    acceptance park. Proven merge == acceptance; there is nothing left to sign
    off, and re-parking only strands a merged card (the observed regression)."""
    monkeypatch.setattr(
        kb, "_resolve_pr_merge_commit", lambda url: ("merged", _MERGED_OID)
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="fix the kanban redirect", assignee="easley", detached=True
        )
        _stamp_owner_map(
            conn, tid,
            "ready: easley, review: lamport, blocked-acceptance: casey",
            team="engineering",
        )
        _link_pr(conn, tid, _MERGED_PR_URL)
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="implemented + tests")  # -> review
        _move_to_review_and_claim(conn, tid, "lamport")

        ok = kb.complete_task(conn, tid, summary="PASS; PR was MERGED")

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "done", (
            "a self-owned review completion on an ALREADY-MERGED PR must land "
            "done, not re-park for acceptance"
        )
        assert task.completed_at is not None, "a merged completion is terminal"
        assert task.status != "blocked", "a merged card must not re-park"


def test_self_owned_review_completion_on_merged_pr_records_merge_audit(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge-at-completion path records a distinguishable audit event
    carrying the proven merge commit + PR url, so a done-by-merge-at-completion
    is as traceable as a done-by-webhook or a done-by-reconcile."""
    monkeypatch.setattr(
        kb, "_resolve_pr_merge_commit", lambda url: ("merged", _MERGED_OID)
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="fix the kanban redirect", assignee="easley", detached=True
        )
        _stamp_owner_map(
            conn, tid,
            "ready: easley, review: lamport, blocked-acceptance: casey",
            team="engineering",
        )
        _link_pr(conn, tid, _MERGED_PR_URL)
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="implemented + tests")
        _move_to_review_and_claim(conn, tid, "lamport")

        kb.complete_task(conn, tid, summary="PASS; PR was MERGED")

        events = kb.list_events(conn, tid)
        merge_events = [
            e for e in events if e.kind == "completion_merged_at_review"
        ]
        assert merge_events, (
            "a merge-proven review completion must emit a distinguishable "
            "completion_merged_at_review audit event"
        )
        payload = merge_events[-1].payload or {}
        assert payload.get("merge_commit") == _MERGED_OID
        assert payload.get("pr_url") == _MERGED_PR_URL


# ---------------------------------------------------------------------------
# CONTROL — the acceptance guard is NOT weakened: a self-owned review completion
# whose PR is NOT proven merged still parks ``blocked`` + casey. Only a proven
# merge routes to done; open / unresolvable / no-PR all fail closed to the park.
# ---------------------------------------------------------------------------


def test_self_owned_review_completion_open_pr_still_parks(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OPEN linked PR is not a merge — the completion still parks for Casey's
    acceptance exactly as before. The merge check must fail CLOSED."""
    monkeypatch.setattr(
        kb, "_resolve_pr_merge_commit", lambda url: ("open", None)
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="fix the kanban redirect", assignee="easley", detached=True
        )
        _stamp_owner_map(
            conn, tid,
            "ready: easley, review: lamport, blocked-acceptance: casey",
            team="engineering",
        )
        _link_pr(conn, tid, _MERGED_PR_URL)
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="implemented + tests")
        _move_to_review_and_claim(conn, tid, "lamport")

        ok = kb.complete_task(conn, tid, summary="PASS; awaiting sign-off")

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            "an OPEN PR must still park for acceptance — the merge check fails "
            "closed"
        )
        assert task.assignee == "casey"
        assert task.completed_at is None


def test_self_owned_review_completion_unresolvable_pr_still_parks(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient/unresolvable ``gh`` answer (``unknown``) is not a proven
    merge — the completion parks for acceptance (fail closed)."""
    monkeypatch.setattr(
        kb, "_resolve_pr_merge_commit", lambda url: ("unknown", None)
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="fix the kanban redirect", assignee="easley", detached=True
        )
        _stamp_owner_map(
            conn, tid,
            "ready: easley, review: lamport, blocked-acceptance: casey",
            team="engineering",
        )
        _link_pr(conn, tid, _MERGED_PR_URL)
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="implemented + tests")
        _move_to_review_and_claim(conn, tid, "lamport")

        ok = kb.complete_task(conn, tid, summary="PASS; awaiting sign-off")

        assert ok is True
        assert kb.get_task(conn, tid).status == "blocked"


def test_self_owned_review_completion_no_pr_still_parks(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card with NO linked PR has nothing to prove a merge against — the merge
    check must not even consult ``gh`` and the completion parks for acceptance
    (the ordinary edit-in-place self-owned review wedge is untouched)."""
    calls: list[str] = []

    def _spy(url: str):
        calls.append(url)
        return ("unknown", None)

    monkeypatch.setattr(kb, "_resolve_pr_merge_commit", _spy)
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="edit-in-place fix", assignee="easley", detached=True
        )
        _stamp_owner_map(
            conn, tid,
            "ready: easley, review: lamport, blocked-acceptance: casey",
            team="engineering",
        )
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="implemented")
        _move_to_review_and_claim(conn, tid, "lamport")

        ok = kb.complete_task(conn, tid, summary="PASS; awaiting sign-off")

        assert ok is True
        assert kb.get_task(conn, tid).status == "blocked"
        assert calls == [], (
            "no linked PR -> the merge check must not consult gh (nothing to "
            "prove a merge against)"
        )
