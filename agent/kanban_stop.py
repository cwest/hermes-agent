"""Turn-end guard for kanban workers.

Kanban workers must end their run with a lane-exiting board tool — one that
moves the card out of ``running`` (see ``KANBAN_LANE_EXIT_TOOLS``). Models
(especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
lane-exiting board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional


# Every kanban tool that moves the worker's card out of ``running`` and so
# legitimately ends the run. Each registered ``kanban_*`` tool must appear in
# exactly one of these two sets; tests/agent/test_kanban_stop.py enforces the
# partition against the tool registry, so a new lane-exit tool cannot be left
# out and resurrect the false "task is still running" nudge.
KANBAN_LANE_EXIT_TOOLS = frozenset({
    "kanban_complete",           # running -> done
    "kanban_block",              # running -> blocked
    "kanban_submit_for_review",  # running -> review (author handoff)
    "kanban_request_changes",    # review -> ready (reviewer bounce)
})

KANBAN_NON_EXIT_TOOLS = frozenset({
    "kanban_show",
    "kanban_list",
    "kanban_heartbeat",
    "kanban_comment",
    "kanban_attach",
    "kanban_attach_url",
    "kanban_attachments",
    "kanban_create",
    "kanban_link",
    "kanban_unblock",
    "kanban_reassign_origin",
})

_DEFAULT_MAX_ATTEMPTS = 2


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a lane-exiting kanban tool."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if _tool_call_name(tc) in KANBAN_LANE_EXIT_TOOLS:
                    return True
        elif role == "tool":
            name = str(msg.get("name") or "")
            if name in KANBAN_LANE_EXIT_TOOLS:
                return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a lane-exit tool.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    already moved the card out of ``running``, or nudge budget exhausted).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    if session_called_kanban_terminal(messages):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "that moves the card out of `running` causes a protocol violation.\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. End with the ONE board tool your task's workflow calls for:\n"
        "   - `kanban_submit_for_review(summary=...)` to hand reviewable work "
        "to the review lane when nothing else moves the card for you;\n"
        "   - `kanban_request_changes(summary=...)` if you are the reviewer "
        "sending the card back to its author;\n"
        "   - `kanban_complete(summary=..., artifacts=[...])` only if the work "
        "is finished outright with nothing left to review or merge;\n"
        "   - `kanban_block(reason=...)` if you genuinely need a human.\n"
        "   If your instructions say the run ends after a handoff comment "
        "(e.g. a draft PR whose card is moved for you), post that comment "
        "and stop — do not mark unreviewed work done.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "KANBAN_LANE_EXIT_TOOLS",
    "KANBAN_NON_EXIT_TOOLS",
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
