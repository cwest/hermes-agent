"""A reused per-task worktree is brought up to its base before a worker gets it.

A per-task worktree is cut from the repo root's HEAD. When the base moves on
and the worktree has no commits of its own, a returning dispatch reused it as
is: from inside the stale tree the base looks *ahead*, so a worker could decide
the work was already done and report complete with no commits. Before reuse,
a clean worktree with no unique commits is fast-forwarded to the base; one
with its own commits is left alone; a dirty one that is behind is refused.
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
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    _commit(root, "base.txt")
    return root


def _task(repo: Path, **kw):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="code", assignee="dev", workspace_kind="worktree",
                             workspace_path=str(repo), **kw)
        return kb.get_task(conn, tid)


def _materialize(task) -> Path:
    return kbw._resolve_worktree_workspace(task)[0]


def test_a_clean_worktree_behind_the_base_is_fast_forwarded_on_reuse(kanban_home, repo) -> None:
    task = _task(repo)
    wt = _materialize(task)
    _commit(repo, "moved-on-1.txt")
    tip = _commit(repo, "moved-on-2.txt")
    assert _git(wt, "rev-parse", "HEAD") != tip

    again = _materialize(task)

    assert again == wt
    assert _git(wt, "rev-parse", "HEAD") == tip


def test_a_worktree_with_its_own_commits_is_left_alone(kanban_home, repo) -> None:
    task = _task(repo)
    wt = _materialize(task)
    own = _commit(wt, "work.txt")
    _commit(repo, "moved-on.txt")

    _materialize(task)

    assert _git(wt, "rev-parse", "HEAD") == own


def test_a_dirty_worktree_behind_the_base_is_refused(kanban_home, repo) -> None:
    task = _task(repo)
    wt = _materialize(task)
    stale = _git(wt, "rev-parse", "HEAD")
    _commit(repo, "moved-on.txt")
    (wt / "base.txt").write_text("uncommitted edit")

    with pytest.raises(RuntimeError, match="behind"):
        _materialize(task)
    assert _git(wt, "rev-parse", "HEAD") == stale
    assert (wt / "base.txt").read_text() == "uncommitted edit"


def test_a_fresh_worktree_is_untouched(kanban_home, repo) -> None:
    task = _task(repo)
    wt = _materialize(task)
    head = _git(wt, "rev-parse", "HEAD")
    (wt / "scratch.txt").write_text("untracked but up to date")

    _materialize(task)

    assert _git(wt, "rev-parse", "HEAD") == head
    assert (wt / "scratch.txt").exists()


def test_reuse_through_the_concrete_path_is_gated_too(kanban_home, repo) -> None:
    task = _task(repo)
    wt = _materialize(task)
    with kbc.connect() as conn:
        kbw.set_workspace_path(conn, task.id, str(wt))
        kbw.set_branch_name(conn, task.id, _git(wt, "branch", "--show-current"))
        task = kb.get_task(conn, task.id)
    tip = _commit(repo, "moved-on.txt")

    assert _materialize(task) == wt.resolve()
    assert _git(wt, "rev-parse", "HEAD") == tip
