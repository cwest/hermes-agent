"""Tests for the budget-exhaustion resume note (structured handoff).

When a dispatched kanban worker exhausts its iteration budget, the run ends by
recording an explicit, machine-legible resume note on the card BEFORE the
terminal ``timed_out`` event. The re-claiming run reads that note from the
card's comment thread instead of inferring where the prior run stopped.

The note follows the existing ``[audit]`` comment convention (the same shape the
one-card move helpers use), so a re-claim can act on it without parsing prose.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.turn_finalizer import finalize_turn


class _LimitAgent:
    def __init__(self, *, max_iterations=90, budget_remaining=0):
        self.max_iterations = max_iterations
        self.iteration_budget = SimpleNamespace(
            remaining=budget_remaining, used=max_iterations, max_total=max_iterations
        )
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self.persisted_messages = None

    def _handle_max_iterations(self, messages, api_call_count):
        return "summary from extra call"

    def _emit_status(self, *_a, **_k):
        pass

    def _safe_print(self, *_a, **_k):
        pass

    def _save_trajectory(self, *_a, **_k):
        pass

    def _cleanup_task_resources(self, *_a, **_k):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        self.persisted_messages = list(messages)

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _format_turn_completion_explanation(self, _reason):
        return "explanation"

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_k):
        pass


def _finalize(agent, *, final_response, exit_reason, api_call_count=90,
              interrupted=False, failed=False, messages=None):
    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=api_call_count,
        interrupted=interrupted,
        failed=failed,
        messages=messages if messages is not None else [{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason=exit_reason,
    )


@pytest.fixture(autouse=True)
def _no_plugin_hooks(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_k: [])


def _wire_kanban(monkeypatch, task_id="t_ec877de7"):
    """Patch the kanban DB seam and capture add_comment + _record_task_failure.

    Returns (comment_mock, record_mock, order) where ``order`` records the
    interleaving of comment/terminal calls so a test can assert the note is
    posted BEFORE the terminal event.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    order = []
    conn = SimpleNamespace(close=lambda: None)
    comment = MagicMock(name="add_comment", side_effect=lambda *a, **k: order.append("comment"))
    record = MagicMock(name="record_task_failure", side_effect=lambda *a, **k: order.append("record"))
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: conn)
    monkeypatch.setattr("hermes_cli.kanban_db.add_comment", comment)
    monkeypatch.setattr("hermes_cli.kanban_db._record_task_failure", record)
    monkeypatch.setattr("hermes_cli.kanban_db.list_comments", lambda *_a, **_k: [])
    return comment, record, order


def test_budget_exhaustion_records_resume_note_with_artifact_and_next_step(monkeypatch):
    """A budget-exhausted kanban run records a resume note naming the in-flight
    artifact and a concrete next step, in the ``[audit]`` convention."""
    comment, record, _order = _wire_kanban(monkeypatch)
    # A prior comment on the card carries the in-flight PR URL (the
    # ready-for-review handoff the prior run posted before running out).
    pr_url = "https://github.com/cwest/knowledge-base/pull/449"
    monkeypatch.setattr(
        "hermes_cli.kanban_db.list_comments",
        lambda *_a, **_k: [SimpleNamespace(body=f"PR opened: {pr_url}")],
    )
    monkeypatch.setenv("HERMES_KANBAN_BRANCH", "topic/curation-drift")

    agent = _LimitAgent()
    messages = [
        {"role": "user", "content": "curate the knowledge base"},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "terminal"}}]},
        {"role": "tool", "content": "..."},
    ]
    _finalize(agent, final_response=None, exit_reason="unknown", messages=messages)

    assert comment.call_count == 1, "expected exactly one resume-note comment"
    body = comment.call_args.kwargs.get("body") or comment.call_args.args[3]
    # Structured, audit-convention shape — not free prose.
    assert body.lstrip().startswith("[audit]")
    assert "stage=resume" in body
    # Names the in-flight artifact and the budget it ran out of.
    assert pr_url in body
    assert "topic/curation-drift" in body
    assert "90/90" in body or "budget_used=90" in body
    # Carries a concrete next-step directive for the re-claim.
    assert "next_step" in body


def test_resume_note_posted_before_terminal_event(monkeypatch):
    """The note must land on the card BEFORE the terminal timeout is recorded,
    so it is present when the dispatcher's re-claim reads the thread."""
    comment, record, order = _wire_kanban(monkeypatch)
    agent = _LimitAgent()
    _finalize(agent, final_response=None, exit_reason="unknown")

    assert order == ["comment", "record"], (
        f"resume note must precede the terminal event; got order {order}"
    )
    record.assert_called_once()


def test_no_resume_note_on_clean_completion(monkeypatch):
    """A run that ends with a normal text response emits NO resume note."""
    comment, record, _order = _wire_kanban(monkeypatch)
    agent = _LimitAgent(budget_remaining=5)

    _finalize(
        agent,
        final_response="here is the finished work",
        exit_reason="text_response(finish_reason=stop)",
        api_call_count=40,
    )

    comment.assert_not_called()
    record.assert_not_called()


def test_no_resume_note_on_provider_failure(monkeypatch):
    """A run that ends because of a provider crash (failed=True) is not a
    budget exhaustion and must not emit a spurious resume note."""
    comment, record, _order = _wire_kanban(monkeypatch)
    agent = _LimitAgent()

    _finalize(
        agent,
        final_response=None,
        exit_reason="provider_failure",
        failed=True,
    )

    comment.assert_not_called()
    record.assert_not_called()


def test_no_resume_note_outside_kanban(monkeypatch):
    """A budget-exhausted run that is NOT a kanban worker records no note."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    comment = MagicMock(name="add_comment")
    monkeypatch.setattr("hermes_cli.kanban_db.add_comment", comment)
    agent = _LimitAgent()

    _finalize(agent, final_response=None, exit_reason="unknown")

    comment.assert_not_called()
