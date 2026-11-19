"""``recent_success`` respawn guard vs a deliberate move back to ``ready``.

A completed run holds a ``ready`` card for ``_RESPAWN_GUARD_SUCCESS_WINDOW``
unless a requeue event follows it. An operator rework bounce (``promote`` from
``blocked`` plus ``assign`` to another profile, a reviewer
``request_changes``, a review reopen, a ``status_changed`` move to ``ready``)
is such a requeue and must spawn on the next tick; a card nobody moved stays
held.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _completed_then(conn, status: str, assignee: str = "dev") -> str:
    """A card whose run completed seconds ago, now parked in ``status``
    (the reviewer's block / the review lane) without touching any event."""
    tid = kb.create_task(conn, title="rework bounce", assignee=assignee)
    kb.claim_task(conn, tid)
    assert kb.complete_task(conn, tid, summary="first pass") is True
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    return tid


def _event(conn, tid: str, kind: str, payload: dict) -> None:
    with kb.write_txn(conn):
        kb._append_event(conn, tid, kind, payload)


def test_promote_plus_assign_rework_bounce_spawns_next_tick(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    spawned: list[str] = []
    with kbc.connect() as conn:
        tid = _completed_then(conn, "blocked")
        ok, why = kb.promote_task(conn, tid, actor="default")
        assert ok, why
        assert kb.assign_task(conn, tid, "rework") is True

        assert kbd.check_respawn_guard(conn, tid) is None
        res = kbd.dispatch_once(conn, spawn_fn=lambda task, ws: spawned.append(task.id))
    assert tid in [s[0] for s in res.spawned]
    assert tid not in dict(res.respawn_guarded)


def test_manual_promote_alone_is_a_requeue(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _completed_then(conn, "blocked")
        assert kb.promote_task(conn, tid, actor="default")[0]
        assert kbd.check_respawn_guard(conn, tid) is None


def test_assign_to_a_different_profile_is_a_requeue(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _completed_then(conn, "ready")
        assert kb.assign_task(conn, tid, "rework") is True
        assert kbd.check_respawn_guard(conn, tid) is None


def test_unmoved_or_noop_reassigned_card_stays_held(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _completed_then(conn, "ready")
        assert kbd.check_respawn_guard(conn, tid) == "recent_success"
        assert kb.assign_task(conn, tid, "dev") is True  # same profile: not a move
        assert kbd.check_respawn_guard(conn, tid) == "recent_success"
        _event(conn, tid, "assigned", {"assignee": "dev", "from": "dev",
                                       "source": "kanban.default_assignee"})
        assert kbd.check_respawn_guard(conn, tid) == "recent_success"


def test_status_changed_to_ready_is_a_requeue_but_to_review_is_not(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        held = _completed_then(conn, "ready")
        _event(conn, held, "status_changed", {"from": "running", "to": "review"})
        assert kbd.check_respawn_guard(conn, held) == "recent_success"

        moved = _completed_then(conn, "ready")
        _event(conn, moved, "status_changed",
               {"from": "running", "to": "ready", "by": "request_changes"})
        assert kbd.check_respawn_guard(conn, moved) is None


@pytest.mark.parametrize("kind", ["changes_requested", "review_reopened"])
def test_review_bounce_events_are_requeues(kanban_home: Path, kind: str) -> None:
    with kbc.connect() as conn:
        tid = _completed_then(conn, "ready")
        _event(conn, tid, kind, {"status": "ready", "implementer": "dev"})
        assert kbd.check_respawn_guard(conn, tid) is None
