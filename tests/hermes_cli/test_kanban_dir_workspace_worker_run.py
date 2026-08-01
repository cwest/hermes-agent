"""E2E: a card worker against a dir-workspace repo root leaves the clone on main.

This is the card's ``Done when`` bar — proven by an actual dispatcher tick that
spawns a worker (not by inspecting ``resolve_workspace`` in isolation). The
worker here does exactly what the office card workers did: it ``cd``s into the
workspace the dispatcher handed it and runs ``git checkout <topic>``. Before the
fix that mutated the shared deploy clone; after the fix the worker is handed an
isolated ``.worktrees/<task-id>`` checkout, so its ``git checkout`` cannot touch
the shared clone's branch.

Run through the REAL ``dispatch_once`` seam (claim → resolve_workspace →
set_workspace_path → spawn_fn), so a regression that reinstates in-place checkout
for dir-on-repo-root fails this test. Kept in the default suite (not the opt-in
stress dir) because it is fast and hermetic — it is the card's proof-by-worker-run.
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
            # Neutralize the host-global core.excludesfile: this dev host's
            # ~/.gitignore-global carries ".worktrees/", which would mask the
            # untracked worktree dir and let the clean-tree assertion below pass
            # by accident on a broken resolver. CI has no such global.
            "-c", "core.excludesfile=/dev/null",
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    ).stdout


def _make_deploy_clone(tmp_path: Path) -> Path:
    """A deploy clone on ``main`` with a real tracked upstream.

    A genuine deploy clone tracks an upstream so the post-merge
    ``git pull --ff-only`` can fast-forward — the invariant the resolver's guard
    protects. Model it (bare origin + clone whose ``main`` tracks ``origin/main``)
    so the happy-path worker run reflects a real deploy checkout.
    """
    bare = tmp_path / "office-origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True, capture_output=True, text=True,
    )
    repo = tmp_path / "office"
    subprocess.run(
        ["git", "clone", str(bare), str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "index.html").write_text("<h1>office</h1>\n", encoding="utf-8")
    _git(repo, "add", "index.html")
    _git(repo, "commit", "-m", "init")
    _git(repo, "push", "-u", "origin", "main")
    return repo


def _current_branch(path: Path) -> str:
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()


def test_worker_run_leaves_dir_repo_root_on_main_clean(
    kanban_home, tmp_path, monkeypatch
):
    deploy = _make_deploy_clone(tmp_path)

    # The dispatcher only spawns tasks whose assignee profile exists. In this
    # hermetic test there is no real profile tree, so stub the check.
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)

    spawned: list[tuple[str, str]] = []

    def worker_spawn(task, workspace):
        """Simulate a card worker: cd to its workspace, cut+checkout a topic branch.

        This is the exact operation that broke the shared clone. In an isolated
        worktree it is harmless; in the shared clone it would leave it off main.
        """
        ws = Path(workspace)
        _git(ws, "checkout", "-b", "topic/feature-x")
        (ws / "index.html").write_text("<h1>changed</h1>\n", encoding="utf-8")
        _git(ws, "add", "index.html")
        _git(ws, "commit", "-m", "feat: change the page")
        spawned.append((task.id, str(workspace)))
        return 4242  # fake pid

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="feat(office): a card that used to corrupt the deploy clone",
            assignee="easley",
            workspace_kind="dir",
            workspace_path=str(deploy),
        )
        # Move it into the dispatchable lane (a freshly-created card with no
        # parents promotes to ready; be explicit so the tick can claim it).
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=?", (tid,)
        )
        conn.commit()

        result = kb.dispatch_once(conn, spawn_fn=worker_spawn, max_spawn=1)

    # The worker actually ran against this task.
    assert any(t == tid for t, _ in spawned), result

    # The workspace the worker was handed is NOT the shared clone.
    _, handed_ws = next(s for s in spawned if s[0] == tid)
    assert Path(handed_ws).resolve() != deploy.resolve()
    assert Path(handed_ws).resolve() == (deploy / ".worktrees" / tid).resolve()

    # The deploy clone is STILL on main with a clean tree — the whole point.
    assert _current_branch(deploy) == "main"
    assert _git(deploy, "status", "--porcelain").strip() == ""

    # The worker's commit landed on its own branch inside the worktree.
    assert _current_branch(Path(handed_ws)) == "topic/feature-x"
    assert "changed" in (Path(handed_ws) / "index.html").read_text(encoding="utf-8")
    # ...and the shared clone's file is untouched.
    assert "office" in (deploy / "index.html").read_text(encoding="utf-8")


def _spawn_recording(spawned: list[tuple[str, str]]):
    """A spawn_fn that just records (task_id, workspace) and returns a fake pid."""

    def _spawn(task, workspace):
        spawned.append((task.id, str(workspace)))
        return 4242

    return _spawn


def _dispatch_ready_card(conn, tid, spawn_fn):
    """Promote a card to ready and run one real dispatcher tick against it."""
    conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
    conn.commit()
    return kb.dispatch_once(conn, spawn_fn=spawn_fn, max_spawn=1)


def test_dir_repo_root_anchor_survives_dispatch(kanban_home, tmp_path, monkeypatch):
    """A dir card pinned to a repo root keeps that anchor on its row after dispatch.

    The dispatcher resolves the pin to ``<repo>/.worktrees/<id>`` and hands THAT
    to the worker, but it must NOT overwrite the card's declared repo-root
    ``workspace_path``. Overwriting it destroys the anchor: ``workspace_kind``
    stays ``dir``, so the next dispatch re-resolves against the (now worktree)
    path instead of the repo root, with no recovery path on the row.
    """
    deploy = _make_deploy_clone(tmp_path)

    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)

    spawned: list[tuple[str, str]] = []

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="feat(office): anchor must survive dispatch",
            assignee="easley",
            workspace_kind="dir",
            workspace_path=str(deploy),
        )
        _dispatch_ready_card(conn, tid, _spawn_recording(spawned))
        task_after = kb.get_task(conn, tid)

    # The worker was handed the per-task worktree, not the shared clone.
    _, handed_ws = next(s for s in spawned if s[0] == tid)
    assert Path(handed_ws).resolve() == (deploy / ".worktrees" / tid).resolve()

    # The declared anchor on the row is UNCHANGED — still the repo root.
    assert task_after is not None
    assert task_after.workspace_path is not None
    assert Path(task_after.workspace_path).resolve() == deploy.resolve()


def test_dir_repo_root_redispatch_resolves_same_worktree_no_nesting(
    kanban_home, tmp_path, monkeypatch
):
    """Re-resolving a dir-on-repo-root card after dispatch yields the SAME worktree.

    The corruption manifests on the SECOND resolution: if run 1 clobbered the pin
    to the worktree path, the next dispatch's resolver sees a path that is its own
    git toplevel and cuts a worktree INSIDE it, producing a nested
    ``.worktrees/<id>/.worktrees/<id>``. Re-resolving from the row state left by a
    real dispatcher tick must land on the single top-level ``<repo>/.worktrees/<id>``.
    """
    deploy = _make_deploy_clone(tmp_path)

    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)

    spawned: list[tuple[str, str]] = []

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="feat(office): re-dispatch must not nest",
            assignee="easley",
            workspace_kind="dir",
            workspace_path=str(deploy),
        )
        # Run 1: a real dispatcher tick (claim → resolve → set_workspace_path → spawn).
        _dispatch_ready_card(conn, tid, _spawn_recording(spawned))
        expected = (deploy / ".worktrees" / tid).resolve()
        run1_ws = Path(next(ws for t, ws in spawned if t == tid)).resolve()
        # Run 2: the next tick reads the row as persisted by run 1 and re-resolves.
        task_reloaded = kb.get_task(conn, tid)
        assert task_reloaded is not None
        run2_ws = kb.resolve_workspace(task_reloaded).resolve()

    # Both resolutions land on the single top-level per-task worktree.
    assert run1_ws == expected
    assert run2_ws == expected
    # No nested worktree path is ever produced.
    assert f".worktrees/{tid}/.worktrees/{tid}" not in str(run2_ws)
    assert not (deploy / ".worktrees" / tid / ".worktrees" / tid).exists()


def test_worktree_kind_write_back_persists_resolved_path(
    kanban_home, tmp_path, monkeypatch
):
    """A worktree-kind card's write-back is unchanged: the resolved path persists.

    For ``worktree`` kind the resolved ``<repo>/.worktrees/<id>`` is recognized
    as an existing linked worktree on re-resolution, so persisting it back is
    harmless and idempotent. The fix for the dir clobber must not change this.
    """
    deploy = _make_deploy_clone(tmp_path)

    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)

    spawned: list[tuple[str, str]] = []

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="worktree card: write-back unchanged",
            assignee="easley",
            workspace_kind="worktree",
            workspace_path=str(deploy),
        )
        _dispatch_ready_card(conn, tid, _spawn_recording(spawned))
        task_after = kb.get_task(conn, tid)

    _, handed_ws = next(s for s in spawned if s[0] == tid)
    expected = (deploy / ".worktrees" / tid).resolve()
    assert Path(handed_ws).resolve() == expected
    # worktree-kind persists the resolved worktree path back to the row.
    assert task_after is not None
    assert task_after.workspace_path is not None
    assert Path(task_after.workspace_path).resolve() == expected
