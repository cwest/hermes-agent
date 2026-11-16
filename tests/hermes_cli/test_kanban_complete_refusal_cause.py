"""A refused completion says why it was refused.

``complete_task`` returns a bare ``False`` for several distinct reasons, and
both callers named only one of them. The tool said "unknown id, stale run, or
already terminal" (and the CLI "unknown id or terminal state") for a card that
was neither unknown nor terminal: a worker whose run had been reclaimed, or a
card sitting in a lane completion does not accept. Refusal text is read as
evidence, so a guess sends the reader after a defect that is not there.
:func:`explain_completion_refusal` reads the cause from the card.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_completion_refusal as kbr


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_PROFILE", "dev")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return h


def _claimed(title: str = "card") -> tuple[str, int]:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=title, assignee="dev")
        kb.claim_task(conn, tid)
        return tid, kb._current_run_id(conn, tid)


def _reclaim_and_reclaim(tid: str) -> int:
    """The worker's run is reclaimed and the card claimed again under a new run."""
    with kbc.connect() as conn:
        assert kb.reclaim_task(conn, tid)
        kb.claim_task(conn, tid)
        return kb._current_run_id(conn, tid)


def _tool_complete(monkeypatch, tid: str, run_id: int) -> str:
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return json.loads(kt._handle_complete({"summary": "did the work"}))["error"]


def test_the_tool_names_a_stale_run_and_the_run_that_owns_the_card(home, monkeypatch) -> None:
    tid, old_run = _claimed()
    new_run = _reclaim_and_reclaim(tid)

    err = _tool_complete(monkeypatch, tid, old_run)

    assert f"run {old_run}" in err and f"run {new_run}" in err
    assert "unknown id" not in err and "terminal" not in err


def test_the_tool_names_a_lane_completion_does_not_accept(home, monkeypatch) -> None:
    tid, run = _claimed()
    with kbc.connect() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))

    err = _tool_complete(monkeypatch, tid, run)

    assert "'triage'" in err
    assert "unknown id" not in err


def test_the_cli_names_a_terminal_card_as_terminal(home, capsys) -> None:
    tid, _run = _claimed()
    with kbc.connect() as conn:
        assert kb.complete_task(conn, tid, summary="first", force=True)

    rc = kb_cli._cmd_complete(argparse.Namespace(
        task_ids=[tid], result=None, summary="again", metadata=None, force=False))

    assert rc != 0
    err = capsys.readouterr().err
    assert "already 'done'" in err


def test_the_cli_names_an_unknown_id_as_unknown(home, capsys) -> None:
    rc = kb_cli._cmd_complete(argparse.Namespace(
        task_ids=["t_00000000"], result=None, summary="x", metadata=None, force=False))
    assert rc != 0
    assert "no task t_00000000" in capsys.readouterr().err


def test_explain_reports_the_pr_acceptance_failure_the_guard_recorded(home) -> None:
    tid, _run = _claimed()
    with kbc.connect() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                     ("PR acceptance failing: required check red", tid))
    with kbc.connect() as conn:
        assert "PR acceptance failing" in kbr.explain_completion_refusal(conn, tid)


def test_explain_never_passes_off_an_old_crash_as_the_refusal(home) -> None:
    tid, _run = _claimed()
    with kbc.connect() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                     ("worker crashed: Traceback ...", tid))
    with kbc.connect() as conn:
        assert "crashed" not in kbr.explain_completion_refusal(conn, tid)
