"""The ``active_pr`` respawn guard: a ready card whose recent comments carry a live GitHub PR
URL is held, so the worker that opened the PR does not open a duplicate.

Only a link the card's own worker could have opened counts: a comment authored by a profile the
card was ever assigned to or that worked one of its runs. Any other author's link (another card's
worker citing its PR, a triager filing "address the review on PR X", a ``webhook:*`` head-push
echo of the card's own PR after a review bounce) is a cross-reference, not evidence of an
interrupted implementer, and spawning the assignee cannot duplicate a PR off it. Without that
filter a ready card sat held behind such a comment until the PR happened to close.

A handoff after the newest PR comment (operator reassign, reviewer ``changes_requested``,
review reopen, or a human release of a parked card: unblock, or promote or drag out of
``blocked``/``triage``) names the profile that must now work on THAT PR — a closer or the
implementer finishing it, not a duplicate implementation (#111910). A crash/reclaim, an
automatic promotion, a same-profile reassign and a triage rewrite (``specified``/
``decomposed``, which the unattended auto-decomposer also writes) are not handoffs. A PR that
is no longer live (closed, merged, never existed) cannot be duplicated, so it does not guard
either.

The guard keeps the implementer from re-spawning, so on its own it never moves the card. A
worker interrupted after opening its PR (exit 130 from a gateway reload) left the card in
``ready`` with nobody to finish it, and one ``respawn_guarded`` event per tick. :func:`on_guard`
hands such a card to its reviewer instead, and writes the guard event once per PR head SHA.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Iterable
from typing import Optional

from hermes_cli import kanban_db as _kb

# Within this window a GitHub PR URL in a comment blocks re-spawn.
_PR_WINDOW = 86400  # 24 hours

_PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)

_HANDOFF_KINDS = ("('assigned', 'unblocked', 'changes_requested', 'review_reopened', "
                  "'promoted_manual', 'status')")
_REVIEW_BOUNCES = ("changes_requested", "review_reopened")

# Run outcomes that end a worker without its say: the card is back in ``ready`` only because the
# run was cut short, not because the worker handed anything off.
_INTERRUPTED_OUTCOMES = frozenset({"crashed", "timed_out", "reclaimed", "stale"})

_OWNER_MAP_RE = re.compile(r"state_owners=\{([^}]*)\}")


def active_pr_reason(conn: sqlite3.Connection, task_id: str, now: int) -> Optional[str]:
    """``"active_pr"`` while a live PR from a recent comment has no handoff after it, else None."""
    return "active_pr" if _held_by(conn, task_id, now) else None


def _held_by(conn: sqlite3.Connection, task_id: str, now: int) -> Optional[tuple[str, int]]:
    """``(newest live PR url, newest PR comment time)`` while the guard holds the card."""
    from hermes_cli.kanban_db_dispatch import _rows_since

    pr_comments = [(t, c["author"], _PR_URL_RE.findall(_kb._lossy_text(c["body"]) or ""))
                   for t, c in _rows_since(conn, "task_comments", task_id, now - _PR_WINDOW,
                                           cols=", author, body", unreadable=now)]  # duplicate-PR risk: fail closed
    pr_comments = [(t, author, urls) for t, author, urls in pr_comments if urls]
    if pr_comments:
        owners = _card_owners(conn, task_id)
        pr_comments = [c for c in pr_comments if _profile_key(_kb._lossy_text(c[1])) in owners]
    if not pr_comments:
        return None
    newest = max(t for t, _, _ in pr_comments)
    if _handoff_follows(conn, task_id, newest, {a for t, a, _ in pr_comments if t == newest}):
        return None
    ordered = sorted(pr_comments, key=lambda c: c[0], reverse=True)
    live = _first_live_pr(url for _, _, urls in ordered for url in reversed(urls))
    return (live, newest) if live else None


def _card_owners(conn: sqlite3.Connection, task_id: str) -> set[str]:
    """Profiles whose own PR a respawn could duplicate: every profile the card was ever assigned
    to (an unassign or a reassign away can still route it back) and every profile that worked one
    of its runs. A link any other author posts is a cross-reference to someone else's PR."""
    names = [r[0] for r in conn.execute(
        "SELECT assignee FROM tasks WHERE id = ? UNION SELECT profile FROM task_runs WHERE task_id = ?",
        (task_id, task_id),
    )]
    for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind IN ('created', 'assigned')", (task_id,),
    ):
        payload = _kb._json_dict(row[0])
        names += [payload.get("assignee"), payload.get("from")]
    return {key for key in map(_profile_key, names) if key}


def _profile_key(name: object) -> Optional[str]:
    return _kb._canonical_assignee(name) if isinstance(name, str) and name.strip() else None


def on_guard(conn: sqlite3.Connection, task_id: str, reason: str) -> bool:
    """Record a respawn-guard hit on a ``ready`` card. True when the card was routed to review
    instead: ``active_pr`` on a card whose implementer was interrupted with no live worker left."""
    held = _held_by(conn, task_id, int(time.time())) if reason == "active_pr" else None
    payload: dict = {"reason": reason}
    if held:
        from hermes_cli import kanban_pr_state as _kps
        payload.update(pr=held[0], head_sha=_kps.resolve_pr_head(held[0]))
        if _route_stranded(conn, task_id, held[0], held[1], payload["head_sha"]):
            return True
    with _kb.write_txn(conn):
        last = _kb._latest_event(conn, task_id, "respawn_guarded")
        if not held or _kb._json_dict(_kb._row_get(last, "payload")) != payload:
            _kb._append_event(conn, task_id, "respawn_guarded", payload)
    return False


def _route_stranded(conn: sqlite3.Connection, task_id: str, pr_url: str, pr_at: int,
                    head_sha: Optional[str]) -> bool:
    """Move a ``ready`` card stranded behind its own open PR to its reviewer.

    Stranded = the latest run was interrupted after it started (the PR comment came from that
    run's window), no worker process of any run is still alive, and a reviewer other than the
    assignee is on record. The assignee's own PR link on a card no run ever worked holds the card
    but is not routed: a reviewer must not get work nobody did."""
    run = conn.execute(
        "SELECT id, outcome, error, started_at FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if run is None or run["outcome"] not in _INTERRUPTED_OUTCOMES:
        return False
    started = _kb._to_epoch(run["started_at"])
    if started is None or pr_at < started or _has_live_worker(conn, task_id):
        return False
    task = _kb.get_task(conn, task_id)
    reviewer = _reviewer_for(conn, task_id)
    if task is None or not reviewer or reviewer == _kb._canonical_assignee(task.assignee):
        return False
    error = _kb._first_line(_kb._lossy_text(run["error"]), 200) or "none recorded"
    head = head_sha or "unknown"
    summary = (f"Implementer run {run['id']} was interrupted ({run['outcome']}) after opening {pr_url} "
               f"(head {head}); the active_pr guard holds the implementer, so the card moves to review.")
    if not _kb.request_review(conn, task_id, summary=summary, reviewer=reviewer):
        return False
    _kb.add_comment(conn, task_id, "dispatcher", (
        "[audit] actor=dispatcher stage=review\n"
        f"notes: reason=active_pr_stranded pr={pr_url} head_sha={head} interrupted_run={run['id']} "
        f"outcome={run['outcome']} reviewer={reviewer} error={error}"))
    return True


def _has_live_worker(conn: sqlite3.Connection, task_id: str) -> bool:
    """Any worker process this card ever spawned is still running (PID + start fingerprint)."""
    from hermes_cli.kanban_db_dispatch import _worker_alive

    rows = conn.execute(
        "SELECT worker_pid, worker_started_at FROM task_runs WHERE task_id = ? AND worker_pid IS NOT NULL "
        "UNION SELECT worker_pid, worker_started_at FROM tasks WHERE id = ? AND worker_pid IS NOT NULL",
        (task_id, task_id),
    ).fetchall()
    return any(_worker_alive(r["worker_pid"], r["worker_started_at"]) for r in rows)


def _reviewer_for(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The reviewer of a prior review bounce, else the ``review`` lane of the card's owner map."""
    prior = _kb._prior_reviewer(conn, task_id)
    if prior:
        return _kb._canonical_assignee(prior)
    return _kb._canonical_assignee(_owner_map(conn, task_id).get("review"))


def _owner_map(conn: sqlite3.Connection, task_id: str) -> dict[str, str]:
    """``state_owners={lane: owner, ...}`` from the card's ``[audit]`` comments: the earliest stamp
    not marked ``kind_source=defaulted``, else the earliest defaulted one, taken whole."""
    fallback: dict[str, str] = {}
    for comment in _kb.list_comments(conn, task_id):
        body = _kb._lossy_text(comment.body) or ""
        match = _OWNER_MAP_RE.search(body) if body.lstrip().startswith("[audit]") else None
        if not match:
            continue
        lanes = {}
        for pair in match.group(1).split(","):
            lane, sep, owner = (_dequote(part) for part in pair.partition(":"))
            if sep and lane and owner:
                lanes[lane] = owner
        if lanes and "kind_source=defaulted" not in body:
            return lanes
        fallback = fallback or lanes
    return fallback


def _dequote(token: str) -> str:
    """Strip one balanced pair of outer quotes, so ``{'review': 'x'}`` reads as ``{review: x}``."""
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1].strip()
    return token


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


def _first_live_pr(urls: Iterable[str]) -> Optional[str]:
    """The first URL that resolves to a PR that may still be worked (open, or
    unresolvable). Each distinct URL is looked up at most once."""
    from hermes_cli import kanban_pr_state as _kps
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        if _kps.resolve_pr_state(url) not in _kps.INACTIVE_PR_STATES:
            return url
    return None
