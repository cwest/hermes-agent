"""``active_pr`` reflects the PR's live state and an explicit unblock.

The guard holds a ``ready`` card whose comments carry a GitHub PR URL so the
implementer does not open a duplicate PR. A stored URL never changes, though:
once its PR is closed, merged or deleted (or the URL never existed) the guard
held the card for the full 24h window with no worker. A deliberate ``unblock``
after the PR comment is the operator sending the card back to work on that PR,
so it lifts the guard the way the other handoff events do (upstream PR #46204).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_pr_state as kps

OPEN_PR = "https://github.com/o/r/pull/1"
DONE_PR = "https://github.com/o/r/pull/2"


@pytest.fixture
def conn(tmp_path: Path):
    db = kbc.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def states(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Live PR states by URL; records every lookup in ``states['calls']``."""
    table: dict = {"calls": []}

    def resolve(url: str) -> str:
        table["calls"].append(url)
        return table.get(url, "unknown")

    monkeypatch.setattr(kps, "resolve_pr_state", resolve)
    return table


def _card(conn, title: str = "card") -> str:
    return kb.create_task(conn, title=title, assignee="dev")


def _comment(conn, tid: str, body: str, at: int) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'dev', ?, ?)",
            (tid, body, at),
        )


def _event(conn, tid: str, kind: str, at: int) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, NULL, ?)",
            (tid, kind, at),
        )


@pytest.mark.parametrize("state", ["closed", "merged", "not_found"])
def test_a_pr_that_is_no_longer_live_does_not_hold_the_card(conn, states, state) -> None:
    tid = _card(conn)
    _comment(conn, tid, f"Opened {DONE_PR}", int(time.time()) - 60)
    states[DONE_PR] = state
    assert kbd.check_respawn_guard(conn, tid) is None


@pytest.mark.parametrize("state", ["open", "unknown"])
def test_an_open_or_unresolvable_pr_still_holds_the_card(conn, states, state) -> None:
    tid = _card(conn)
    _comment(conn, tid, f"Opened {OPEN_PR}", int(time.time()) - 60)
    states[OPEN_PR] = state
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_any_live_pr_among_dead_ones_holds_the_card(conn, states) -> None:
    now = int(time.time())
    tid = _card(conn)
    _comment(conn, tid, f"Opened {OPEN_PR}", now - 120)
    _comment(conn, tid, f"Superseded by {DONE_PR}", now - 60)
    states.update({OPEN_PR: "open", DONE_PR: "merged"})
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_each_pr_url_is_resolved_once_per_check(conn, states) -> None:
    now = int(time.time())
    tid = _card(conn)
    _comment(conn, tid, f"Opened {DONE_PR}", now - 120)
    _comment(conn, tid, f"Pushed to {DONE_PR} and {DONE_PR}", now - 60)
    states[DONE_PR] = "closed"
    assert kbd.check_respawn_guard(conn, tid) is None
    assert states["calls"] == [DONE_PR]


def test_an_unblock_after_the_pr_comment_lifts_the_guard(conn, states) -> None:
    now = int(time.time())
    tid = _card(conn)
    _comment(conn, tid, f"Opened {OPEN_PR}", now - 60)
    _event(conn, tid, "unblocked", now)
    states[OPEN_PR] = "open"
    assert kbd.check_respawn_guard(conn, tid) is None


def test_a_pr_comment_after_the_unblock_holds_again(conn, states) -> None:
    now = int(time.time())
    tid = _card(conn)
    _event(conn, tid, "unblocked", now - 60)
    _comment(conn, tid, f"Opened {OPEN_PR}", now)
    states[OPEN_PR] = "open"
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_a_same_second_unblock_stays_guarded(conn, states) -> None:
    now = int(time.time())
    tid = _card(conn)
    _comment(conn, tid, f"Opened {OPEN_PR}", now)
    _event(conn, tid, "unblocked", now)
    states[OPEN_PR] = "open"
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_a_real_block_unblock_cycle_releases_the_card(conn, states) -> None:
    tid = _card(conn)
    _comment(conn, tid, f"Opened {OPEN_PR}", int(time.time()) - 60)
    states[OPEN_PR] = "open"
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"
    assert kb.block_task(conn, tid, reason="needs a decision")
    assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "ready"
    assert kbd.check_respawn_guard(conn, tid) is None


# --- the resolver itself -----------------------------------------------------


def _fake_run(monkeypatch, *, returncode=0, stdout="", stderr="", raises=None):
    seen = []

    def run(cmd, **kwargs):
        seen.append(cmd)
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr(kps.subprocess, "run", run)
    return seen


@pytest.mark.real_pr_state
@pytest.mark.parametrize("gh_state,expected", [("OPEN", "open"), ("CLOSED", "closed"), ("MERGED", "merged")])
def test_resolver_maps_gh_states(monkeypatch, gh_state, expected) -> None:
    seen = _fake_run(monkeypatch, stdout=f'{{"state": "{gh_state}"}}')
    assert kps.resolve_pr_state(OPEN_PR) == expected
    assert seen == [["gh", "pr", "view", OPEN_PR, "--json", "state"]]


@pytest.mark.real_pr_state
def test_resolver_reports_a_pr_github_does_not_have_as_not_found(monkeypatch) -> None:
    _fake_run(monkeypatch, returncode=1,
              stderr="GraphQL: Could not resolve to a PullRequest with the number of 1. (repository.pullRequest)")
    assert kps.resolve_pr_state(OPEN_PR) == "not_found"


@pytest.mark.real_pr_state
@pytest.mark.parametrize("kwargs", [
    {"returncode": 1, "stderr": "error connecting to api.github.com"},
    {"returncode": 4, "stderr": "To get started with GitHub CLI, please run:  gh auth login"},
    {"stdout": "not json"},
    {"stdout": '{"state": 7}'},
    {"stdout": '{"state": "DRAFTY"}'},
    {"stdout": "[]"},
    {"raises": FileNotFoundError("gh")},
    {"raises": subprocess.TimeoutExpired(["gh"], 20)},
])
def test_resolver_fails_open_to_unknown(monkeypatch, kwargs) -> None:
    _fake_run(monkeypatch, **kwargs)
    assert kps.resolve_pr_state(OPEN_PR) == "unknown"


def test_tests_never_reach_gh_without_opting_in() -> None:
    """The autouse pin keeps every other test on the old always-guard answer."""
    assert kps.resolve_pr_state(OPEN_PR) == "unknown"
