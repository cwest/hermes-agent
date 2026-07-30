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
    repo = tmp_path / "office"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "index.html").write_text("<h1>office</h1>\n", encoding="utf-8")
    _git(repo, "add", "index.html")
    _git(repo, "commit", "-m", "init")
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
