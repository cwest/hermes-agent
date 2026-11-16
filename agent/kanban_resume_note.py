"""The resume note a kanban worker leaves when it runs out of iteration budget.

Budget exhaustion records ``timed_out`` and releases the claim, so the next run
starts cold. The note tells it where this one stopped, in the ``[audit]``
comment shape the board already uses: the budget it ran out of, the branch, the
newest PR linked on the card, the last tool it called, and a next step. Only
facts the run can know are recorded; nothing is guessed.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from contextlib import suppress

_PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)


def last_tool_name(messages) -> str | None:
    """Name of the newest tool call in ``messages``."""
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list) or not calls or not isinstance(calls[-1], dict):
            continue
        function = calls[-1].get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if name:
            return name
    return None


def build_note(*, budget_used: int, budget_max: int, branch: str | None,
               pr_url: str | None, last_tool: str | None) -> str:
    notes = ["reason=budget_exhausted", f"budget_used={budget_used}", f"budget_max={budget_max}"]
    if branch:
        notes.append(f"branch={branch}")
    if pr_url:
        notes.append(f"artifact={pr_url}")
    if last_tool:
        notes.append(f"last_tool={last_tool}")
    if pr_url:
        next_step = ("resume the in-flight PR: check its state and CI, land the pending change, "
                     "then hand it off for review")
    elif last_tool:
        next_step = f"continue from the last tool ({last_tool}); the branch carries partial work"
    else:
        next_step = "re-read the card and resume the in-progress work"
    notes.append(f"next_step={next_step}")
    return "[audit] actor=worker stage=resume\nnotes: " + " ".join(notes)


def _newest_pr_url(conn: sqlite3.Connection, task_id: str) -> str | None:
    from hermes_cli import kanban_db as _kb
    for comment in reversed(_kb.list_comments(conn, task_id)):
        urls = _PR_URL_RE.findall(comment.body or "")
        if urls:
            return urls[-1]
    return None


def post_resume_note(task_id: str, *, budget_used: int, budget_max: int, messages,
                     logger: logging.Logger) -> None:
    """Best effort: a failed note is logged and never stops the terminal event."""
    try:
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban_db_connect as _kbc
        conn = _kbc.connect()
        try:
            note = build_note(
                budget_used=budget_used, budget_max=budget_max,
                branch=os.environ.get("HERMES_KANBAN_BRANCH") or None,
                pr_url=_newest_pr_url(conn, task_id), last_tool=last_tool_name(messages),
            )
            _kb.add_comment(conn, task_id, author="worker", body=note)
        finally:
            with suppress(Exception):
                conn.close()
    except Exception:  # health: allow BLE001 -- the timeout must be recorded even when the note cannot be
        logger.warning("Failed to post the resume note for task %s", task_id, exc_info=True)
