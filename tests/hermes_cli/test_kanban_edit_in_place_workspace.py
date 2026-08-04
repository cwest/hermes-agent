"""A ``dir`` card on an EDIT-IN-PLACE repo root must not be redirected to a worktree.

Sibling carve-out to ``test_kanban_dir_workspace_worktree.py``. That file proves
the office-deploy redirect: a ``dir`` workspace on a shared clone's repo ROOT is
turned into a per-task ``<repo>/.worktrees/<id>`` checkout so a worker never
checks out its branch in the shared clone.

That redirect is WRONG for an edit-in-place repo. ``~/.hermes`` is the working
tree of ``cwest/hermes-config``: the checkout IS the live running install,
changes land directly in it, and an hourly ``backup_commit`` cron owns
commit+push to ``main``. There is no PR flow for this repo. Redirecting such a
card to a ``wt/<id>`` worktree makes the worker commit to a branch and open an
unwanted PR, and leaves a stray worktree + branch in the live install.

The carve-out: a ``dir`` card whose ``workspace_path`` resolves to a DECLARED
edit-in-place repo root returns that path unchanged — no ``.worktrees/<id>``, no
branch, and the default-branch/upstream guard does not run (it is meaningless for
a checkout that is itself the deploy target). Every OTHER ``dir`` card keeps
today's redirect-and-guard behavior exactly (the regression fence lives in the
sibling file).

The declaration is DECLARED, not guessed: ``hermes_cli.edit_in_place_repos``
resolves an explicit set of edit-in-place roots (the Hermes home checkout) to
real paths, and both core and the homestead filing side read it, so the two
cannot drift.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """A temp HERMES_HOME that is ALSO a git repo root — the edit-in-place case.

    The fixture sets HERMES_HOME=<tmp>/.hermes and Path.home()=<tmp>, then makes
    <tmp>/.hermes a real git repo with a tracked upstream on ``main`` — the exact
    shape of the live install checkout. Because it is a repo ROOT, the legacy
    resolver would redirect it to a worktree; the carve-out must not.
    """
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
            "-c", "core.excludesfile=/dev/null",
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    ).stdout


def _make_repo_at(path: Path, name_hint: str = "origin") -> Path:
    """Turn an EXISTING ``path`` into a real clone on ``main`` with a tracked upstream.

    Models the live-install checkout: a repo root that tracks an upstream (so the
    legacy resolver would NOT trip its guard) — the carve-out must skip redirect
    AND guard regardless. ``path`` may already exist (the ``kanban_home`` fixture
    creates ``~/.hermes`` before this runs), so we ``git init`` in place and wire
    a bare origin by hand rather than ``git clone`` (which refuses a non-empty
    target).
    """
    path.mkdir(parents=True, exist_ok=True)
    bare = path.parent / f"{name_hint}-origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True, capture_output=True, text=True,
    )
    _git(path, "init", "-b", "main")
    _git(path, "remote", "add", "origin", str(bare))
    (path / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    _git(path, "add", "config.yaml")
    _git(path, "commit", "-m", "init")
    _git(path, "push", "-u", "origin", "main")
    return path


def _current_branch(path: Path) -> str:
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()


# --------------------------------------------------------------------------
# The carve-out (must FAIL before the fix, pass after)
# --------------------------------------------------------------------------

def test_dir_edit_in_place_root_resolves_in_place_not_worktree(kanban_home, tmp_path):
    """A dir card on the edit-in-place root returns that exact path, no redirect."""
    _make_repo_at(kanban_home, "hermes-config")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="chore(config): edit the live install",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
        )
        task = kb.get_task(conn, tid)

    workspace = kb.resolve_workspace(task)

    # Returned unchanged — the live install checkout itself.
    assert workspace.resolve() == kanban_home.resolve()
    # NO per-task worktree was created under it.
    assert not (kanban_home / ".worktrees" / tid).exists()
    # The checkout is untouched — still on main, still clean.
    assert _current_branch(kanban_home) == "main"


def test_dir_edit_in_place_root_persists_no_branch_name(kanban_home, tmp_path):
    """Dispatch of an edit-in-place dir card leaves NO branch_name on the row."""
    _make_repo_at(kanban_home, "hermes-config")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="chore(config): edit the live install",
            assignee="easley",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
        )
        # Resolve directly (no dispatch needed) to inspect the persisted row.
        task = kb.get_task(conn, tid)
        kb.resolve_workspace(task)
        task_after = kb.get_task(conn, tid)

    # No branch was ever set for an edit-in-place card.
    assert (task_after.branch_name or "") == ""


def test_dir_edit_in_place_root_guard_does_not_run_on_detached_head(
    kanban_home, tmp_path
):
    """The deploy-clone guard is meaningless for an edit-in-place root: skip it.

    A detached HEAD on the LIVE INSTALL is not a dispatch hazard — the install is
    the deploy target, not a shared clone that must ff-only. The carve-out must
    return the path without raising, where a non-edit-in-place repo would refuse.
    """
    _make_repo_at(kanban_home, "hermes-config")
    head = _git(kanban_home, "rev-parse", "HEAD").strip()
    _git(kanban_home, "checkout", "--detach", head)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="chore(config): edit with detached head",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
        )
        task = kb.get_task(conn, tid)

    # Must NOT raise — the guard does not run for an edit-in-place root.
    workspace = kb.resolve_workspace(task)
    assert workspace.resolve() == kanban_home.resolve()


def test_dispatch_edit_in_place_dir_hands_worker_in_place_no_branch(
    kanban_home, tmp_path, monkeypatch
):
    """Full dispatch path: the worker gets the in-place workspace, no branch set."""
    _make_repo_at(kanban_home, "hermes-config")

    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)

    spawned: list[tuple[str, str]] = []

    def worker_spawn(task, workspace):
        spawned.append((task.id, str(workspace)))
        return 4242

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="chore(config): a card that used to open an unwanted PR",
            assignee="easley",
            workspace_kind="dir",
            workspace_path=str(kanban_home),
        )
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
        result = kb.dispatch_once(conn, spawn_fn=worker_spawn, max_spawn=1)
        task_after = kb.get_task(conn, tid)

    assert any(t == tid for t, _ in spawned), result
    _, handed_ws = next(s for s in spawned if s[0] == tid)
    # The worker is handed the live install checkout itself.
    assert Path(handed_ws).resolve() == kanban_home.resolve()
    # No worktree, no branch.
    assert not (kanban_home / ".worktrees" / tid).exists()
    assert (task_after.branch_name or "") == ""
    # The declared anchor on the row is unchanged.
    assert Path(task_after.workspace_path).resolve() == kanban_home.resolve()


# --------------------------------------------------------------------------
# The refusal for worktree-kind aimed at an edit-in-place root (#5)
# --------------------------------------------------------------------------

def test_worktree_kind_on_edit_in_place_root_is_refused(kanban_home, tmp_path):
    """A worktree card explicitly aimed at an edit-in-place root is a caller error.

    Asking for an isolated worktree/branch on a checkout that IS the deploy target
    is contradictory. Refuse loudly at resolution time rather than silently
    coercing, so the filing mistake surfaces.
    """
    _make_repo_at(kanban_home, "hermes-config")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="worktree card wrongly aimed at the live install",
            workspace_kind="worktree",
            workspace_path=str(kanban_home),
        )
        task = kb.get_task(conn, tid)

    with pytest.raises((ValueError, RuntimeError), match="edit-in-place"):
        kb.resolve_workspace(task)


# --------------------------------------------------------------------------
# Regression fence: a NON-edit-in-place repo root still redirects + guards
# (mirrors the sibling file, asserted here against the same fixture so the
#  carve-out cannot silently widen to cover ordinary repos)
# --------------------------------------------------------------------------

def test_dir_non_edit_in_place_repo_root_still_redirects(kanban_home, tmp_path):
    """A dir card on an ORDINARY repo root is still redirected to a worktree."""
    repo = _make_repo_at(tmp_path / "office", "office")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="feat(office): ordinary repo still redirects",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    workspace = kb.resolve_workspace(task)
    assert workspace.resolve() == (repo / ".worktrees" / tid).resolve()
    assert kb._is_linked_worktree_checkout(workspace)


def test_dir_non_edit_in_place_repo_root_still_guards(kanban_home, tmp_path):
    """The default-branch/upstream guard still fires for a NON-edit-in-place clone."""
    repo = _make_repo_at(tmp_path / "office", "office")
    _git(repo, "checkout", "-b", "topic/leftover")  # local-only, no upstream

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="feat(office): guard must still fire",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        task = kb.get_task(conn, tid)

    with pytest.raises(RuntimeError, match="cannot .*fast-forward|does not track an upstream"):
        kb.resolve_workspace(task)


# --------------------------------------------------------------------------
# The declaration module (single source of truth)
# --------------------------------------------------------------------------

def test_edit_in_place_module_recognizes_hermes_home(kanban_home):
    """The declaration resolves the Hermes home checkout as edit-in-place."""
    from hermes_cli import edit_in_place_repos as eip

    assert eip.is_edit_in_place_root(kanban_home)
    # A resolved (symlink-normalized) form is recognized too.
    assert eip.is_edit_in_place_root(kanban_home.resolve())


def test_edit_in_place_module_rejects_other_paths(kanban_home, tmp_path):
    """An ordinary directory is NOT edit-in-place."""
    from hermes_cli import edit_in_place_repos as eip

    assert not eip.is_edit_in_place_root(tmp_path / "office")
    # A subdirectory of the Hermes home is not the ROOT, so not a match.
    assert not eip.is_edit_in_place_root(kanban_home / "skills")
