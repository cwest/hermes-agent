"""Regression tests: an AUTHOR's ``complete_task`` MOVES the card to the review
lane, never straight to ``done``.

``done`` on the board means exactly one thing — Casey merged/accepted the work.
A recurring defect (hit twice live on one writing card) let an author finish
their lane and the card jump straight to ``done``, SKIPPING the reviewer and the
Casey acceptance gate. Code cards were accidentally rescued because their PR
fires the ``github-prs`` webhook -> ``stage-pr-review`` moves them to review; a
writing card whose review is board-driven (no PR-review webhook on the drafting
step) had no such rescue, so the author's ``complete_task`` landed it in ``done``
past the reviewer and past Casey.

The fix: when an author completes a card FROM the author lane (``running`` /
``ready``) AND the card's OWN stamped owner map carries a ``review`` owner, the
completion MOVES the card to ``status='review'`` + that review owner instead of
setting ``done``. This is a general fix — it holds for every kind with a review
lane (code AND writing AND research). Code cards keep the webhook rescue too, so
the two paths must be idempotent: once the card is in ``review``, a second
completion attempt is a clean no-op.

These tests pin the contract on the real ``complete_task`` path:

* A writing author's completion MOVES the card to ``review`` + the card's review
  owner (perkins for writing), NOT ``done``, and emits a ``status_changed``.
* A code author's completion MOVES the card to ``review`` + lamport, NOT ``done``.
* The redirect fires from a ``ready`` card too (a manual CLI complete of a
  never-claimed card).
* Idempotency: once the card is in ``review`` (webhook already moved it), a
  second completion is a no-op — it does not re-move and does not land ``done``.
* A card with NO stamped review owner (legacy / un-stamped, or a plain
  non-pipeline task) still completes to ``done`` — the redirect never shunts a
  card that was not declared to have a review lane.
* Casey's merge (``allow_acceptance_complete=True``) is UNCHANGED — it lands the
  card in ``done`` regardless of the owner map.
* A generic ``blocked`` card (needs_input / review-changes-requested) stays
  completable to ``done`` — the redirect keys on the author lane, not on the
  owner map alone.
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


def _stamp_owner_map(conn, tid: str, owner_map: str) -> None:
    """Record the card's submit-stage audit comment carrying ``state_owners``.

    Mirrors the ``[audit] ... stage=submit`` comment ``submit_card`` writes at
    creation — the free-text ``notes:`` line holds the materialized owner map.
    """
    body = (
        "[audit] actor=hollis stage=submit ts=2026-07-09T18:29:21Z\n"
        f"notes: state_owners={{{owner_map}}} triager=hollis team=engineering"
    )
    kb.add_comment(conn, tid, author="hollis", body=body)


def _author_card(conn, *, owner_map: str | None, assignee: str = "eckert") -> str:
    """A running author-lane card, optionally stamped with an owner map."""
    tid = kb.create_task(conn, title="feature work", assignee=assignee)
    if owner_map is not None:
        _stamp_owner_map(conn, tid, owner_map)
    kb.claim_task(conn, tid)
    assert kb.get_task(conn, tid).status == "running"
    return tid


# ---------------------------------------------------------------------------
# RED 1 — a writing author's completion MOVES the card to review + perkins
# ---------------------------------------------------------------------------


def test_writing_author_completion_moves_to_review(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = _author_card(
            conn,
            owner_map="ready: orwell, review: perkins, blocked-acceptance: casey",
            assignee="orwell",
        )

        ok = kb.complete_task(conn, tid, summary="draft finished")

        assert ok is True, "the author completion must succeed (as a move)"
        task = kb.get_task(conn, tid)
        assert task.status == "review", "author completion must MOVE to review, not done"
        assert task.assignee == "perkins", "assignee is the card's review owner"
        assert task.completed_at is None, "a review move is not a completion timestamp"


def test_writing_author_completion_emits_status_changed(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = _author_card(
            conn,
            owner_map="ready: orwell, review: perkins, blocked-acceptance: casey",
            assignee="orwell",
        )
        kb.complete_task(conn, tid, summary="draft finished")

        events = kb.list_events(conn, tid)
        moves = [
            e for e in events
            if e.kind == "status_changed" and (e.payload or {}).get("to") == "review"
        ]
        assert moves, "the redirect must emit a status_changed -> review event"
        # A review move is NOT a completion: no 'done'-completed event landed.
        assert not [e for e in events if e.kind == "completed"], \
            "a review move must not emit a 'completed' event"


# ---------------------------------------------------------------------------
# RED 2 — a code author's completion MOVES the card to review + lamport
# ---------------------------------------------------------------------------


def test_code_author_completion_moves_to_review(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = _author_card(
            conn,
            owner_map="ready: eckert, review: lamport, blocked-acceptance: casey",
        )

        ok = kb.complete_task(conn, tid, summary="implemented + tests")

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "review", "code author completion must MOVE to review"
        assert task.assignee == "lamport", "assignee is the card's review owner"


def test_ready_lane_author_completion_moves_to_review(kanban_home: Path) -> None:
    """A never-claimed ``ready`` card completed via the CLI is still redirected."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="feature work", assignee="eckert")
        _stamp_owner_map(
            conn, tid, "ready: eckert, review: lamport, blocked-acceptance: casey"
        )
        assert kb.get_task(conn, tid).status == "ready"

        ok = kb.complete_task(conn, tid, summary="done from ready")

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        assert task.assignee == "lamport"


# ---------------------------------------------------------------------------
# RED 3 — idempotency: a card already in review is a no-op (no double-move)
# ---------------------------------------------------------------------------


def test_completion_of_review_card_is_noop(kanban_home: Path) -> None:
    """Once the card is in ``review`` (e.g. the webhook moved it), a second
    completion attempt does not re-move it and does not land it in ``done``."""
    with kb.connect() as conn:
        tid = _author_card(
            conn,
            owner_map="ready: eckert, review: lamport, blocked-acceptance: casey",
        )
        # First completion moves it to review.
        assert kb.complete_task(conn, tid, summary="impl") is True
        assert kb.get_task(conn, tid).status == "review"

        # Second completion attempt (as if a duplicate webhook / stray call).
        ok = kb.complete_task(conn, tid, summary="second attempt")

        assert ok is False, "a review-lane card is not completable — clean no-op"
        task = kb.get_task(conn, tid)
        assert task.status == "review", "card must stay in review, not flip to done"
        assert task.assignee == "lamport"
        assert task.completed_at is None


# ---------------------------------------------------------------------------
# RED 4 — a card with no stamped review owner still completes to done
# ---------------------------------------------------------------------------


def test_unstamped_card_completes_to_done(kanban_home: Path) -> None:
    """A card with no submit-stage owner map (legacy / plain task) has no review
    lane declared — the redirect must NOT shunt it; it completes to ``done``."""
    with kb.connect() as conn:
        tid = _author_card(conn, owner_map=None)

        ok = kb.complete_task(conn, tid, summary="plain task done")

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "done", "an un-stamped card completes to done as before"
        assert task.completed_at is not None


def test_owner_map_without_review_lane_completes_to_done(kanban_home: Path) -> None:
    """A stamped card whose owner map has no ``review`` lane completes to done."""
    with kb.connect() as conn:
        tid = _author_card(conn, owner_map="ready: eckert, blocked-acceptance: casey")

        ok = kb.complete_task(conn, tid, summary="no review lane")

        assert ok is True
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# RED 5 — Casey's merge override is unchanged (regression)
# ---------------------------------------------------------------------------


def test_merge_override_still_reaches_done(kanban_home: Path) -> None:
    """``allow_acceptance_complete=True`` (Casey's merge) lands the card in
    ``done`` regardless of the owner map — the redirect must not intercept it."""
    with kb.connect() as conn:
        tid = _author_card(
            conn,
            owner_map="ready: eckert, review: lamport, blocked-acceptance: casey",
        )

        ok = kb.complete_task(
            conn, tid, summary="merged by Casey", allow_acceptance_complete=True
        )

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "done", "the merge override must reach done, not review"
        assert task.completed_at is not None


# ---------------------------------------------------------------------------
# RED 6 — a generic blocked card stays completable (no regression)
# ---------------------------------------------------------------------------


def test_generic_blocked_card_still_completes_to_done(kanban_home: Path) -> None:
    """A ``blocked`` card (needs_input) with a review owner is NOT redirected —
    the redirect fires only from the author lane (running/ready), and a generic
    blocked card stays completable to ``done`` (manual-complete-a-stuck-card)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="genuinely stuck", assignee="eckert")
        _stamp_owner_map(
            conn, tid, "ready: eckert, review: lamport, blocked-acceptance: casey"
        )
        kb.claim_task(conn, tid)
        assert kb.block_task(
            conn, tid,
            reason="review-required: please verify the ACL change",
            kind="needs_input",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        ok = kb.complete_task(conn, tid, summary="resolved out of band")

        assert ok is True, "a generic blocked card must stay completable"
        assert kb.get_task(conn, tid).status == "done"
