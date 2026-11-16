"""At most one ``dir`` card runs on a given workspace root at a time.

``resolve_workspace`` returns a ``dir`` card's path as-is, so two such cards on
one path run in place on the same files and one worker's full-file write can
silently erase the other's edits. Scratch cards get a per-task directory and
worktree cards a per-task checkout, so only ``dir`` cards are serialized. A
deferred card stays ``ready`` and is claimed once the occupant leaves
``running``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "unknown")
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda _name: True))
    kb.init_db()
    return home


def _dir_card(conn, root: Path, title: str) -> str:
    return kb.create_task(
        conn, title=title, assignee="worker", workspace_kind="dir", workspace_path=str(root),
    )


def _tick(conn):
    spawns: list[str] = []

    def spawn(task, workspace, board=None):
        spawns.append(task.id)

    res = kbd.dispatch_once(conn, spawn_fn=spawn)
    return spawns, res


def test_two_ready_cards_on_one_root_spawn_one_per_tick(kanban_home, tmp_path):
    root = tmp_path / "shared"
    with kbc.connect() as conn:
        first = _dir_card(conn, root, "a")
        second = _dir_card(conn, root, "b")

        spawns, res = _tick(conn)

        assert spawns == [first]
        assert [tid for tid, _root in res.skipped_workspace_busy] == [second]
        assert kb.get_task(conn, second).status == "ready"


def test_a_running_card_holds_its_root(kanban_home, tmp_path):
    root = tmp_path / "shared"
    with kbc.connect() as conn:
        first = _dir_card(conn, root, "a")
        _tick(conn)
        second = _dir_card(conn, root / ".." / "shared", "b")

        spawns, _res = _tick(conn)

        assert spawns == []
        assert kb.get_task(conn, first).status == "running"
        assert kb.get_task(conn, second).status == "ready"


def test_the_deferred_card_runs_once_the_root_is_free(kanban_home, tmp_path):
    root = tmp_path / "shared"
    with kbc.connect() as conn:
        first = _dir_card(conn, root, "a")
        second = _dir_card(conn, root, "b")
        _tick(conn)
        assert kb.complete_task(conn, first, summary="done")

        spawns, _res = _tick(conn)

        assert spawns == [second]


def test_cards_on_different_roots_run_in_parallel(kanban_home, tmp_path):
    with kbc.connect() as conn:
        a = _dir_card(conn, tmp_path / "one", "a")
        b = _dir_card(conn, tmp_path / "two", "b")

        spawns, res = _tick(conn)

        assert sorted(spawns) == sorted([a, b])
        assert res.skipped_workspace_busy == []


def test_scratch_cards_are_never_serialized(kanban_home):
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="a", assignee="worker")
        b = kb.create_task(conn, title="b", assignee="worker")

        spawns, _res = _tick(conn)

        assert sorted(spawns) == sorted([a, b])
