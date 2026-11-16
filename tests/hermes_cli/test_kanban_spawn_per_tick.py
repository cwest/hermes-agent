"""``kanban.max_spawn_per_tick`` bounds how many workers ONE dispatcher tick launches.

``max_spawn`` and ``max_in_progress`` are live concurrency caps: a tick after a
long stall (a wedged gateway, a dispatcher restart) still sees every slot free
and launches the whole ready queue at once. The per-tick bound spreads that
burst across ticks; ready and review spawns count together.
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


def _board_with(conn, *, ready: int, review: int) -> None:
    for i in range(ready):
        kb.create_task(conn, title=f"ready {i}", assignee="worker")
    for i in range(review):
        tid = kb.create_task(conn, title=f"review {i}", assignee="reviewer")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
    conn.commit()


def _tick(conn, spawns: list, **caps):
    def spawn(task, workspace, board=None):
        spawns.append(task.id)

    return kbd.dispatch_once(conn, spawn_fn=spawn, **caps)


def test_one_tick_launches_at_most_the_per_tick_bound(kanban_home):
    with kbc.connect() as conn:
        _board_with(conn, ready=5, review=1)
        spawns: list = []

        res = _tick(conn, spawns, max_spawn_per_tick=2)

        assert len(spawns) == 2
        assert len(res.spawned) == 2


def test_the_rest_of_the_queue_drains_on_later_ticks(kanban_home):
    with kbc.connect() as conn:
        _board_with(conn, ready=5, review=0)
        spawns: list = []
        for _ in range(3):
            _tick(conn, spawns, max_spawn_per_tick=2)
        assert len(spawns) == 5


def test_review_work_still_spawns_under_a_ready_backlog(kanban_home):
    with kbc.connect() as conn:
        _board_with(conn, ready=5, review=1)
        spawns: list = []

        _tick(conn, spawns, max_spawn_per_tick=2)

        lanes = [kb.get_task(conn, tid).assignee for tid in spawns]
        assert "reviewer" in lanes


def test_the_tighter_of_the_live_cap_and_the_per_tick_bound_wins(kanban_home):
    with kbc.connect() as conn:
        _board_with(conn, ready=5, review=0)
        spawns: list = []
        _tick(conn, spawns, max_spawn=1, max_spawn_per_tick=3)
        assert len(spawns) == 1


@pytest.mark.parametrize("bound", [None, 0, -1])
def test_unset_or_non_positive_bound_does_not_cap(kanban_home, bound):
    with kbc.connect() as conn:
        _board_with(conn, ready=4, review=0)
        spawns: list = []
        _tick(conn, spawns, max_spawn_per_tick=bound)
        assert len(spawns) == 4


def test_gateway_dispatcher_reads_the_bound_from_config():
    from gateway.kanban_watchers_dispatcher import _resolve_dispatcher_settings

    settings = _resolve_dispatcher_settings({"max_spawn_per_tick": 3}, kb)
    assert settings.max_spawn_per_tick == 3
    assert _resolve_dispatcher_settings({}, kb).max_spawn_per_tick is None


def test_standalone_daemon_reads_the_bound_from_config(kanban_home, monkeypatch):
    import threading

    import hermes_cli.config as cfgmod

    monkeypatch.setattr(cfgmod, "load_config_readonly", lambda: {"kanban": {"max_spawn_per_tick": 2}})
    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=lambda _res: stop.set())

    assert captured.get("max_spawn_per_tick") == 2


def test_cli_dispatch_reads_the_bound_from_config(kanban_home, monkeypatch):
    import argparse

    from hermes_cli import kanban as kb_cli

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {"max_spawn_per_tick": 4}})
    captured: dict = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    kb_cli._cmd_dispatch(argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False))

    assert captured.get("max_spawn_per_tick") == 4
