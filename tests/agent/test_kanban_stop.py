"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import pytest

from agent.kanban_stop import (
    KANBAN_LANE_EXIT_TOOLS,
    KANBAN_NON_EXIT_TOOLS,
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_disabled_without_kanban_task(clear_kanban_env):
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_enabled_with_kanban_task(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    assert kanban_stop_nudge_enabled() is True


def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_no_nudge_after_kanban_block(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {"role": "tool", "name": "kanban_block", "tool_call_id": "1", "content": "blocked"},
    ]
    assert build_kanban_stop_nudge(messages=messages) is None


def _called(tool_name: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": tool_name, "tool_call_id": "1", "content": "ok"},
    ]


@pytest.mark.parametrize(
    "tool_name", ["kanban_submit_for_review", "kanban_request_changes"]
)
def test_no_nudge_after_lane_exit_handoff(clear_kanban_env, tool_name):
    """A worker that handed the card to another lane has exited correctly."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = _called(tool_name)
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


@pytest.mark.parametrize(
    "tool_name", ["kanban_submit_for_review", "kanban_request_changes"]
)
def test_lane_exit_detected_from_assistant_tool_call_alone(clear_kanban_env, tool_name):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    assert build_kanban_stop_nudge(messages=_called(tool_name)[:1]) is None


def test_non_exit_kanban_tools_still_nudge(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    for name in ("kanban_comment", "kanban_heartbeat", "kanban_show"):
        assert build_kanban_stop_nudge(messages=_called(name)) is not None, name


def test_nudge_text_offers_every_lane_exit(clear_kanban_env):
    """The nudge must not present complete/block as the only exits — that
    pushes a reviewed-handoff worker to mark unmerged work done."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    nudge = build_kanban_stop_nudge(messages=[], attempts=0)
    assert nudge is not None
    for name in KANBAN_LANE_EXIT_TOOLS:
        assert f"`{name}" in nudge, name


def test_every_registered_kanban_tool_is_classified():
    """Partition invariant: each registered kanban_* tool is declared either a
    lane exit or a non-exit, so a future lane-exit tool can't silently
    reopen the false-nudge bug by being left out of the terminal set."""
    import tools.kanban_tools  # noqa: F401  ensure registered
    from tools.registry import registry

    registered = {n for n in registry.get_all_tool_names() if n.startswith("kanban_")}
    assert KANBAN_LANE_EXIT_TOOLS.isdisjoint(KANBAN_NON_EXIT_TOOLS)
    unclassified = registered - (KANBAN_LANE_EXIT_TOOLS | KANBAN_NON_EXIT_TOOLS)
    assert not unclassified, (
        f"classify {sorted(unclassified)} in agent/kanban_stop.py as a lane "
        "exit (KANBAN_LANE_EXIT_TOOLS) or not (KANBAN_NON_EXIT_TOOLS)"
    )
    assert KANBAN_LANE_EXIT_TOOLS <= registered


def test_nudge_budget_exhausted(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    assert build_kanban_stop_nudge(messages=[], attempts=2) is None
    assert build_kanban_stop_nudge(messages=[], attempts=1, max_attempts=1) is None
    assert build_kanban_stop_nudge(messages=[], attempts=0, max_attempts=1) is not None


# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.


def test_nudge_text_warns_about_blocking(clear_kanban_env):
    """The nudge should warn that repeated violations will block the task."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    nudge = build_kanban_stop_nudge(messages=[], attempts=0)
    assert nudge is not None
    assert "block" in nudge.lower(), (
        "nudge should warn that repeated violations will block the task"
    )


def test_nudge_and_dispatcher_budgets_are_independent(clear_kanban_env):
    """Agent-side nudge budget (2) and dispatcher-side streak (3) are
    separate budgets — the nudge counter does not affect the dispatcher's
    violation streak, and vice versa.

    This is a source-level invariant check: the nudge counter
    (``_kanban_stop_nudges``) lives on the AIAgent instance and resets
    per session, while the dispatcher streak lives in the task_runs DB
    table and persists across worker respawns.
    """
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    # Agent-side: 2 nudge attempts per session
    assert build_kanban_stop_nudge(messages=[], attempts=0) is not None
    assert build_kanban_stop_nudge(messages=[], attempts=1) is not None
    assert build_kanban_stop_nudge(messages=[], attempts=2) is None
    # Dispatcher-side streak is tracked in the DB, not in the nudge module —
    # the nudge module has no knowledge of the streak counter.
    assert not hasattr(build_kanban_stop_nudge, "_streak")


def test_nudge_does_not_tie_submit_for_review_to_opening_a_pr(clear_kanban_env):
    """A draft-PR workflow ends at a handoff comment; the submit_for_review
    bullet must not contradict that by naming PR-open as its trigger."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    nudge = build_kanban_stop_nudge(messages=[], attempts=0)
    line = next(l for l in nudge.splitlines() if "`kanban_submit_for_review" in l)
    assert "opening a PR" not in line
