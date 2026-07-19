"""The reviewer PASS -> acceptance transition is a single ATOMIC primitive.

Card t_b22fe0ba. The bug: a reviewer PASS set ``assignee=casey`` but did NOT flip
``status`` to ``blocked`` (or did the two halves as separate, non-atomic steps),
so a PASS'd card stranded in ``review``/casey — the reviewer's lane with the
acceptance owner's name on it — and, because the acceptance notification rides the
``blocked`` event, no ping fired. The fix: one atomic acceptance primitive
(``accept_task``) that flips ``status -> blocked``, sets ``assignee`` = the
acceptance owner, and emits the ``blocked``/``awaiting-casey-signoff`` event in a
SINGLE transaction. The reviewer PASS path calls it so the transition can never
land half-done.

The invariant, stated once: **a reviewer PASS lands the card in ``blocked`` + the
acceptance owner, atomically, with a single ``blocked``/``awaiting-casey-signoff``
event — no ``review``/owner stranding, and the acceptance ping fires.**
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


_PR_URL = "https://github.com/cwest/hermes-agent/pull/71"
_PASS_GIST = (
    f"awaiting-casey-signoff: reviewed PASS — {_PR_URL}; threads resolved; "
    "240 tests green. Ready to merge."
)


def _stage_reviewer_running(
    conn, *, author: str = "eckert", reviewer: str = "lamport",
    acceptance_owner: str = "casey", stamp_owner_map: bool = True,
) -> str:
    """Reproduce the live flow up to the reviewer holding the card in ``running``.

    Author builds + opens PR -> card MOVES to ``review`` + reviewer -> the
    reviewer CLAIMS it (``review -> running``). This is the exact state a reviewer
    PASSes from. When ``stamp_owner_map`` is set, the submit-stage §9.1 audit
    comment carries the materialized owner map so the acceptance owner is
    resolvable from the board alone.
    """
    tid = kb.create_task(conn, title="feature work", assignee=author)
    if stamp_owner_map:
        kb.add_comment(
            conn, tid, author="hollis",
            body=(
                "[audit] actor=hollis stage=submit\n"
                f"notes: state_owners={{ready: {author}, review: {reviewer}, "
                f"blocked-acceptance: {acceptance_owner}}}"
            ),
        )
    kb.claim_task(conn, tid)
    kb.complete_task(conn, tid, result=f"PR opened: {_PR_URL}")
    # Build -> review hop: status flips to review, reviewer takes ownership.
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='review', assignee=? WHERE id=?",
            (reviewer, tid),
        )
        kb._append_event(
            conn, tid, "status_changed",
            {"from": "ready", "to": "review", "by": "onecard:move_card"},
        )
        kb._append_event(
            conn, tid, "assigned",
            {"from": author, "to": reviewer, "by": "onecard:move_card"},
        )
    # Reviewer claims the review card: review -> running.
    claimed = kb.claim_review_task(conn, tid, claimer="lamport-host:1")
    assert claimed is not None, "reviewer must claim the review card"
    task = kb.get_task(conn, tid)
    assert task.status == "running" and task.assignee == reviewer
    return tid


# ---------------------------------------------------------------------------
# RED 1 — accept_task lands blocked + owner + the blocked event, atomically
# ---------------------------------------------------------------------------


def test_accept_task_lands_blocked_and_owner_atomically(kanban_home: Path) -> None:
    """A reviewer PASS via ``accept_task`` must land the card ``blocked`` + the
    acceptance owner in a SINGLE transition, emitting the ``blocked`` event that
    fires the acceptance ping."""
    with kb.connect() as conn:
        tid = _stage_reviewer_running(conn)

        ok = kb.accept_task(
            conn, tid, acceptance_owner="casey", reason=_PASS_GIST,
        )

        assert ok is True, "accept_task must succeed from the reviewer's running state"
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "PASS must land the card in the acceptance lane"
        assert task.assignee == "casey", "PASS must hand the card to the acceptance owner"
        # No ``review``/owner stranding: the card is never left in review with the
        # acceptance owner's name.
        assert not (task.status == "review"), "must not strand in review"


def test_accept_task_emits_single_blocked_signoff_event(kanban_home: Path) -> None:
    """The atomic accept emits exactly ONE ``blocked`` event carrying the
    ``awaiting-casey-signoff`` reason — the event the acceptance notifier pings on
    (``kanban_watchers`` fires on ``kind == 'blocked'``)."""
    with kb.connect() as conn:
        tid = _stage_reviewer_running(conn)

        kb.accept_task(conn, tid, acceptance_owner="casey", reason=_PASS_GIST)

        events = kb.list_events(conn, tid)
        blocked = [e for e in events if e.kind == "blocked"]
        assert len(blocked) == 1, "exactly one blocked event fires the acceptance ping"
        reason = (blocked[0].payload or {}).get("reason", "")
        assert "awaiting-casey-signoff" in reason, \
            "the acceptance block reason must be awaiting-casey-signoff"
        # The sticky-block reader (what the guards / auto-router key on) sees it too.
        sticky = kb._latest_sticky_block_reason(conn, tid)
        assert sticky and "awaiting-casey-signoff" in sticky


def test_accept_task_normalizes_bare_reason_to_signoff(kanban_home: Path) -> None:
    """A caller reason lacking the prefix is normalized so the emitted block
    reason always starts with ``awaiting-casey-signoff`` (the token guards key on)."""
    with kb.connect() as conn:
        tid = _stage_reviewer_running(conn)

        kb.accept_task(
            conn, tid, acceptance_owner="casey",
            reason=f"reviewed PASS — {_PR_URL}; ready to merge",
        )

        sticky = kb._latest_sticky_block_reason(conn, tid)
        assert sticky and sticky.startswith("awaiting-casey-signoff"), sticky


def test_accept_task_ends_the_review_run(kanban_home: Path) -> None:
    """The atomic accept must end the reviewer's run (release the claim), so the
    dispatcher does not treat the card as still-claimed."""
    with kb.connect() as conn:
        tid = _stage_reviewer_running(conn)

        kb.accept_task(conn, tid, acceptance_owner="casey", reason=_PASS_GIST)

        task = kb.get_task(conn, tid)
        assert task.claim_lock is None, "the reviewer's claim must be released"
        run = kb.latest_run(conn, tid)
        assert run is not None and run.status != "running", \
            "the reviewer's run must be ended, not left running"


def test_accept_task_refuses_a_non_review_running_card(kanban_home: Path) -> None:
    """accept_task only transitions a card that is under review (``review`` or a
    reviewer-claimed ``running``). A plain ``ready`` card is not acceptable."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="not in review", assignee="eckert")
        # ready, never reviewed
        ok = kb.accept_task(conn, tid, acceptance_owner="casey", reason=_PASS_GIST)
        assert ok is False, "accept_task must refuse a card that is not under review"
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.assignee == "eckert"


def test_accept_task_transitions_straight_from_review_lane(kanban_home: Path) -> None:
    """accept_task also accepts a card sitting in the ``review`` lane (unclaimed),
    covering a reviewer/orchestrator that parks acceptance without a running claim."""
    with kb.connect() as conn:
        tid = _stage_reviewer_running(conn)
        # Drop the card back to the review lane (no running claim) to model the
        # unclaimed-review acceptance path.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='review', claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL WHERE id=?",
                (tid,),
            )
            kb._append_event(
                conn, tid, "status_changed",
                {"from": "running", "to": "review", "by": "test"},
            )

        ok = kb.accept_task(conn, tid, acceptance_owner="casey", reason=_PASS_GIST)

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "blocked" and task.assignee == "casey"
        blocked = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"]
        assert len(blocked) == 1


# ---------------------------------------------------------------------------
# RED 2 — the reviewer PASS toolset path routes to the atomic primitive
# ---------------------------------------------------------------------------


def test_reviewer_block_signoff_routes_to_atomic_acceptance(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reviewer ``kanban_block(reason="awaiting-casey-signoff: …")`` on the card
    it is reviewing must land the card ``blocked`` + the acceptance owner (resolved
    from the owner map) atomically — NOT ``blocked`` + the reviewer's own name."""
    import json as _json
    from tools import kanban_tools

    with kb.connect() as conn:
        tid = _stage_reviewer_running(conn)

    # The reviewer worker owns the task + holds its run.
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setattr(kanban_tools, "_enforce_worker_task_ownership",
                        lambda _tid: None)
    monkeypatch.setattr(kanban_tools, "_worker_run_id", lambda _tid: None)

    res = kanban_tools._handle_block(
        {"task_id": tid, "reason": _PASS_GIST}
    )
    payload = _json.loads(res)
    assert payload.get("ok") is True, payload

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "reviewer PASS must land in the acceptance lane"
        assert task.assignee == "casey", \
            "reviewer PASS must hand the card to the acceptance owner, not keep it"
        blocked = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"]
        assert len(blocked) == 1
        assert "awaiting-casey-signoff" in (blocked[0].payload or {}).get("reason", "")


def test_reviewer_block_non_signoff_is_unchanged(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NON-acceptance block (e.g. a needs_input block) is untouched by the
    acceptance routing — it still lands blocked with the reviewer keeping the card,
    proving the routing keys strictly on the awaiting-casey-signoff reason."""
    import json as _json
    from tools import kanban_tools

    with kb.connect() as conn:
        tid = _stage_reviewer_running(conn)

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setattr(kanban_tools, "_enforce_worker_task_ownership",
                        lambda _tid: None)
    monkeypatch.setattr(kanban_tools, "_worker_run_id", lambda _tid: None)

    res = kanban_tools._handle_block(
        {"task_id": tid, "reason": "needs_input: which config key?",
         "kind": "needs_input"}
    )
    payload = _json.loads(res)
    assert payload.get("ok") is True, payload

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        # Non-acceptance block: the reviewer keeps the card (no acceptance handoff).
        assert task.assignee == "lamport", \
            "a non-signoff block must not hand the card to the acceptance owner"
