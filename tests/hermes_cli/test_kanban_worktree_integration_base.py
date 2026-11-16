"""A repo can name the base its per-task worktrees are cut from.

A fork's deploy clone keeps its root on ``main`` so a post-merge pull
fast-forwards, while the work targets an integration branch that diverges
from ``main``. Cut from the root's HEAD, a worker starts on ``main`` without
the fork's patches. ``git config kanban.integrationBase <ref>`` names the ref
to cut from instead; the reuse freshness check uses the same base.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=T", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(cwd: Path, name: str) -> str:
    (cwd / name).write_text(name)
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", name)
    return _git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def fork(tmp_path) -> tuple[Path, str]:
    """Root on ``main``; ``integration`` carries a patch ``main`` lacks."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    _commit(root, "base.txt")
    _git(root, "checkout", "-q", "-b", "integration")
    tip = _commit(root, "fork-patch.txt")
    _git(root, "checkout", "-q", "main")
    return root, tip


def _materialize(repo: Path):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="code", assignee="dev", workspace_kind="worktree",
                             workspace_path=str(repo))
        task = kb.get_task(conn, tid)
    return task, kbw._resolve_worktree_workspace(task)[0]


def test_a_new_worktree_is_cut_from_the_configured_base(kanban_home, fork) -> None:
    repo, tip = fork
    _git(repo, "config", "kanban.integrationBase", "integration")
    _task, wt = _materialize(repo)
    assert _git(wt, "rev-parse", "HEAD") == tip
    assert (wt / "fork-patch.txt").exists()
    assert _git(repo, "branch", "--show-current") == "main"


def test_reuse_freshens_to_the_configured_base(kanban_home, fork) -> None:
    repo, _tip = fork
    _git(repo, "config", "kanban.integrationBase", "integration")
    task, wt = _materialize(repo)
    _git(repo, "checkout", "-q", "integration")
    new_tip = _commit(repo, "fork-patch-2.txt")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "main-only.txt")

    kbw._resolve_worktree_workspace(task)

    assert _git(wt, "rev-parse", "HEAD") == new_tip
    assert not (wt / "main-only.txt").exists()


def test_without_a_configured_base_the_root_head_is_used(kanban_home, fork) -> None:
    repo, _tip = fork
    _task, wt = _materialize(repo)
    assert _git(wt, "rev-parse", "HEAD") == _git(repo, "rev-parse", "main")


def test_an_unresolvable_configured_base_falls_back_to_the_root_head(kanban_home, fork) -> None:
    repo, _tip = fork
    _git(repo, "config", "kanban.integrationBase", "origin/not-fetched")
    _task, wt = _materialize(repo)
    assert _git(wt, "rev-parse", "HEAD") == _git(repo, "rev-parse", "main")
