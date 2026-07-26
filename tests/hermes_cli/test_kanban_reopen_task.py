"""Regression tests: the sanctioned un-done reconciliation path.

``done`` on the board is terminal and means exactly one thing — Casey
merged/accepted the work. ``move_card`` refuses to move a ``done`` card and there
was no sanctioned verb to walk a card back out of ``done``. When a card reaches
``done`` by a route OTHER than Casey's merge/accept (the ``t_baaa247f``
false-``done``: a worker's ``kanban_complete`` self-completed a review-eligible
card past its reviewer), there was no audited way to reconcile it.

``reopen_task`` is that path: it walks a ``done`` card back to a live lane
(default ``todo``, or an explicit target), records WHY via a mandatory reason on
an audited ``reopened`` event, and clears the completion timestamp. It refuses a
non-``done`` card (nothing to reconcile) and an archived card (archived is
terminal by a different route).
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


def _done_card(conn) -> str:
    """A card sitting at ``done`` (via Casey's merge override, so setup itself
    doesn't depend on the very fall-through under test)."""
    tid = kb.create_task(conn, title="reached done off-merge", assignee="eckert")
    kb.claim_task(conn, tid)
    kb.complete_task(conn, tid, summary="self-completed", allow_acceptance_complete=True)
    assert kb.get_task(conn, tid).status == "done"
    return tid


def test_reopen_walks_done_card_back_to_todo(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = _done_card(conn)

        ok = kb.reopen_task(conn, tid, reason="false-done: self-completed past reviewer")

        assert ok is True
        task = kb.get_task(conn, tid)
        # Reopened to ``todo``; recompute_ready then auto-promotes a parentless
        # todo card to ``ready`` (the normal lifecycle) — either live lane is
        # correct, the point is it left the terminal ``done``.
        assert task.status in ("todo", "ready"), "reopen returns the card to a live lane"
        assert task.completed_at is None, "the completion timestamp is cleared"


def test_reopen_can_target_an_explicit_status(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = _done_card(conn)

        ok = kb.reopen_task(
            conn, tid, reason="route to review", to_status="review", assignee="lamport",
        )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        assert task.assignee == "lamport"


def test_reopen_records_an_audited_reason(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = _done_card(conn)
        kb.reopen_task(conn, tid, reason="false-done: self-completed past reviewer")

        events = kb.list_events(conn, tid)
        reopened = [e for e in events if e.kind == "reopened"]
        assert reopened, "a reopened event must be recorded"
        assert reopened[0].payload.get("reason") == (
            "false-done: self-completed past reviewer"
        )
        assert reopened[0].payload.get("from") == "done"


def test_reopen_requires_a_reason(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = _done_card(conn)
        with pytest.raises(ValueError):
            kb.reopen_task(conn, tid, reason="")


def test_reopen_refuses_a_non_done_card(kanban_home: Path) -> None:
    """Only a ``done`` card is reconcilable via reopen — a live card is a no-op
    (returns False, no mutation)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="still running", assignee="eckert")
        kb.claim_task(conn, tid)
        assert kb.get_task(conn, tid).status == "running"

        ok = kb.reopen_task(conn, tid, reason="should not apply")
        assert ok is False
        assert kb.get_task(conn, tid).status == "running"


def test_reopen_rejects_an_invalid_target_status(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = _done_card(conn)
        with pytest.raises(ValueError):
            kb.reopen_task(conn, tid, reason="x", to_status="bogus")
