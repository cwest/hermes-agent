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

A guard fails loudly only when the anchor deploy clone genuinely cannot
``git pull --ff-only`` — a detached HEAD, or a branch with no tracked upstream —
rather than assuming the branch must be named ``main`` (a fork legitimately
deploys from its own tracked integration branch). A linked worktree checkout is
never subject to the guard: sitting on a topic branch is what a worktree is for.
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
            # Neutralize any host-global core.excludesfile (this dev host's
            # ~/.gitignore-global carries ".worktrees/", which would mask the
            # untracked worktree dir and let a clean-tree assertion pass by
            # accident on a broken resolver). CI has no such global, so pin it
            # off here to make these assertions match CI ground truth.
            "-c", "core.excludesfile=/dev/null",
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    ).stdout


def _make_bare_origin(tmp_path: Path, name: str = "origin.git") -> Path:
    """A bare remote to give clones a real upstream to track."""
    bare = tmp_path / name
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True, capture_output=True, text=True,
    )
    return bare


def _make_repo(tmp_path: Path, name: str = "office") -> Path:
    """A deploy clone on ``main`` with a real tracked upstream.

    A genuine deploy clone tracks an upstream so the post-merge
    ``git pull --ff-only`` can fast-forward — that tracking IS the invariant the
    resolver's guard protects. The fixture models it (a bare origin + a clone
    whose ``main`` tracks ``origin/main``) so happy-path resolution reflects
    reality; a test that wants the no-upstream hazard drops the tracking by
    cutting a local-only branch or detaching HEAD.
    """
    bare = _make_bare_origin(tmp_path, f"{name}-origin.git")
    repo = tmp_path / name
    subprocess.run(
        ["git", "clone", str(bare), str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "push", "-u", "origin", "main")
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


def test_dir_deploy_clone_untracked_branch_fails_loudly(kanban_home, tmp_path):
    """A clone on a branch with no upstream can't ff-only, so it must refuse.

    A previous buggy worker left the shared clone on a local-only topic branch
    that tracks nothing. ``git pull --ff-only`` cannot work there, so building on
    it is the genuine hazard the guard exists to catch.
    """
    repo = _make_repo(tmp_path)
    # A previous buggy worker left the shared clone on a local-only topic branch.
    _git(repo, "checkout", "-b", "topic/leftover")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="office feature",
            workspace_kind="dir", workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    with pytest.raises(RuntimeError, match="cannot .*fast-forward|does not track an upstream"):
        kb.resolve_workspace(task)


def _make_fork_clone_on_tracked_branch(
    tmp_path: Path, deploy_branch: str, name: str = "fork"
) -> Path:
    """A real clone whose HEAD is a NON-main branch that TRACKS an upstream.

    Models the hermes-agent fork: ``origin/HEAD`` mirrors upstream (``main``),
    but the deploy checkout lives on its own integration branch that has a real
    tracked upstream and fast-forwards cleanly. This is a legitimate deploy
    clone, not drift.
    """
    bare = _make_bare_origin(tmp_path, f"{name}-origin.git")
    repo = tmp_path / name
    subprocess.run(
        ["git", "clone", str(bare), str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "push", "-u", "origin", "main")
    # origin/HEAD points at main (mirrors upstream), as on a real fork.
    _git(repo, "remote", "set-head", "origin", "main")
    # Cut the fork's own deploy branch and give it a tracked upstream.
    _git(repo, "checkout", "-b", deploy_branch)
    _git(repo, "push", "-u", "origin", deploy_branch)
    return repo


def test_dir_fork_clone_on_tracked_non_main_branch_dispatches(kanban_home, tmp_path):
    """A fork clone on its own tracked integration branch resolves, not refuses.

    ``origin/HEAD`` says ``main``, but the clone lives on ``cwest/integration``
    which tracks ``origin/cwest/integration`` and fast-forwards cleanly. The
    old guard demanded the branch be named ``main`` and refused this correct
    state; the corrected guard accepts any branch that tracks an upstream.
    """
    repo = _make_fork_clone_on_tracked_branch(tmp_path, "cwest/integration")
    assert _current_branch(repo) == "cwest/integration"

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="fork feature",
            workspace_kind="dir", workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    # Must NOT raise, and must redirect to a per-task worktree (not the clone).
    workspace = kb.resolve_workspace(task)
    assert workspace.resolve() == (repo / ".worktrees" / tid).resolve()
    # The fork clone is untouched — still on its integration branch.
    assert _current_branch(repo) == "cwest/integration"


def test_dir_clone_detached_at_ancestor_of_upstream_self_heals(kanban_home, tmp_path):
    """Clean tree + detached HEAD already contained upstream → auto-reattach.

    This is the review-leg polluter case: a prior lane worker checked the shared
    deploy clone out to a commit that is an ancestor of (here, equal to) the
    tracked deploy branch and exited without restoring the branch. Reattaching to
    the deploy branch and fast-forwarding is provably lossless — nothing is
    stranded — so the resolver must self-heal instead of hard-failing the spawn.
    """
    repo = _make_repo(tmp_path)
    head_sha = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "--detach", head_sha)
    # Precondition: really detached, clean, and contained in the upstream.
    assert _current_branch(repo) == "HEAD"
    assert _git(repo, "status", "--porcelain").strip() == ""

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="office feature",
            workspace_kind="dir", workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    # Must NOT raise — the recoverable detach self-heals.
    workspace = kb.resolve_workspace(task)
    assert workspace.resolve() == (repo / ".worktrees" / tid).resolve()
    # The shared clone is reattached to its deploy branch at the upstream SHA.
    assert _current_branch(repo) == "main"
    assert (
        _git(repo, "rev-parse", "HEAD").strip()
        == _git(repo, "rev-parse", "origin/main").strip()
    )
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_dir_clone_detached_at_ancestor_ff_advances_to_upstream(kanban_home, tmp_path):
    """Detached BEHIND the upstream (a strict ancestor) → reattach + fast-forward.

    The detached commit is a real ancestor of the deploy branch's upstream, not
    just equal to it. Reattaching and ``merge --ff-only`` must advance the clone
    to the upstream tip, stranding nothing.
    """
    repo = _make_repo(tmp_path)
    old_sha = _git(repo, "rev-parse", "HEAD").strip()
    # Advance the upstream past the detach point.
    (repo / "README.md").write_text("advanced\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "advance")
    _git(repo, "push", "origin", "main")
    upstream_sha = _git(repo, "rev-parse", "origin/main").strip()
    assert upstream_sha != old_sha
    # A prior worker detached at the older commit and never restored the branch.
    _git(repo, "checkout", "--detach", old_sha)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="office feature",
            workspace_kind="dir", workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    workspace = kb.resolve_workspace(task)
    assert workspace.resolve() == (repo / ".worktrees" / tid).resolve()
    # Reattached to main AND fast-forwarded to the upstream tip.
    assert _current_branch(repo) == "main"
    assert _git(repo, "rev-parse", "HEAD").strip() == upstream_sha


def test_dir_clone_detached_at_commit_not_upstream_fails_loudly(kanban_home, tmp_path):
    """Clean tree + detached HEAD carrying a commit NOT contained upstream → RAISE.

    Reattaching here would strand the detached commit, so the reattach is not
    lossless and must be refused. The guard keeps failing loudly.
    """
    repo = _make_repo(tmp_path)
    # Detach and commit — the detached commit is now reachable from nothing that
    # the upstream contains, so it would be stranded by a reattach.
    _git(repo, "checkout", "--detach", "HEAD")
    (repo / "orphan.txt").write_text("stranded\n", encoding="utf-8")
    _git(repo, "add", "orphan.txt")
    _git(repo, "commit", "-m", "orphan work off a detached HEAD")
    assert _current_branch(repo) == "HEAD"
    assert _git(repo, "status", "--porcelain").strip() == ""
    orphan_sha = _git(repo, "rev-parse", "HEAD").strip()

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="office feature",
            workspace_kind="dir", workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    with pytest.raises(RuntimeError, match="not contained|would strand|cannot .*fast-forward"):
        kb.resolve_workspace(task)
    # Nothing was reattached — the orphan commit is preserved at HEAD.
    assert _current_branch(repo) == "HEAD"
    assert _git(repo, "rev-parse", "HEAD").strip() == orphan_sha


def test_dir_clone_dirty_detached_head_fails_loudly(kanban_home, tmp_path):
    """Dirty tree + detached HEAD → RAISE, and the message says the tree is dirty.

    A dirty tree must never be auto-recovered — reattaching could discard or
    entangle uncommitted work. The refusal must name the dirtiness explicitly.
    """
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "--detach", "HEAD")
    # Leave uncommitted work in the tree.
    (repo / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    assert _git(repo, "status", "--porcelain").strip() != ""

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="office feature",
            workspace_kind="dir", workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    with pytest.raises(RuntimeError, match="(?i)dirty|uncommitted"):
        kb.resolve_workspace(task)
    # The dirty tree is left exactly as-is — nothing recovered.
    assert (repo / "wip.txt").read_text(encoding="utf-8") == "uncommitted\n"
    assert _current_branch(repo) == "HEAD"


def test_dir_clone_named_local_only_branch_still_fails(kanban_home, tmp_path):
    """A named local-only branch with no upstream still RAISES (unchanged).

    A named branch is a deliberate state — not the accidental detach the
    self-heal targets. It must never be auto-reattached; the guard keeps
    refusing exactly as before.
    """
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "topic/leftover")
    assert _current_branch(repo) == "topic/leftover"

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="office feature",
            workspace_kind="dir", workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    with pytest.raises(RuntimeError, match="does not track an upstream|cannot .*fast-forward"):
        kb.resolve_workspace(task)
    # The named branch is untouched — never auto-reattached.
    assert _current_branch(repo) == "topic/leftover"


def test_dir_linked_worktree_on_topic_branch_dispatches(kanban_home, tmp_path):
    """A dir workspace pointing at a LINKED WORKTREE on a topic branch resolves.

    The bug: ``_resolve_dir_workspace`` ran the default-branch guard against any
    path whose resolved form equals its git toplevel. A linked worktree satisfies
    that (its own root IS the toplevel), so a worktree correctly sitting on its
    topic branch was misread as a deploy clone left off ``main`` and refused.

    The fix consults ``_is_linked_worktree_checkout`` and skips the guard for a
    worktree — being on a topic branch is exactly what a worktree is for. This
    test fails if the guard is reintroduced without that predicate.
    """
    anchor = _make_repo(tmp_path, "anchor")
    worktree = tmp_path / "wt-checkout"
    _git(anchor, "worktree", "add", "-b", "topic/feature-x", str(worktree))

    # Sanity: this really is a linked worktree on a topic branch.
    assert kb._is_linked_worktree_checkout(worktree)
    assert _current_branch(worktree) == "topic/feature-x"
    assert kb._git_toplevel(worktree) == worktree.resolve()

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="work in a linked worktree",
            workspace_kind="dir", workspace_path=str(worktree),
        )
        task = kb.get_task(conn, tid)

    # Must NOT raise — the guard must be skipped for a linked worktree.
    kb.resolve_workspace(task)
    # The worktree is untouched — still on its topic branch.
    assert _current_branch(worktree) == "topic/feature-x"


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
