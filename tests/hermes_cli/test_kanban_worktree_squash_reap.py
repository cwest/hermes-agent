"""A finished card's worktree is reaped when its PR was squash-merged.

``_cleanup_worktree_workspace`` keeps a worktree while it holds commits no
remote-tracking ref reaches. A squash merge lands the work on trunk as a new
commit, and the PR branch is usually deleted, so the original commits are never
reachable again and the tree was kept forever. Commits that are
patch-equivalent to upstream (``git cherry``) are not unpushed work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db_workspace as kbw


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60, check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-b", "main", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("-C", str(project), "config", "user.email", "t@example.com")
    _git("-C", str(project), "config", "user.name", "t")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(project), "add", "README.md")
    _git("-C", str(project), "commit", "-m", "init")
    _git("-C", str(project), "push", "origin", "HEAD:main")
    return project


def _worktree_with_commit(repo: Path, task_id: str, name: str, body: str) -> Path:
    target = repo / ".worktrees" / task_id
    kbw._ensure_git_worktree(repo, target, f"wt/{task_id}")
    _git("-C", str(target), "config", "user.email", "t@example.com")
    _git("-C", str(target), "config", "user.name", "t")
    (target / name).write_text(body, encoding="utf-8")
    _git("-C", str(target), "add", name)
    _git("-C", str(target), "commit", "-m", f"add {name}")
    return target


def _squash_merge_onto_origin(repo: Path, name: str, body: str) -> None:
    """Land the same change on origin/main as a different commit, as a squash merge does."""
    (repo / name).write_text(body, encoding="utf-8")
    _git("-C", str(repo), "add", name)
    _git("-C", str(repo), "commit", "-m", f"squash: add {name}")
    _git("-C", str(repo), "push", "origin", "HEAD:main")
    _git("-C", str(repo), "fetch", "origin")


def test_squash_merged_worktree_is_reaped(repo: Path) -> None:
    wt = _worktree_with_commit(repo, "t_sq000001", "feature.txt", "feature\n")
    _squash_merge_onto_origin(repo, "feature.txt", "feature\n")

    kbw._cleanup_worktree_workspace("t_sq000001", str(wt))

    assert not wt.exists()


def test_unmerged_local_commit_is_kept(repo: Path) -> None:
    wt = _worktree_with_commit(repo, "t_sq000002", "feature.txt", "feature\n")

    kbw._cleanup_worktree_workspace("t_sq000002", str(wt))

    assert wt.exists()


def test_a_different_change_on_trunk_does_not_count_as_merged(repo: Path) -> None:
    wt = _worktree_with_commit(repo, "t_sq000003", "feature.txt", "mine\n")
    _squash_merge_onto_origin(repo, "feature.txt", "someone else's\n")

    kbw._cleanup_worktree_workspace("t_sq000003", str(wt))

    assert wt.exists()


def test_dirty_squash_merged_worktree_is_kept(repo: Path) -> None:
    wt = _worktree_with_commit(repo, "t_sq000004", "feature.txt", "feature\n")
    _squash_merge_onto_origin(repo, "feature.txt", "feature\n")
    (wt / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

    kbw._cleanup_worktree_workspace("t_sq000004", str(wt))

    assert wt.exists()
