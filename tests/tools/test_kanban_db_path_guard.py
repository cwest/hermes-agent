"""The terminal guard that stops a sqlite3 probe from creating a stray board db."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from tools.kanban_db_path_guard import stray_board_db_refusal


@pytest.fixture
def kanban_root(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / "kanban" / "boards" / "default").mkdir(parents=True)
    (home / "kanban.db").write_bytes(b"")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    assert kb.kanban_home() == home
    return home


@pytest.mark.parametrize("command", [
    "sqlite3 {home}/kanban/boards/default/kanban.db \"SELECT id FROM tasks\"",
    "sqlite3 {home}/kanban/default.db .tables",
    "python3 -c \"import sqlite3;c=sqlite3.connect('{home}/kanban/boards/default/kanban.db')\"",
    "echo x; sqlite3 '{home}/kanban/boards/knowledge/kanban.db' 'select 1'",
])
def test_refuses_a_sqlite3_open_of_a_missing_kanban_db(kanban_root, command):
    reason = stray_board_db_refusal(command.format(home=kanban_root), str(kanban_root))
    assert reason is not None
    assert "does not exist" in reason
    assert str(kanban_root / "kanban.db") in reason


def test_refuses_a_relative_path_resolved_against_the_cwd(kanban_root):
    cwd = kanban_root / "kanban"
    assert stray_board_db_refusal("sqlite3 default.db .tables", str(cwd)) is not None


def test_refuses_a_home_relative_spelling(kanban_root, monkeypatch):
    monkeypatch.setenv("HOME", str(kanban_root.parent))
    monkeypatch.setenv("HERMES_KANBAN_HOME", "~/home")
    for spelling in ("~/home", "$HOME/home", "${HOME}/home"):
        command = f"sqlite3 {spelling}/kanban/boards/default/kanban.db .tables"
        assert stray_board_db_refusal(command, "/") is not None, spelling
    # An inline-Python probe that builds the path with expanduser.
    probe = (
        'python -c "\nimport sqlite3,os\n'
        "p=os.path.expanduser('~/home/kanban/boards/default/kanban.db')\n"
        "c=sqlite3.connect(p);print(c.execute('select id from tasks').fetchall())\""
    )
    assert stray_board_db_refusal(probe, "/") is not None


@pytest.mark.parametrize("command", [
    # The file exists, so opening it creates nothing.
    "sqlite3 {home}/kanban.db 'select 1'",
    # A read-only URI open of a missing path fails without creating it.
    "python3 -c \"import sqlite3;sqlite3.connect('file:{home}/kanban/default.db?mode=ro', uri=True)\"",
    # No sqlite3 in the command: listing or stat-ing a missing path creates nothing.
    "ls -la {home}/kanban/default.db",
    # A missing db outside the kanban tree is not a board.
    "sqlite3 {home}/../elsewhere/scratch.db .tables",
    # Sidecars and backups are not db paths.
    "sqlite3 {home}/kanban/boards/default/kanban.db-wal .tables",
    # A shell variable is not resolved; the guard does not guess.
    "for d in {home}/kanban/boards/*/; do sqlite3 \"$d/kanban.db\" 'select 1'; done",
])
def test_lets_safe_commands_through(kanban_root, command):
    assert stray_board_db_refusal(command.format(home=kanban_root), str(kanban_root)) is None


@pytest.mark.parametrize("workspace", [
    ("kanban", "workspaces", "t_demo"),
    ("kanban", "boards", "ops", "workspaces", "t_demo"),
    ("kanban", "boards", "default", "workspaces", "t_demo", "nested"),
])
@pytest.mark.parametrize("command", [
    "sqlite3 test.db 'create table t(x)'",
    "python3 -c \"import sqlite3; sqlite3.connect('app.db')\"",
])
def test_a_new_db_inside_a_task_workspace_is_allowed(kanban_root, workspace, command):
    cwd = kanban_root.joinpath(*workspace)
    cwd.mkdir(parents=True)
    assert stray_board_db_refusal(command, str(cwd)) is None
    assert stray_board_db_refusal(command.replace("test.db", f"{cwd}/test.db")
                                  .replace("app.db", f"{cwd}/app.db"), "/") is None


def test_a_multi_parameter_read_only_uri_is_allowed(kanban_root):
    command = ("python3 -c \"import sqlite3;sqlite3.connect("
               "'file:{home}/kanban/default.db?cache=shared&mode=ro', uri=True)\"")
    assert stray_board_db_refusal(command.format(home=kanban_root), "/") is None


def test_a_read_write_uri_is_still_refused(kanban_root):
    command = ("python3 -c \"import sqlite3;sqlite3.connect("
               "'file:{home}/kanban/default.db?cache=shared&mode=rwc', uri=True)\"")
    assert stray_board_db_refusal(command.format(home=kanban_root), "/") is not None


def test_a_missing_db_directly_under_boards_is_refused(kanban_root):
    command = f"sqlite3 {kanban_root}/kanban/boards/default.db .tables"
    assert stray_board_db_refusal(command, "/") is not None


def test_an_existing_named_board_db_is_allowed(kanban_root):
    board_db = kanban_root / "kanban" / "boards" / "ops" / "kanban.db"
    board_db.parent.mkdir(parents=True)
    board_db.write_bytes(b"")
    assert stray_board_db_refusal(f"sqlite3 {board_db} .tables", "/") is None


def _env_config(cwd: str) -> dict:
    return {
        "env_type": "local", "timeout": 180, "cwd": cwd, "host_cwd": None,
        "modal_mode": "auto", "docker_image": "", "modal_image": "", "daytona_image": "",
    }


def _terminal(command: str, cwd: str):
    from tools.terminal_tool import terminal_tool

    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "ok", "returncode": 0}
    mock_env.cwd = cwd
    with patch("tools.terminal_tool._get_env_config", return_value=_env_config(cwd)), \
            patch("tools.terminal_tool._start_cleanup_thread"), \
            patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
            patch("tools.terminal_tool._last_activity", {"default": 0}), \
            patch("tools.terminal_tool._session_cwd", {}), \
            patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command=command))
    return result, mock_env


def test_terminal_tool_blocks_the_probe_before_it_runs(kanban_root):
    stray = kanban_root / "kanban" / "boards" / "default" / "kanban.db"
    result, env = _terminal(f"sqlite3 {stray} 'select 1'", str(kanban_root))
    assert result["status"] == "blocked"
    assert "does not exist" in result["error"]
    env.execute.assert_not_called()
    assert not stray.exists()


def test_terminal_tool_runs_a_probe_of_the_real_board(kanban_root):
    result, env = _terminal(f"sqlite3 {kanban_root / 'kanban.db'} 'select 1'", str(kanban_root))
    assert result.get("status") != "blocked"
    env.execute.assert_called_once()
