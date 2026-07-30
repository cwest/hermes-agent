"""A ``dir`` workspace whose path is a repo root must not be checked out in place.

Regression for the recurring office-deploy defect: office cards are created
with ``workspace_kind=dir`` and ``workspace_path`` pointing at the shared
deploy clone ``~/src/office``, which is meant to stay on ``main`` permanently
so the post-merge ``git pull --ff-only`` can fast-forward. A worker that ran
``git checkout <topic-branch>`` in that shared clone left it on a topic branch
twice in one session, breaking the fast-forward and dirtying the base for the
next card.

The fix: when a ``dir`` workspace_path is a git repo ROOT, ``resolve_workspace``
materializes a per-task linked worktree under ``<repo>/.worktrees/<task-id>``
(the same machinery the ``worktree`` kind uses) instead of returning the shared
clone. The worker then lands in an isolated worktree — its skill's Step-0
isolation detection (``GIT_DIR != GIT_COMMON``) sees it and works there without
touching the shared clone. A ``dir`` workspace that is NOT a repo root (a plain
ops directory) keeps returning the directory itself.

A guard fails loudly if the anchor deploy clone is found off its default branch,
rather than silently building on a dirty, wrong-branch base.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


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
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    ).stdout


def _make_repo(tmp_path: Path, name: str = "office") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _current_branch(path: Path) -> str:
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()


def test_dir_repo_root_resolves_to_worktree_not_in_place(kanban_home, tmp_path):
    """A dir workspace on a repo root returns a per-task worktree, not the clone."""
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="office feature",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    workspace = kb.resolve_workspace(task)

    # The worker must NOT be handed the shared deploy clone itself.
    assert workspace.resolve() != repo.resolve()
    # It gets a per-task linked worktree under the repo's .worktrees/.
    assert workspace.resolve() == (repo / ".worktrees" / tid).resolve()
    assert kb._is_linked_worktree_checkout(workspace)
    # The shared clone is untouched — still on main, still clean.
    assert _current_branch(repo) == "main"
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_dir_repo_root_concurrent_tasks_get_distinct_worktrees(kanban_home, tmp_path):
    """Two dir-on-repo-root tasks never collide on branch state."""
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        t1 = kb.create_task(
            conn, title="card one",
            workspace_kind="dir", workspace_path=str(repo),
        )
        t2 = kb.create_task(
            conn, title="card two",
            workspace_kind="dir", workspace_path=str(repo),
        )
        task1 = kb.get_task(conn, t1)
        task2 = kb.get_task(conn, t2)

    ws1 = kb.resolve_workspace(task1)
    ws2 = kb.resolve_workspace(task2)

    assert ws1.resolve() != ws2.resolve()
    assert ws1.resolve() == (repo / ".worktrees" / t1).resolve()
    assert ws2.resolve() == (repo / ".worktrees" / t2).resolve()
    # The shared clone stays on main regardless of concurrent resolution.
    assert _current_branch(repo) == "main"


def test_dir_repo_root_runtime_files_not_carried_into_worker_branch(
    kanban_home, tmp_path
):
    """Runtime-written files in the deploy tree don't leak into the worktree.

    The preview supervisor writes files like ``deploy/caddy/blog-backends.map``
    into the deploy clone's working tree. A fresh worktree checkout must not
    carry that incidental, uncommitted WIP.
    """
    repo = _make_repo(tmp_path)
    caddy = repo / "deploy" / "caddy"
    caddy.mkdir(parents=True)
    (caddy / "blog-backends.map").write_text("runtime-generated\n", encoding="utf-8")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="office feature",
            workspace_kind="dir", workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    workspace = kb.resolve_workspace(task)

    # The runtime file lives only in the shared clone's tree, not the worktree.
    assert not (workspace / "deploy" / "caddy" / "blog-backends.map").exists()
    assert (repo / "deploy" / "caddy" / "blog-backends.map").exists()
    # And it stays untracked in the shared clone (never staged into a branch);
    # git --porcelain collapses the wholly-untracked tree to its top dir.
    status = _git(repo, "status", "--porcelain").strip()
    assert status.startswith("??")
    assert "deploy/" in status


def test_dir_plain_directory_still_returns_directory(kanban_home, tmp_path):
    """A dir workspace that is NOT a git repo root keeps the legacy behavior."""
    opsdir = tmp_path / "ops"

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="ops sweep",
            workspace_kind="dir", workspace_path=str(opsdir),
        )
        task = kb.get_task(conn, tid)

    workspace = kb.resolve_workspace(task)
    assert workspace.resolve() == opsdir.resolve()
    assert opsdir.is_dir()


def test_dir_subdir_of_repo_still_returns_directory(kanban_home, tmp_path):
    """A dir workspace pointing INSIDE a repo (not its root) is left as-is.

    Only the repo-root case is the deploy-clone hazard; a task explicitly
    scoped to a subdirectory keeps working in place.
    """
    repo = _make_repo(tmp_path)
    subdir = repo / "subproject"
    subdir.mkdir()

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="scoped work",
            workspace_kind="dir", workspace_path=str(subdir),
        )
        task = kb.get_task(conn, tid)

    workspace = kb.resolve_workspace(task)
    assert workspace.resolve() == subdir.resolve()


def test_dir_deploy_clone_off_main_fails_loudly(kanban_home, tmp_path):
    """If the deploy clone is found off its default branch, raise — don't build."""
    repo = _make_repo(tmp_path)
    # A previous buggy worker left the shared clone on a topic branch.
    _git(repo, "checkout", "-b", "topic/leftover")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="office feature",
            workspace_kind="dir", workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    with pytest.raises(RuntimeError, match="not on its default branch|off .*main"):
        kb.resolve_workspace(task)


def _exclude_lines(repo: Path) -> list[str]:
    common = kb._git_common_dir(repo)
    assert common is not None
    exclude = common / "info" / "exclude"
    if not exclude.exists():
        return []
    return exclude.read_text(encoding="utf-8").splitlines()


def test_dir_repo_root_excludes_worktrees_dir_from_anchor(kanban_home, tmp_path):
    """The anchor clone gets ``.worktrees/`` in info/exclude, version-independent.

    On git >= 2.54, ``git worktree add <repo>/.worktrees/<id>`` leaves the
    anchor clone showing ``?? .worktrees/`` in ``git status`` — a dirty tree on
    a deploy clone pinned to ``main``, which breaks the post-merge
    ``git pull --ff-only`` this redirect exists to protect. Older git hid it, so
    ``test_dir_repo_root_resolves_to_worktree_not_in_place``'s clean-tree
    assertion only failed on newer git. This test asserts the actual fix — the
    exclude entry — so it catches a regression on ANY git version.
    """
    repo = _make_repo(tmp_path)

    # Precondition: the anchor does not already ignore .worktrees/.
    assert ".worktrees/" not in _exclude_lines(repo)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="office feature",
            workspace_kind="dir", workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    kb.resolve_workspace(task)

    # The exclude now carries the entry, so the worktree dir never dirties the
    # anchor regardless of git version.
    assert ".worktrees/" in _exclude_lines(repo)
    # And the anchor is clean — the assertion that fails on git 2.54 without
    # the fix.
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_dir_repo_root_exclude_is_idempotent(kanban_home, tmp_path):
    """Resolving twice (or with a pre-existing entry) never duplicates the line."""
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        t1 = kb.create_task(
            conn, title="card one",
            workspace_kind="dir", workspace_path=str(repo),
        )
        t2 = kb.create_task(
            conn, title="card two",
            workspace_kind="dir", workspace_path=str(repo),
        )
        task1 = kb.get_task(conn, t1)
        task2 = kb.get_task(conn, t2)

    kb.resolve_workspace(task1)
    kb.resolve_workspace(task2)

    lines = _exclude_lines(repo)
    assert lines.count(".worktrees/") == 1


def test_worktree_kind_repo_root_also_excludes_worktrees_dir(kanban_home, tmp_path):
    """The ``worktree`` kind writes under .worktrees/ too — same exclude fix.

    A ``worktree``-kind task whose workspace_path names a repo root anchors a
    linked worktree at ``<repo>/.worktrees/<id>``, so it shares the exact
    dirty-anchor exposure. The exclude must be applied there as well.
    """
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="worktree card",
            workspace_kind="worktree", workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    kb.resolve_workspace(task)

    assert ".worktrees/" in _exclude_lines(repo)
    assert _git(repo, "status", "--porcelain").strip() == ""
