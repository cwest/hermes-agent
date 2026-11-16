"""A worktree card that names an existing branch gets that branch, never a fresh cut.

``hermes kanban create --branch B`` names the branch the work belongs on,
typically an open PR's head that sibling cards contribute to. If ``B`` existed
only on ``origin``, ``_ensure_git_worktree`` silently cut a new local ``B`` from
the root's HEAD, so the worker started without the PR's commits and its push
diverged from the PR. Such a branch is now fetched from ``origin``. A branch
origin does not have is still created, as ``--branch`` documents; when origin
cannot be asked at all, the dispatch is refused rather than guessed.
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
def repos(tmp_path):
    """``origin`` (bare) with ``main`` and a PR branch ``feature/pr``; ``clone`` knows only ``main``."""
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    _commit(seed, "base.txt")
    _git(seed, "checkout", "-q", "-b", "feature/pr")
    pr_tip = _commit(seed, "pr-work.txt")
    _git(seed, "checkout", "-q", "main")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--single-branch", "-b", "main", str(origin), str(clone)], check=True)
    assert "feature/pr" not in _git(clone, "branch", "-a")
    return clone, origin, pr_tip


def _task(repo: Path, **kw):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="contribute", assignee="dev", workspace_kind="worktree",
                             workspace_path=str(repo), **kw)
        return kb.get_task(conn, tid)


def test_a_declared_branch_that_exists_only_on_origin_is_fetched(kanban_home, repos) -> None:
    clone, _origin, pr_tip = repos
    wt, branch = kbw._resolve_worktree_workspace(_task(clone, branch_name="feature/pr"))
    assert branch == "feature/pr"
    assert _git(wt, "rev-parse", "HEAD") == pr_tip
    assert (wt / "pr-work.txt").exists()


def test_a_declared_local_branch_is_checked_out_as_is(kanban_home, repos) -> None:
    clone, _origin, _pr_tip = repos
    _git(clone, "branch", "local/topic")
    _git(clone, "checkout", "-q", "local/topic")
    local_tip = _commit(clone, "local.txt")
    _git(clone, "checkout", "-q", "main")
    wt, _ = kbw._resolve_worktree_workspace(_task(clone, branch_name="local/topic"))
    assert _git(wt, "rev-parse", "HEAD") == local_tip


def test_a_declared_branch_origin_lacks_is_created_as_documented(kanban_home, repos) -> None:
    clone, _origin, _pr_tip = repos
    wt, branch = kbw._resolve_worktree_workspace(_task(clone, branch_name="feature/new"))
    assert branch == "feature/new"
    assert _git(wt, "rev-parse", "HEAD") == _git(clone, "rev-parse", "main")


def test_a_declared_branch_is_refused_when_origin_cannot_be_asked(kanban_home, repos, tmp_path) -> None:
    clone, _origin, _pr_tip = repos
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    task = _task(clone, branch_name="feature/pr")
    with pytest.raises(RuntimeError, match="feature/pr"):
        kbw._resolve_worktree_workspace(task)
    assert _git(clone, "branch", "--list", "feature/pr") == ""
    assert not (clone / ".worktrees" / task.id).exists()


def test_an_auto_derived_branch_is_still_cut_fresh(kanban_home, repos) -> None:
    clone, _origin, _pr_tip = repos
    wt, branch = kbw._resolve_worktree_workspace(_task(clone))
    assert branch.startswith("wt/")
    assert _git(wt, "rev-parse", "HEAD") == _git(clone, "rev-parse", "HEAD")


def test_a_reused_declared_worktree_is_never_moved_onto_the_root(kanban_home, repos) -> None:
    """A declared branch with nothing of its own yet (just pushed off an older
    main) is still the card's branch: reuse must not fast-forward it to main."""
    clone, _origin, _pr_tip = repos
    _git(clone, "branch", "feature/empty")
    empty_tip = _git(clone, "rev-parse", "feature/empty")
    task = _task(clone, branch_name="feature/empty")
    wt, _ = kbw._resolve_worktree_workspace(task)
    _commit(clone, "main-moved-on.txt")

    again, branch = kbw._resolve_worktree_workspace(task)

    assert (again, branch) == (wt, "feature/empty")
    assert _git(wt, "rev-parse", "HEAD") == empty_tip
