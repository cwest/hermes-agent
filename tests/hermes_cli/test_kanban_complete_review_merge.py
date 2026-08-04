"""Regression tests: a MERGED PR's card in the ``review`` lane must reach ``done``.

``done`` on the board means exactly one thing — Casey merged/accepted the work.
The sanctioned merge-close path (``close-pr-card`` -> ``accept_card`` ->
``complete_task(allow_acceptance_complete=True)``) is the ONE legitimate route a
card reaches ``done``. That route was structurally DEAD for a card sitting in the
``review`` lane.

Hit live (2026-08-04) on card ``t_0d86b126`` (PR #180, cwest/cwest.github.io,
MERGED): both ``hermes kanban complete`` and ``close_pr_card.py --merged true``
refused, leaving a merged card stranded in ``review``.

Root cause: ``complete_task``'s final ``-> done`` UPDATE is gated on
``status IN ('running','ready','blocked')`` — ``review`` is omitted. On the merge
path (``allow_acceptance_complete=True``) the redirect-to-review branch is skipped
(as it must be — the merge is the terminal accept, not an author handoff), so the
card falls through to that UPDATE, which matches ZERO rows for a ``review`` card.
``cur.rowcount != 1`` then returns ``False``.

The fix widens the allowed set to include ``review`` ONLY when
``allow_acceptance_complete=True`` — i.e. only on the verified-merge path. A worker
completing its own ``review`` card WITHOUT the override is still refused, so the
reviewer-bypass the guard exists to prevent stays closed. The rework path
(``ready -> review`` after a bounce) is untouched.

Also pins the refusal-message fix: when a completion fails purely because the
card's status is not in the completable set, ``explain_completion_refusal`` names
the actual status and the allowed set instead of asserting a concurrent lane move
that did not happen (the misleading diagnosis that cost real debugging time).
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


def _review_lane_card(conn, *, assignee: str = "lamport") -> str:
    """Leave a card in the ``review`` lane, exactly as a PR-open handoff parks it.

    Mirrors how the existing suite stages a review card: a plain status flip to
    ``review`` (the ``stage-pr-review`` MOVE that happens on PR-open).
    """
    tid = kb.create_task(conn, title="implemented feature", assignee=assignee)
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
    assert kb.get_task(conn, tid).status == "review"
    return tid


# ---------------------------------------------------------------------------
# RED 1 — a merged PR's review-lane card reaches done via the merge path
# ---------------------------------------------------------------------------


def test_merge_override_completes_review_lane_card(kanban_home: Path) -> None:
    """The merge path (``allow_acceptance_complete=True``) lands a ``review``
    card in ``done`` — this is the exact route Casey's merge webhook takes, and
    the ONLY route by which a merged PR's card reaches ``done`` from review."""
    with kb.connect() as conn:
        tid = _review_lane_card(conn)

        ok = kb.complete_task(
            conn, tid,
            summary="merged by Casey",
            allow_acceptance_complete=True,
        )

        assert ok is True, "the merge override must complete the review-lane card"
        task = kb.get_task(conn, tid)
        assert task.status == "done", "the merge path must land the card in done"
        assert task.completed_at is not None, "a real completion stamps completed_at"


def test_merge_override_review_card_emits_completed_event(kanban_home: Path) -> None:
    """The successful merge-completion is auditable as a ``completed`` event and
    does NOT emit a refusal."""
    with kb.connect() as conn:
        tid = _review_lane_card(conn)
        kb.complete_task(
            conn, tid, summary="merged", allow_acceptance_complete=True,
        )

        events = kb.list_events(conn, tid)
        assert [e for e in events if e.kind == "completed"], \
            "a merge completion must emit a 'completed' event"
        assert not [
            e for e in events
            if e.kind in ("completion_refused_acceptance",
                          "completion_redirect_unresolved")
        ], "a successful merge completion must not record a refusal"


# ---------------------------------------------------------------------------
# RED 2 — the reviewer-bypass stays closed: a worker CANNOT self-complete
#          out of review WITHOUT the merge override
# ---------------------------------------------------------------------------


def test_worker_cannot_self_complete_out_of_review(kanban_home: Path) -> None:
    """A completion of a ``review`` card WITHOUT ``allow_acceptance_complete`` is
    a clean no-op refusal — the card stays in ``review``, never flips to ``done``.
    This is the reviewer-bypass the guard exists to prevent; widening the merge
    path must not open it."""
    with kb.connect() as conn:
        tid = _review_lane_card(conn)

        ok = kb.complete_task(conn, tid, summary="worker tried to self-complete")

        assert ok is False, "a review card is not completable by a worker"
        task = kb.get_task(conn, tid)
        assert task.status == "review", "the card must stay in the review lane"
        assert task.completed_at is None, "no completion timestamp on a refusal"
        assert not [
            e for e in kb.list_events(conn, tid) if e.kind == "completed"
        ], "a refused completion must not emit a 'completed' event"


# ---------------------------------------------------------------------------
# RED 3 — the refusal message names the status + allowed set, and does NOT
#          claim a concurrent lane move that did not happen
# ---------------------------------------------------------------------------


def test_refusal_message_for_review_names_status_not_concurrency(
    kanban_home: Path,
) -> None:
    """When a completion fails purely because the card's status (``review``) is
    not in the completable set, the diagnosis must name the actual status and the
    allowed set — NOT assert a concurrent lane move (the misleading wording that
    cost real debugging time)."""
    with kb.connect() as conn:
        tid = _review_lane_card(conn)
        # Reproduce the exact false path: a non-override complete refuses.
        assert kb.complete_task(conn, tid, summary="tried") is False

        msg = kb.explain_completion_refusal(conn, tid)

        assert "review" in msg, "the message must name the actual status"
        assert (
            "moved lane concurrently" not in msg
        ), "must not assert a concurrent lane move that did not happen"
        # It should name the completable set so the reader knows why 'review' is
        # not completable by this path.
        assert (
            "running" in msg and "ready" in msg and "blocked" in msg
        ), "the message must name the completable (allowed) set"


# ---------------------------------------------------------------------------
# RED 4 — the rework path (ready -> review after a bounce) is UNCHANGED
# ---------------------------------------------------------------------------


def _stamp_owner_map(conn, tid: str, owner_map: str) -> None:
    body = (
        "[audit] actor=hollis stage=submit ts=2026-08-04T13:29:21Z\n"
        f"notes: state_owners={{{owner_map}}} triager=hollis team=engineering"
    )
    kb.add_comment(conn, tid, author="hollis", body=body)


def test_rework_ready_to_review_is_unchanged(kanban_home: Path) -> None:
    """An author completing from ``ready`` after a review bounce still MOVES the
    card to ``review`` + its review owner — the merge-path widening must not turn
    this handoff into a straight-to-done completion."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="reworked after bounce", assignee="eckert")
        _stamp_owner_map(
            conn, tid, "ready: eckert, review: lamport, blocked-acceptance: casey"
        )
        assert kb.get_task(conn, tid).status == "ready"

        ok = kb.complete_task(conn, tid, summary="rework done")

        assert ok is True, "the rework completion must succeed as a move"
        task = kb.get_task(conn, tid)
        assert task.status == "review", "rework must MOVE to review, not done"
        assert task.assignee == "lamport", "assignee is the card's review owner"
        assert task.completed_at is None, "a review move is not a completion"
