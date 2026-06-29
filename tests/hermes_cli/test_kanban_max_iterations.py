"""Tests for the per-task ``max_iterations`` override.

A large kanban task can exhaust the inner agent budget (``agent.max_turns``,
default 90) and time out. This override lets a card carry its own iteration
budget, wired into the worker process via the *existing* ``HERMES_MAX_ITERATIONS``
env var (which ``cli.py`` already honors in its fallback chain). No new
user-facing env var is introduced — the user-facing surface is the DB column +
the ``--max-iterations`` CLI flag.

Covers three layers, mirroring ``test_kanban_goal_mode``:

1. DB: ``max_iterations`` persists through ``create_task`` + ``from_row``, and a
   legacy DB (without the column) migrates cleanly.
2. Spawn: ``_default_spawn`` sets ``HERMES_MAX_ITERATIONS`` only when the card
   carries a value; a plain card leaves the worker env clean so the global
   default (90) is preserved.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban as kc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

def test_max_iterations_defaults_none(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain task", assignee="worker")
        task = kb.get_task(conn, tid)
    assert task.max_iterations is None


def test_max_iterations_persists(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="big task",
            assignee="worker",
            max_iterations=200,
        )
        task = kb.get_task(conn, tid)
    assert task.max_iterations == 200


def test_legacy_db_migrates_max_iterations_column(tmp_path, monkeypatch):
    """A tasks table created without the column must gain it on init."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal legacy schema: tasks table missing max_iterations.
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    legacy.execute(
        "INSERT INTO tasks (id, title, status, priority, created_at, workspace_kind) "
        "VALUES ('legacy1', 'old', 'ready', 0, 1, 'scratch')"
    )
    legacy.commit()
    legacy.close()

    # init_db runs the additive migration.
    kb.init_db()
    with kb.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "max_iterations" in cols
        task = kb.get_task(conn, "legacy1")
    # Existing row keeps the safe default.
    assert task.max_iterations is None


# ---------------------------------------------------------------------------
# Spawn env
# ---------------------------------------------------------------------------

def test_spawn_sets_max_iterations_env_when_present(kanban_home, monkeypatch):
    captured = {}

    class _FakeProc:
        pid = 5252

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    # Avoid the kanban-worker skill probe touching the real skills dir.
    monkeypatch.setattr(kb, "_kanban_worker_skill_available", lambda home: False)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="big task",
            assignee="default",
            max_iterations=200,
        )
        task = kb.get_task(conn, tid)

    kb._default_spawn(task, str(kanban_home))
    env = captured["env"]
    assert env.get("HERMES_MAX_ITERATIONS") == "200"


def test_spawn_no_max_iterations_env_for_plain_task(kanban_home, monkeypatch):
    captured = {}

    class _FakeProc:
        pid = 5253

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    monkeypatch.setattr(kb, "_kanban_worker_skill_available", lambda home: False)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain", assignee="default")
        task = kb.get_task(conn, tid)

    kb._default_spawn(task, str(kanban_home))
    env = captured["env"]
    # Clean env → the worker falls through to the global default (90).
    assert "HERMES_MAX_ITERATIONS" not in env


# ---------------------------------------------------------------------------
# CLI surface: --max-iterations flag flows create → DB column, and `show`
# (--json) surfaces it. Behavior contract over the real parser + handler.
# ---------------------------------------------------------------------------

def _parser():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser


def test_cli_create_max_iterations_flag_persists(kanban_home, capsys):
    parser = _parser()
    args = parser.parse_args(
        ["kanban", "create", "big task", "--assignee", "worker",
         "--max-iterations", "200", "--json"]
    )
    rc = kc.kanban_command(args)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["max_iterations"] == 200

    # Round-trips through the DB, not just the in-memory create return.
    with kb.connect() as conn:
        task = kb.get_task(conn, out["id"])
    assert task.max_iterations == 200


def test_cli_create_without_flag_leaves_none(kanban_home, capsys):
    parser = _parser()
    args = parser.parse_args(
        ["kanban", "create", "plain task", "--assignee", "worker", "--json"]
    )
    rc = kc.kanban_command(args)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["max_iterations"] is None

