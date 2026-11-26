"""Every operator release of a human-held card lifts ``active_pr``; crash and auto-promotion do not.

A worker that parks at a decision gate (``blocked`` ``needs_input``, or ``triage`` after the
unblock-loop breaker) usually has its own draft PR open. The human's release is the handoff that
sends it back to work on that PR. Only ``unblock`` was recognised, so a release by ``promote`` or
by a dashboard drag out of triage left the card in ``ready`` with no worker until the 24h window
ran out. A second gate park after a ``promote`` release also counted as an unblock loop and went
to triage.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_pr_state as kps
from hermes_cli.kanban_db_graph import decompose_triage_task

PR = "https://github.com/o/r/pull/268"


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kps, "resolve_pr_state", lambda _url: "open")
    monkeypatch.setattr(kps, "resolve_pr_head", lambda _url: "aa3946c05a8cf56b0a7f83e7ee9ff6bd53a89f62")
    kb.init_db()
    with kbc.connect() as db:
        yield db


def _dashboard():
    """The kanban dashboard plugin's API module, loaded from its file."""
    path = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_dashboard_gate_release_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tick(conn) -> list[str]:
    spawned: list[str] = []
    kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 0)
    return spawned


def _parked_at_gate(conn, *, kind: str = "needs_input") -> str:
    """A card whose run opened ``PR`` and then parked at a decision gate."""
    tid = kb.create_task(conn, title="motion films", assignee="author")
    assert kb.claim_task(conn, tid) is not None
    kb.add_comment(conn, tid, "author", f"Storyboards ready on draft PR {PR}")
    with kb.write_txn(conn):
        conn.execute("UPDATE task_comments SET created_at = created_at - 120 WHERE task_id = ?", (tid,))
    assert kb.block_task(conn, tid, reason="storyboard gate: needs sign-off", kind=kind)
    return tid


def _in_triage(conn) -> str:
    tid = _parked_at_gate(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
    return tid


def test_promote_after_a_needs_input_park_spawns_the_author(conn) -> None:
    tid = _parked_at_gate(conn)
    ok, err = kb.promote_task(conn, tid, actor="operator")
    assert ok, err
    assert _tick(conn) == [tid]


@pytest.mark.parametrize("kind", ["capability", "transient", None])
def test_promote_releases_a_park_of_any_kind(conn, kind) -> None:
    tid = _parked_at_gate(conn, kind=kind)
    assert kb.promote_task(conn, tid, actor="operator")[0]
    assert kbd.check_respawn_guard(conn, tid) is None


def _release_by_promote(conn, tid: str) -> None:
    assert kb.promote_task(conn, tid, actor="operator")[0]


def _release_by_unblock(conn, tid: str) -> None:
    assert kb.unblock_task(conn, tid)


def _release_by_dashboard_drag(conn, tid: str) -> None:
    assert _dashboard()._drag_to(conn, tid, "ready")


def _release_by_specify(conn, tid: str) -> None:
    assert kb.specify_triage_task(conn, tid, body="Gate 1 decided: B approved, C dropped.", author="operator")


def _release_by_decompose(conn, tid: str) -> None:
    assert decompose_triage_task(conn, tid, root_assignee="author", children=[{"title": "child"}])


@pytest.mark.parametrize("release", [_release_by_promote, _release_by_unblock])
def test_every_release_from_blocked_lifts_the_guard(conn, release) -> None:
    tid = _parked_at_gate(conn)
    release(conn, tid)
    assert kbd.check_respawn_guard(conn, tid) is None


@pytest.mark.parametrize("release", [_release_by_promote, _release_by_dashboard_drag])
def test_every_release_from_triage_lifts_the_guard(conn, release) -> None:
    tid = _in_triage(conn)
    release(conn, tid)
    assert kbd.check_respawn_guard(conn, tid) is None


@pytest.mark.parametrize("rewrite", [_release_by_specify, _release_by_decompose])
def test_a_triage_rewrite_stays_guarded(conn, rewrite) -> None:
    """The unattended auto-decomposer writes the same events, so they prove no human decided."""
    tid = _in_triage(conn)
    rewrite(conn, tid)
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_a_pr_comment_after_the_release_holds_again(conn) -> None:
    tid = _parked_at_gate(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET created_at = created_at - 60 WHERE task_id = ?", (tid,))
    _release_by_promote(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET created_at = created_at - 30 WHERE task_id = ? "
                     "AND kind = 'promoted_manual'", (tid,))
    kb.add_comment(conn, tid, "author", f"Pushed rework to {PR}")
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_a_promotion_recorded_without_its_source_lane_stays_guarded(conn) -> None:
    """Events written before ``from`` was recorded cannot prove a lane exit: fail closed."""
    tid = kb.create_task(conn, title="legacy", assignee="author")
    kb.add_comment(conn, tid, "author", f"Opened {PR}")
    with kb.write_txn(conn):
        conn.execute("UPDATE task_comments SET created_at = created_at - 120 WHERE task_id = ?", (tid,))
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES "
                     "(?, 'promoted_manual', '{\"actor\": \"operator\", \"reason\": null}', ?)",
                     (tid, int(time.time())))
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_a_crash_after_the_pr_comment_stays_guarded(conn) -> None:
    tid = kb.create_task(conn, title="crash", assignee="author")
    assert kb.claim_task(conn, tid) is not None
    kb.add_comment(conn, tid, "author", f"Opened {PR}")
    with kb.write_txn(conn):
        conn.execute("UPDATE task_comments SET created_at = created_at - 120 WHERE task_id = ?", (tid,))
    assert kb.reclaim_task(conn, tid, reason="worker crashed")
    assert kb.get_task(conn, tid).status == "ready"
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_an_automatic_promotion_out_of_blocked_stays_guarded(conn) -> None:
    """``recompute_ready`` releasing a breaker park is not a human decision."""
    tid = kb.create_task(conn, title="breaker", assignee="author")
    kb.add_comment(conn, tid, "author", f"Opened {PR}")
    with kb.write_txn(conn):
        conn.execute("UPDATE task_comments SET created_at = created_at - 120 WHERE task_id = ?", (tid,))
        conn.execute("UPDATE tasks SET status = 'blocked', consecutive_failures = 0 WHERE id = ?", (tid,))
    assert kb.recompute_ready(conn) == 1
    assert kb.get_task(conn, tid).status == "ready"
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_a_same_profile_reassign_after_the_park_is_still_not_a_handoff(conn) -> None:
    tid = _parked_at_gate(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    assert kb.assign_task(conn, tid, "author")
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_a_dashboard_drag_from_running_to_ready_stays_guarded(conn) -> None:
    tid = kb.create_task(conn, title="drag", assignee="author")
    assert kb.claim_task(conn, tid) is not None
    kb.add_comment(conn, tid, "author", f"Opened {PR}")
    with kb.write_txn(conn):
        conn.execute("UPDATE task_comments SET created_at = created_at - 120 WHERE task_id = ?", (tid,))
    assert _dashboard()._set_status_direct(conn, tid, "ready")
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def _reach_ready_by_specify(conn, tid: str) -> None:
    assert kb.specify_triage_task(conn, tid, body="rewritten", author="operator")


def _reach_ready_by_recompute(conn, tid: str) -> None:
    kb.recompute_ready(conn)


@pytest.mark.parametrize(("start", "to", "reach_ready"), [
    ("blocked", "triage", _reach_ready_by_specify),
    ("triage", "triage", _reach_ready_by_specify),
    ("blocked", "todo", _reach_ready_by_recompute),
])
def test_a_drag_between_held_lanes_is_not_a_release(conn, start, to, reach_ready) -> None:
    """Only a move that lands the card in ``ready`` releases it; a later automatic
    promotion from the new lane proves no human decided it should resume."""
    tid = _in_triage(conn) if start == "triage" else _parked_at_gate(conn)
    assert _dashboard()._set_status_direct(conn, tid, to)
    reach_ready(conn, tid)
    assert kb.get_task(conn, tid).status == "ready"
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def _rerun(conn, tid: str) -> None:
    assert kb.claim_task(conn, tid) is not None


def test_a_second_gate_after_a_promote_release_is_not_an_unblock_loop(conn) -> None:
    tid = _parked_at_gate(conn)
    _release_by_promote(conn, tid)
    _rerun(conn, tid)
    assert kb.block_task(conn, tid, reason="storyboard gate 2", kind="needs_input")
    task = kb.get_task(conn, tid)
    assert (task.status, task.block_recurrences) == ("blocked", 1)


def test_an_unblock_release_still_counts_toward_the_loop_breaker(conn) -> None:
    tid = _parked_at_gate(conn)
    _release_by_unblock(conn, tid)
    _rerun(conn, tid)
    assert kb.block_task(conn, tid, reason="same cause again", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
