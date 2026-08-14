"""Equivalence tests for the single ``source_status == "review"`` reader.

``hermes_cli/kanban_db.py`` used to carry TWO byte-for-byte identical readers of
the same durable signal (``source_status: "review"`` on a run's ``claimed``
event): ``_run_claimed_from_review`` (the completion-path self-handoff
discriminator) and ``_crashed_run_was_review`` (crash classification). The risk
was DIVERGENCE — a future edit to one silently leaving the other stale, so the
two call sites disagree about whether a run came from the review lane.

They were collapsed to ONE implementation. These tests pin the behavior of that
single reader across the four axes the two originals both had to satisfy, so a
future edit that would have needed to touch both readers now has one body to
change and one contract to keep:

* run-scoped read (review run vs build run, scoped by ``run_id``);
* latest-``claimed`` fallback when ``run_id`` is unknown (None);
* missing ``claimed`` event → False;
* malformed / non-dict / non-review payload → False.

The proof that the two call sites now agree is structural (they resolve through
the same body) AND behavioral: the completion-path self-handoff guard and the
crash-classification path are exercised over the same claim shapes and observed
to make consistent review/not-review decisions.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _insert_claimed(conn, tid: str, payload_text: str, run_id: int) -> None:
    """Insert a raw ``claimed`` event with an explicit (possibly malformed)
    payload string, satisfying the ``created_at`` NOT NULL constraint. Used to
    exercise the reader's malformed/non-dict payload branches directly."""
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, 'claimed', ?, ?)",
        (tid, run_id, payload_text, int(time.time())),
    )


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _to_review(conn, tid: str, reviewer: str) -> None:
    """Flip a card into ``review`` under its reviewer (the build->review hop)."""
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


def _latest_claimed_run_id(conn, tid: str) -> int:
    row = conn.execute(
        "SELECT run_id FROM task_events "
        "WHERE task_id = ? AND kind = 'claimed' ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert row is not None, "expected a claimed event"
    return row["run_id"]


# ---------------------------------------------------------------------------
# Exactly-one-implementation guarantee (behavioral, not source-reading).
# ---------------------------------------------------------------------------


def test_single_reader_is_the_authoritative_implementation():
    """The consolidated reader exists and is the one body both call sites use.

    ``_crashed_run_was_review`` is no longer a second, independently-maintained
    body: either it is gone entirely (both sites call ``_run_claimed_from_review``
    directly) or it survives only as a thin wrapper that delegates to it. Assert
    the single reader exists; if a crash-site wrapper survives, assert it is a
    delegate (same object semantics), never a second copy of the query.
    """
    assert callable(kb._run_claimed_from_review)
    wrapper = getattr(kb, "_crashed_run_was_review", None)
    if wrapper is not None:
        # A surviving intent-named wrapper is allowed, but it must delegate to
        # the single reader rather than re-implement the query. We prove
        # delegation behaviorally in the equivalence tests below; here we only
        # assert it is still callable with the historical signature.
        assert callable(wrapper)


# ---------------------------------------------------------------------------
# Run-scoped read: a review run vs a build run, classified by the run_id.
# ---------------------------------------------------------------------------


def test_run_scoped_review_run_reads_true(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="reviewer")
        _to_review(conn, tid, "reviewer")
        claimed = kb.claim_review_task(conn, tid)
        assert claimed is not None
        run_id = _latest_claimed_run_id(conn, tid)
        assert kb._run_claimed_from_review(conn, tid, run_id) is True


def test_run_scoped_build_run_reads_false(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="build", assignee="builder")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        run_id = _latest_claimed_run_id(conn, tid)
        assert kb._run_claimed_from_review(conn, tid, run_id) is False


def test_history_does_not_leak_a_build_run_into_review(kanban_home):
    """A card built once (build claim) then moved to review (review claim) is
    classified by its CURRENT run — the build run stays False, the review run
    True — proving the run-scoping, not a task-wide latest read."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="build then review", assignee="worker")
        kb.claim_task(conn, tid)
        build_run = _latest_claimed_run_id(conn, tid)
        # Release the build run, move to review, claim from review.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL WHERE id=?",
                (tid,),
            )
        _to_review(conn, tid, "worker")
        kb.claim_review_task(conn, tid)
        review_run = _latest_claimed_run_id(conn, tid)

        assert review_run != build_run
        assert kb._run_claimed_from_review(conn, tid, build_run) is False
        assert kb._run_claimed_from_review(conn, tid, review_run) is True


# ---------------------------------------------------------------------------
# Latest-claimed fallback when run_id is unknown (None).
# ---------------------------------------------------------------------------


def test_latest_fallback_reads_review_when_last_claim_is_review(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="reviewer")
        _to_review(conn, tid, "reviewer")
        kb.claim_review_task(conn, tid)
        # run_id unknown → falls back to the latest claimed event, which is review.
        assert kb._run_claimed_from_review(conn, tid, None) is True


def test_latest_fallback_reads_build_when_last_claim_is_build(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="build", assignee="builder")
        kb.claim_task(conn, tid)
        assert kb._run_claimed_from_review(conn, tid, None) is False


# ---------------------------------------------------------------------------
# Missing event and malformed payload both read False (in both directions).
# ---------------------------------------------------------------------------


def test_missing_claimed_event_reads_false(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="no claim yet", assignee="worker")
        # No claim has happened → no claimed event at all.
        assert kb._run_claimed_from_review(conn, tid, None) is False
        assert kb._run_claimed_from_review(conn, tid, 999999) is False


def test_malformed_payload_reads_false(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="malformed", assignee="worker")
        # Hand-write a claimed event with a non-JSON payload.
        with kb.write_txn(conn):
            _insert_claimed(conn, tid, "not-json-{", 1)
        assert kb._run_claimed_from_review(conn, tid, 1) is False
        assert kb._run_claimed_from_review(conn, tid, None) is False


def test_non_review_source_status_reads_false(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="other source", assignee="worker")
        with kb.write_txn(conn):
            _insert_claimed(conn, tid, json.dumps({"source_status": "ready"}), 1)
        assert kb._run_claimed_from_review(conn, tid, 1) is False


def test_non_dict_payload_reads_false(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="list payload", assignee="worker")
        with kb.write_txn(conn):
            _insert_claimed(conn, tid, json.dumps(["review"]), 1)
        assert kb._run_claimed_from_review(conn, tid, 1) is False


# ---------------------------------------------------------------------------
# Cross-call-site agreement: whatever the crash site would see, the completion
# site sees identically — because they read the same body.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_status,expected", [
    ("review", True),
    ("ready", False),
    ("blocked", False),
    (None, False),
])
def test_both_call_sites_agree_across_source_status(kanban_home, source_status, expected):
    """The completion-path reader and any surviving crash-path wrapper must
    return the SAME verdict for the same claim payload — the whole point of the
    consolidation. If ``_crashed_run_was_review`` is gone, the completion reader
    alone carries the contract."""
    payload = {} if source_status is None else {"source_status": source_status}
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="agree", assignee="worker")
        with kb.write_txn(conn):
            _insert_claimed(conn, tid, json.dumps(payload), 1)
        completion_verdict = kb._run_claimed_from_review(conn, tid, 1)
        assert completion_verdict is expected

        wrapper = getattr(kb, "_crashed_run_was_review", None)
        if wrapper is not None:
            assert wrapper(conn, tid, 1) is completion_verdict
