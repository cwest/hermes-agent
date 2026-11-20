"""The ``active_pr`` respawn guard: a ready card whose recent comments carry a live GitHub PR
URL is held, so the worker that opened the PR does not open a duplicate.

A handoff after the newest PR comment (operator reassign, explicit unblock, reviewer
``changes_requested``, review reopen) names the profile that must now work on THAT PR — a
closer or the implementer finishing it, not a duplicate implementation (#111910). A
crash/reclaim is not a handoff. A PR that is no longer live (closed, merged, never existed)
cannot be duplicated, so it does not guard either.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Iterable
from typing import Optional

from hermes_cli import kanban_db as _kb

# Within this window a GitHub PR URL in a comment blocks re-spawn.
_PR_WINDOW = 86400  # 24 hours

_PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)

_HANDOFF_KINDS = "('assigned', 'unblocked', 'changes_requested', 'review_reopened')"
_REVIEW_BOUNCES = ("changes_requested", "review_reopened")


def active_pr_reason(conn: sqlite3.Connection, task_id: str, now: int) -> Optional[str]:
    """``"active_pr"`` while a live PR from a recent comment has no handoff after it, else None."""
    from hermes_cli.kanban_db_dispatch import _rows_since

    pr_comments = [(t, c["author"], _PR_URL_RE.findall(_kb._lossy_text(c["body"]) or ""))
                   for t, c in _rows_since(conn, "task_comments", task_id, now - _PR_WINDOW,
                                           cols=", author, body", unreadable=now)]  # duplicate-PR risk: fail closed
    pr_comments = [(t, author, urls) for t, author, urls in pr_comments if urls]
    if not pr_comments:
        return None
    newest = max(t for t, _, _ in pr_comments)
    if _handoff_follows(conn, task_id, newest, {a for t, a, _ in pr_comments if t == newest}):
        return None
    return "active_pr" if _any_live_pr(url for _, _, urls in pr_comments for url in urls) else None


def _handoff_follows(conn: sqlite3.Connection, task_id: str, newest: int, newest_authors: set) -> bool:
    """A handoff event strictly after the newest PR comment, or a review bounce in that same
    second whose reviewer wrote every PR comment of that second: the verdict comment and the
    bounce are one act, so they routinely share an epoch second. Any other same-second tie stays
    guarded (fail closed). Comment and event row ids are separate sequences, so the epoch second
    is the only clock both tables share."""
    from hermes_cli.kanban_db_dispatch import _rows_since
    from hermes_cli.kanban_db_dispatch_requeue import is_handoff_event

    for t, e in _rows_since(conn, "task_events", task_id, newest, f"AND kind IN {_HANDOFF_KINDS}",
                            cols=", kind, payload"):
        if t > newest and is_handoff_event(e["kind"], e["payload"]):
            return True
        if t == newest and e["kind"] in _REVIEW_BOUNCES:
            reviewer = _bounce_reviewer(conn, task_id, e["kind"], e["payload"])
            if reviewer and {_kb._canonical_assignee(a) for a in newest_authors} == {reviewer}:
                return True
    return False


def _bounce_reviewer(conn: sqlite3.Connection, task_id: str, kind: str, payload: Optional[str]) -> Optional[str]:
    """Reviewer who issued a review bounce: ``changes_requested`` records it; a
    ``review_reopened`` inherits it from the ``review_requested`` it reopens."""
    data = _kb._json_dict(payload)
    if kind == "review_reopened":
        data = _kb._json_dict(_kb._row_get(_kb._latest_event(conn, task_id, "review_requested"), "payload"))
    return _kb._canonical_assignee(_kb._nonblank_str(data.get("reviewer")))


def _any_live_pr(urls: Iterable[str]) -> bool:
    """True once any URL resolves to a PR that may still be worked (open, or
    unresolvable). Each distinct URL is looked up at most once."""
    from hermes_cli import kanban_pr_state as _kps
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        if _kps.resolve_pr_state(url) not in _kps.INACTIVE_PR_STATES:
            return True
    return False
