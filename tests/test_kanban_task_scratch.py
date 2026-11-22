"""Each kanban task gets its own scratch dir: concurrent cards on one profile never share temp space.

Two workers of one profile used to be handed the same ``<home>/cache/scratch`` (in the prompt and
in ``TMPDIR``); a helper script one wrote there under a fixed name was run by the other.
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

import hermes_constants
from hermes_constants import apply_scratch_tmp_env, get_scratch_dir, prune_scratch_dir


def kanban_task_scratch_dir(task_id, home):
    return hermes_constants.kanban_task_scratch_dir(task_id, home)


def get_session_scratch_dir():
    return hermes_constants.get_session_scratch_dir()


def _task(kb, task_id):
    return kb.Task(
        id=task_id, title="scratch", body=None, assignee="w", status="running", priority=0,
        created_by="test", created_at=1, started_at=None, completed_at=None, workspace_kind="dir",
        workspace_path=None, claim_lock="lock", claim_expires=None, tenant=None, current_run_id=1,
    )


def _spawn_env(monkeypatch, task_id, workspace):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    kbd._default_spawn(_task(kb, task_id), str(workspace))
    return captured["env"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    return root


def test_two_worker_spawn_envs_get_distinct_existing_scratch_dirs(home, tmp_path, monkeypatch):
    """The dispatcher's env for two different tasks names two different scratch dirs, each a real
    directory, with every temp var pointing into it."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env_a = _spawn_env(monkeypatch, "t_aaaa1111", workspace)
    env_b = _spawn_env(monkeypatch, "t_bbbb2222", workspace)

    assert env_a["TMPDIR"] != env_b["TMPDIR"]
    for env in (env_a, env_b):
        scratch = Path(env["HERMES_SCRATCH_DIR"])
        assert scratch.is_dir()
        assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == str(scratch)
        # A direct child of the profile's scratch root: the idle prune handles each task's
        # dir as its own top-level entry.
        assert scratch.parent == get_scratch_dir(Path(env["HERMES_HOME"]), prune=False)


def test_task_scratch_overrides_an_inherited_tmpdir_and_stays_inside_scratch(tmp_path):
    """A kanban task's temp vars always name its own dir (an inherited ``/var/folders`` TMPDIR is
    shared by every worker of the account); an odd task id cannot escape the scratch root."""
    home = tmp_path / "h"
    env = {"HERMES_HOME": str(home), "HERMES_KANBAN_TASK": "t_1234abcd", "TMPDIR": "/var/folders/zz"}
    assert apply_scratch_tmp_env(env) is True
    expected = kanban_task_scratch_dir("t_1234abcd", home)
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == env["HERMES_SCRATCH_DIR"] == str(expected)
    assert expected.is_dir()

    root = get_scratch_dir(home, prune=False)
    for hostile in ("../../etc", "a/b", "..", ""):
        path = kanban_task_scratch_dir(hostile, home)
        assert path.parent == root and path != root


def test_non_kanban_env_keeps_the_shared_scratch_dir(tmp_path):
    """No task id: the profile's shared scratch dir, and a user/OS temp var is still respected."""
    home = tmp_path / "h"
    env = {"HERMES_HOME": str(home)}
    assert apply_scratch_tmp_env(env) is True
    assert env["TMPDIR"] == str(get_scratch_dir(home, prune=False))
    user_env = {"HERMES_HOME": str(home), "TMPDIR": "/var/folders/zz"}
    assert apply_scratch_tmp_env(user_env) is False and user_env["TMPDIR"] == "/var/folders/zz"


def test_child_that_leaves_the_task_rederives_the_shared_dir(tmp_path):
    """The per-task value is Hermes-exported (marker-tagged): a child env without the task id
    (a daemon the worker cold-starts) is handed the shared dir again, not the task's."""
    home = tmp_path / "h"
    env = {"HERMES_HOME": str(home), "HERMES_KANBAN_TASK": "t_1234abcd"}
    apply_scratch_tmp_env(env)
    env.pop("HERMES_KANBAN_TASK")
    assert apply_scratch_tmp_env(env) is True
    assert env["TMPDIR"] == str(get_scratch_dir(home, prune=False))


def test_prompt_names_the_tasks_own_scratch_dir(home, monkeypatch):
    """The scratch dir the model is told to use is the task's, not the profile's."""
    from agent.prompt_builder import _local_host_hints

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_5678cdef")
    expected = kanban_task_scratch_dir("t_5678cdef", home)
    assert get_session_scratch_dir() == expected
    assert f"Scratch directory: {expected} (TMPDIR points here" in _local_host_hints()[0]
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert get_session_scratch_dir() == get_scratch_dir(home, prune=False)


def test_bound_worker_process_moves_its_temp_space_into_the_task_dir(tmp_path, monkeypatch):
    """The owner's managed worker binds the claim, then ``tempfile`` and the temp vars of this
    process (and so every terminal child) land in the task's dir."""
    import tempfile
    from contextlib import closing

    from gateway import session_kanban
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_KANBAN_RUN_ID",
                "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_PROFILE", "HERMES_SESSION_SOURCE", "HERMES_KANBAN_CLAIM_TTL_SECONDS",
                "TMPDIR", "TMP", "TEMP", "HERMES_SCRATCH_DIR"):
        monkeypatch.setenv(key, os.environ.get(key, ""))  # restored after the bind mutates them
    monkeypatch.setenv("TMPDIR", "/var/folders/shared")
    monkeypatch.setattr(tempfile, "tempdir", None)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)

    kb.init_db()
    with closing(connect()) as conn:
        task_id = kb.create_task(conn, title="bind", assignee="default")
        task = kb.claim_task(conn, task_id)
    context = {"task_id": task_id, "board": "default", "db": str(kb.kanban_db_path()),
               "run_id": task.current_run_id, "claim_lock": task.claim_lock, "workspace": str(workspace),
               "workspaces_root": str(tmp_path / "wsroot"), "profile": "default", "branch": None,
               "tenant": None, "goal_mode": False, "max_runtime_seconds": None}
    turns_done = threading.Event()
    monkeypatch.setattr(session_kanban, "_TURNS_DONE", turns_done)
    try:
        session_kanban.bind_worker_context({"policy": {"source": "kanban", "kanban_json": json.dumps(context)}})
    finally:
        turns_done.set()  # stops the submitter watcher thread the bind started

    expected = str(kanban_task_scratch_dir(task_id, tmp_path))
    assert os.environ["TMPDIR"] == os.environ["TMP"] == os.environ["TEMP"] == expected
    assert tempfile.gettempdir() == expected


def test_prune_covers_each_task_dir_independently(tmp_path):
    """An idle task dir is pruned while a sibling task's live dir stays."""
    home = tmp_path / "h"
    idle = kanban_task_scratch_dir("t_idle0000", home)
    live = kanban_task_scratch_dir("t_live0000", home)
    (idle / "open_pr.py").write_text("x", encoding="utf-8")
    (live / "open_pr.py").write_text("y", encoding="utf-8")
    ancient = time.time() - 30 * 3600
    for path in (idle / "open_pr.py", idle):
        os.utime(path, (ancient, ancient))
    assert prune_scratch_dir(get_scratch_dir(home, prune=False)) == 1
    assert not idle.exists() and (live / "open_pr.py").exists()
