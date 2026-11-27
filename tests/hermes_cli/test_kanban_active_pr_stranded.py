"""A ``ready`` card stranded behind the ``active_pr`` guard is routed, not looped.

A worker interrupted mid-run (exit 130 from a gateway reload) leaves its card in
``ready`` with an open PR. The guard refuses to re-spawn the implementer so it does not
open a duplicate PR, and nothing else moves the card: it logged one ``respawn_guarded``
event per tick until someone moved it by hand. With no live worker the dispatcher now
hands the card to the reviewer its owner map names, and writes the guard event once per
PR head SHA instead of once per tick.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_pr_state as kps

PR = "https://github.com/o/r/pull/181"
OWNER_MAP = (
    "[audit] actor=triage stage=submit ts=2026-11-22T02:26:49Z\n"
    "notes: state_owners={ready: implementer, review: reviewer, blocked-acceptance: maintainer} "
    "kind=code kind_source=explicit"
)


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as db:
        yield db


@pytest.fixture
def pr(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Live PR state and head SHA, editable per test."""
    live = {"state": "open", "head": "1c4bce7b2023d8993e8bb75047eb207ec9a2ea0a"}
    monkeypatch.setattr(kps, "resolve_pr_state", lambda _url: live["state"])
    monkeypatch.setattr(kps, "resolve_pr_head", lambda _url: live["head"])
    return live


def _stranded(conn, *, owner_map: bool = True) -> str:
    """A code card whose interrupted run opened ``PR`` and then exited 130."""
    tid = kb.create_task(conn, title="per-task scratch", assignee="implementer")
    if owner_map:
        kb.add_comment(conn, tid, "triage", OWNER_MAP)
    kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, "implementer", f"progress: committed + draft PR opened\n- PR: {PR} (draft)")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET ended_at = ?, outcome = 'crashed', status = 'crashed', error = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (int(time.time()), "pid 79452 exited with code 130 [owner admission settled interrupted]", tid),
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, worker_started_at = NULL, current_run_id = NULL WHERE id = ?",
            (tid,),
        )
    return tid


def _keep_worker_alive(conn, tid: str) -> None:
    """The interrupted run's worker process is still running (this test process)."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE task_id = ?",
            (os.getpid(), kbd._process_fingerprint(os.getpid()), tid),
        )


def _tick(conn) -> list[str]:
    spawned: list[str] = []
    kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 0)
    return spawned


def _guard_events(conn, tid: str) -> list:
    return conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'respawn_guarded'", (tid,)
    ).fetchall()


def test_stranded_card_moves_to_the_owner_map_reviewer_on_the_first_guard_hit(conn, pr) -> None:
    tid = _stranded(conn)

    assert tid not in _tick(conn)

    task = kb.get_task(conn, tid)
    assert (task.status, task.assignee) == ("review", "reviewer")
    review = kb._json_dict(kb._latest_event(conn, tid, "review_requested")["payload"])
    assert (review["implementer"], review["reviewer"]) == ("implementer", "reviewer")
    audit = kb.list_comments(conn, tid)[-1].body
    assert PR in audit
    assert pr["head"] in audit
    assert "code 130" in audit


def test_a_live_worker_keeps_the_card_where_it_is(conn, pr) -> None:
    tid = _stranded(conn)
    _keep_worker_alive(conn, tid)

    assert tid not in _tick(conn)

    task = kb.get_task(conn, tid)
    assert (task.status, task.assignee) == ("ready", "implementer")
    assert kb._latest_event(conn, tid, "review_requested") is None


def test_no_resolvable_reviewer_keeps_the_card_where_it_is(conn, pr) -> None:
    tid = _stranded(conn, owner_map=False)

    _tick(conn)

    assert (kb.get_task(conn, tid).status, kb.get_task(conn, tid).assignee) == ("ready", "implementer")


def test_a_pr_link_no_run_ever_worked_is_not_routed(conn, pr) -> None:
    """The assignee's own PR link on a card no run ever worked (posted by hand) holds the
    card, but there is no interrupted implementer run behind it: a reviewer must not get
    work nobody did."""
    tid = kb.create_task(conn, title="address review", assignee="implementer")
    kb.add_comment(conn, tid, "triage", OWNER_MAP)
    kb.add_comment(conn, tid, "implementer", f"Working on {PR}")

    _tick(conn)

    assert (kb.get_task(conn, tid).status, kb.get_task(conn, tid).assignee) == ("ready", "implementer")
    assert len(_guard_events(conn, tid)) == 1


FOREIGN_PR = "https://github.com/o/kb/pull/770"


def test_a_never_run_card_dispatches_past_a_pr_link_another_profile_posted(conn, pr) -> None:
    """A cross-reference from another card's worker is not this card's PR: the assignee
    never opened it, so spawning the assignee cannot duplicate it."""
    tid = kb.create_task(conn, title="curate the node", assignee="implementer")
    kb.add_comment(conn, tid, "triage", OWNER_MAP)
    kb.add_comment(conn, tid, "researcher", f"finding from a sibling card: see {FOREIGN_PR}")

    assert kbd.check_respawn_guard(conn, tid) is None
    assert _tick(conn) == [tid]
    assert _guard_events(conn, tid) == []


def _foreign_link_after_the_pr(conn, tid: str) -> None:
    """Another profile cites a different open PR, strictly newer than the card's own."""
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'researcher', ?, ?)",
            (tid, f"related: {FOREIGN_PR}", int(time.time()) + 5),
        )


def test_a_foreign_link_does_not_mask_the_assignees_own_pr(conn, pr) -> None:
    tid = _stranded(conn)
    _keep_worker_alive(conn, tid)
    _foreign_link_after_the_pr(conn, tid)

    assert tid not in _tick(conn)

    events = _guard_events(conn, tid)
    assert kb._json_dict(events[-1]["payload"])["pr"] == PR


def test_a_stranded_card_with_a_newer_foreign_link_still_moves_to_review(conn, pr) -> None:
    tid = _stranded(conn)
    _foreign_link_after_the_pr(conn, tid)

    assert tid not in _tick(conn)

    task = kb.get_task(conn, tid)
    assert task is not None
    assert (task.status, task.assignee) == ("review", "reviewer")
    audit = [c.body for c in kb.list_comments(conn, tid) if c.author == "dispatcher"][-1]
    assert PR in audit
    assert FOREIGN_PR not in audit


def test_a_webhook_echo_of_the_cards_own_pr_after_changes_requested_dispatches(conn, pr) -> None:
    """A head-push notice from a webhook carries the card's own PR URL, but no implementer run
    wrote it: it must not re-arm the guard that ``changes_requested`` just lifted."""
    tid = kb.create_task(conn, title="rework", assignee="implementer")
    kb.add_comment(conn, tid, "triage", OWNER_MAP)
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    kb.add_comment(conn, tid, "implementer", f"draft PR opened: {PR}")
    with kb.write_txn(conn):
        conn.execute("UPDATE task_comments SET created_at = created_at - 120 WHERE task_id = ?", (tid,))
    assert kb.request_review(conn, tid, summary="PR ready", reviewer="reviewer",
                             expected_run_id=claimed.current_run_id)
    rclaim = kb.claim_review_task(conn, tid)
    assert rclaim is not None
    assert kb.request_changes(conn, tid, reason="fix it",
                              expected_run_id=rclaim.current_run_id) == (True, "implementer")
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (tid, "webhook:github-prs", f"head pushed on {PR}", int(time.time()) + 5),
        )

    assert kbd.check_respawn_guard(conn, tid) is None
    assert _tick(conn) == [tid]


def test_the_guard_event_is_written_once_per_head_sha(conn, pr) -> None:
    tid = _stranded(conn)
    _keep_worker_alive(conn, tid)

    for _ in range(3):
        _tick(conn)
    events = _guard_events(conn, tid)
    assert len(events) == 1
    assert kb._json_dict(events[0]["payload"]) == {"reason": "active_pr", "pr": PR, "head_sha": pr["head"]}

    pr["head"] = "2d5ecf8c"
    for _ in range(2):
        _tick(conn)
    assert len(_guard_events(conn, tid)) == 2
