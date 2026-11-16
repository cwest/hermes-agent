"""A kanban worker out of iteration budget leaves a resume note on its card.

Running out of budget records ``timed_out`` and releases the claim; the next
run starts cold. Without a note it has to reconstruct where the last run
stopped. The finalizer posts one ``[audit] ... stage=resume`` comment before
the terminal event: the budget used, the branch, the newest PR linked on the
card, the last tool the run called, and a next step.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import turn_finalizer as tf
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_BRANCH", raising=False)
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="long job", assignee="dev")
        kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def _agent(max_iterations: int = 90):
    return SimpleNamespace(
        max_iterations=max_iterations,
        iteration_budget=SimpleNamespace(remaining=0),
        quiet_mode=True,
        _emit_diagnostic_status=lambda *_a, **_k: None,
        _safe_print=lambda *_a, **_k: None,
        _handle_max_iterations=lambda _messages, _count: "summary",
    )


def _exhaust(messages, *, api_call_count: int = 90, failed: bool = False, exit_reason: str = "unknown"):
    return tf._resolve_budget_fallback(
        _agent(), final_response=None, api_call_count=api_call_count, interrupted=False,
        failed=failed, messages=messages, _turn_exit_reason=exit_reason,
        _pending_verification_response=None, _pending_verification_response_previewed=False,
        logger=tf.logging.getLogger("test"),
    )


def _tool_turn(name: str) -> list[dict]:
    return [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": name}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]


def _resume_notes(tid: str) -> list[str]:
    with kbc.connect() as conn:
        return [c.body for c in kb.list_comments(conn, tid) if "stage=resume" in c.body]


def test_budget_exhaustion_leaves_a_resume_note_before_timing_out(board, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_BRANCH", "topic/long-job")
    with kbc.connect() as conn:
        kb.add_comment(conn, board, author="dev", body="Opened https://github.com/o/r/pull/7 as a draft")
        kb.add_comment(conn, board, author="dev", body="Superseded by https://github.com/o/r/pull/8")

    _exhaust(_tool_turn("terminal"))

    [note] = _resume_notes(board)
    assert note.startswith("[audit] actor=worker stage=resume\nnotes: reason=budget_exhausted")
    for fact in ("budget_used=90", "budget_max=90", "branch=topic/long-job",
                 "artifact=https://github.com/o/r/pull/8", "last_tool=terminal", "next_step="):
        assert fact in note
    with kbc.connect() as conn:
        assert kb.get_task(conn, board).status != "running"  # the timeout was still recorded


def test_the_note_lands_before_the_terminal_event(board, monkeypatch) -> None:
    order: list[str] = []
    real_comment, real_record = kb.add_comment, tf._record_kanban_budget_exhausted

    def comment(*a, **k):
        order.append("note")
        return real_comment(*a, **k)

    def record(*a, **k):
        order.append("timed_out")
        return real_record(*a, **k)

    monkeypatch.setattr(kb, "add_comment", comment)
    monkeypatch.setattr(tf, "_record_kanban_budget_exhausted", record)
    _exhaust(_tool_turn("patch"))
    assert order == ["note", "timed_out"]


def test_the_note_names_only_what_the_run_knows(board) -> None:
    _exhaust([{"role": "user", "content": "do it"}], api_call_count=12)
    [note] = _resume_notes(board)
    assert "budget_used=12" in note
    for absent in ("branch=", "artifact=", "last_tool="):
        assert absent not in note


def test_a_failed_note_never_blocks_the_terminal_event(board, monkeypatch) -> None:
    def broken(*_a, **_k):
        raise RuntimeError("comment write failed")

    monkeypatch.setattr(kb, "add_comment", broken)
    _exhaust(_tool_turn("terminal"))
    with kbc.connect() as conn:
        task = kb.get_task(conn, board)
    assert task.status != "running"


def test_no_note_when_the_budget_is_not_exhausted(board) -> None:
    tf._resolve_budget_fallback(
        SimpleNamespace(max_iterations=90, iteration_budget=SimpleNamespace(remaining=5), quiet_mode=True),
        final_response="done", api_call_count=40, interrupted=False, failed=False,
        messages=_tool_turn("terminal"), _turn_exit_reason="text_response(finish_reason=stop)",
        _pending_verification_response=None, _pending_verification_response_previewed=False,
        logger=tf.logging.getLogger("test"),
    )
    assert _resume_notes(board) == []


def test_no_note_outside_a_dispatched_worker(board, monkeypatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    _exhaust(_tool_turn("terminal"))
    assert _resume_notes(board) == []
