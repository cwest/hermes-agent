"""Regression tests: ``complete_task`` must refuse an acceptance-lane card.

``done`` on the board means exactly one thing — Casey merged/accepted the work.
That invariant was enforced only by convention, and convention failed once
(2026-07-02): a worker correctly parked a PASS'd card in the acceptance lane
(``blocked`` + owner ``casey``, reason ``awaiting-casey-signoff``) then in the
same run also called ``kanban_complete``, flipping the acceptance card straight
to ``done`` on a PR that was never merged.

The hole was ``complete_task``'s UPDATE clause: ``WHERE ... status IN
('running','ready','blocked')`` — because ``blocked`` is completable, any caller
(the worker ``kanban_complete`` tool, ``hermes kanban complete``, the swarm root
helper) could turn an awaiting-Casey-signoff card into ``done``.

These tests pin the contract on the real ``complete_task`` path:

* An acceptance-lane card (``blocked`` + ``awaiting-casey-signoff``) is REFUSED —
  ``complete_task`` returns ``False``, the card stays ``blocked``, and an audit
  event (``completion_refused_acceptance``) records the rejected attempt.
* Casey's merge — the ONE legitimate completer — passes
  ``allow_acceptance_complete=True`` and DOES land the card in ``done``.
* A ``blocked`` card whose sticky reason is NOT ``awaiting-casey-signoff`` (e.g.
  a generic ``needs_input`` / review-changes-requested block) stays completable
  exactly as before — no regression to the manual-complete-a-stuck-card flow.
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


_SIGNOFF_REASON = (
    "awaiting-casey-signoff: reviewed PASS — "
    "https://github.com/cwest/hermes-agent/pull/71; threads resolved; 240 tests "
    "green. Ready to merge."
)


def _stage_acceptance_card(conn, *, reason: str = _SIGNOFF_REASON) -> str:
    """Leave a card in the acceptance lane: ``blocked`` with a sticky
    ``blocked`` event carrying ``reason``, exactly as a reviewer PASS parks it."""
    tid = kb.create_task(conn, title="feature work", assignee="casey")
    kb.claim_task(conn, tid)
    assert kb.block_task(
        conn, tid, reason=reason,
        expected_run_id=kb.get_task(conn, tid).current_run_id,
    )
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    # The sticky reason must be what the guard keys on.
    assert kb._latest_sticky_block_reason(conn, tid) == reason
    return tid


# ---------------------------------------------------------------------------
# RED 1 — completion of an acceptance card is refused (the whole bug class)
# ---------------------------------------------------------------------------


def test_complete_refuses_acceptance_lane_card(kanban_home: Path) -> None:
    """A worker/CLI ``complete`` on an ``awaiting-casey-signoff`` card is a clean
    no-op refusal: returns False, card stays ``blocked`` + ``casey``, no ``done``."""
    with kb.connect() as conn:
        tid = _stage_acceptance_card(conn)

        ok = kb.complete_task(conn, tid, summary="worker tried to complete")

        assert ok is False, "acceptance card must not be completable by default"
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "card must stay in the acceptance lane"
        assert task.assignee == "casey", "acceptance owner is unchanged"
        assert task.completed_at is None, "no completion timestamp on a refusal"


def test_complete_refusal_emits_audit_event(kanban_home: Path) -> None:
    """The rejected completion attempt is auditable in the event trail."""
    with kb.connect() as conn:
        tid = _stage_acceptance_card(conn)
        kb.complete_task(conn, tid, summary="worker tried to complete")

        events = kb.list_events(conn, tid)
        refusals = [e for e in events if e.kind == "completion_refused_acceptance"]
        assert refusals, "a completion_refused_acceptance event must be recorded"
        # No 'completed' event should have landed.
        assert not [e for e in events if e.kind == "completed"], \
            "a refused completion must not emit a 'completed' event"


# ---------------------------------------------------------------------------
# RED 2 — Casey's merge (the one legit completer) DOES complete via override
# ---------------------------------------------------------------------------


def test_merge_override_completes_acceptance_card(kanban_home: Path) -> None:
    """The merge path passes ``allow_acceptance_complete=True`` and lands the
    acceptance card in ``done`` — Casey's merge still works end to end."""
    with kb.connect() as conn:
        tid = _stage_acceptance_card(conn)

        ok = kb.complete_task(
            conn, tid,
            summary="merged by Casey",
            allow_acceptance_complete=True,
        )

        assert ok is True, "the merge override must complete the acceptance card"
        task = kb.get_task(conn, tid)
        assert task.status == "done", "override must land the card in done"
        assert task.completed_at is not None


# ---------------------------------------------------------------------------
# RED 3 — a non-acceptance blocked card stays completable (no regression)
# ---------------------------------------------------------------------------


def test_generic_blocked_card_still_completable(kanban_home: Path) -> None:
    """A ``blocked`` card whose reason is NOT ``awaiting-casey-signoff`` (here a
    generic ``needs_input`` review-required park) is STILL completable via
    ``complete_task`` — the manual-complete-a-stuck-card flow is unchanged."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="genuinely stuck", assignee="eckert")
        kb.claim_task(conn, tid)
        assert kb.block_task(
            conn, tid,
            reason="review-required: please verify the ACL change",
            kind="needs_input",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        ok = kb.complete_task(conn, tid, summary="resolved out of band")

        assert ok is True, "a non-acceptance blocked card must stay completable"
        assert kb.get_task(conn, tid).status == "done"


def test_review_bounce_blocked_card_still_completable(kanban_home: Path) -> None:
    """A ``review-changes-requested`` bounce block is also non-acceptance and
    must remain completable — the guard keys on the signoff reason, not on
    ``blocked`` status generally."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="bounced back", assignee="eckert")
        kb.claim_task(conn, tid)
        assert kb.block_task(
            conn, tid,
            reason="review-changes-requested: fix the guard; see PR #71.",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        assert kb.complete_task(conn, tid, summary="author completed") is True
        assert kb.get_task(conn, tid).status == "done"
