"""Per-task worktree isolation for decompose siblings.

Decompose children used to inherit the root's literal ``workspace_path``,
so every sibling of a worktree-kind root pointed at the SAME checkout —
and ``_resolve_worktree_workspace``'s existing-checkout shortcut reused it
on whatever branch was there, letting sibling workers run concurrently in
one directory on one branch (cross-task provenance corruption, no lock).

Two-part fix under test:
- ``decompose_triage_task`` leaves worktree children's ``workspace_path``
  unset so each child materializes its own ``<repo>/.worktrees/<child-id>``.
- ``_resolve_worktree_workspace`` falls back to a fresh per-task worktree
  when the requested path is occupied by another task's branch (heals
  pre-existing rows that still carry a shared path).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_worktree(repo: Path, target: Path, branch: str) -> Path:
    _git(repo, "worktree", "add", str(target), "-b", branch, "HEAD")
    return target


def test_decompose_worktree_children_get_own_workspace(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="build the feature", triage=True, detached=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', "
            "workspace_path='/repo/.worktrees/root' WHERE id = ?",
            (root,),
        )
        conn.commit()

        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "spec it", "assignee": "alice", "parents": []},
                {"title": "implement it", "assignee": "bob", "parents": [0]},
            ],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 2

        for cid in child_ids:
            row = conn.execute(
                "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
                (cid,),
            ).fetchone()
            assert row["workspace_kind"] == "worktree"
            # Each child resolves its own <repo>/.worktrees/<child-id> at
            # dispatch; the root's literal path must never be shared.
            assert row["workspace_path"] is None


def test_decompose_dir_children_still_inherit_path(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="ops sweep", triage=True, detached=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='dir', "
            "workspace_path='/srv/ops' WHERE id = ?",
            (root,),
        )
        conn.commit()

        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "child", "assignee": "alice", "parents": []}],
            author="decomposer",
        )
        assert child_ids is not None
        row = conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
            (child_ids[0],),
        ).fetchone()
        assert row["workspace_kind"] == "dir"
        assert row["workspace_path"] == "/srv/ops"


def test_resolve_worktree_falls_back_when_path_occupied(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    occupied = _add_worktree(repo, repo / ".worktrees" / "sibling", "wt/sibling")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="second sibling",
            workspace_kind="worktree",
            workspace_path=str(occupied), detached=True,  # inherited shared/stale path
        )
        task = kb.get_task(conn, tid)

    workspace, branch = kb._resolve_worktree_workspace(task)
    assert workspace == (repo / ".worktrees" / tid).resolve()
    assert branch == f"wt/{tid}-second-sibling"
    # The sibling's checkout is untouched, still on its own branch.
    assert (occupied / "README.md").exists()
    head = subprocess.run(
        ["git", "-C", str(occupied), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "wt/sibling"


def test_resolve_worktree_same_branch_still_reuses(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="returning task",
            workspace_kind="worktree", detached=True,
        )
        own = _add_worktree(repo, repo / ".worktrees" / tid, f"wt/{tid}")
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(own), tid),
        )
        conn.commit()
        task = kb.get_task(conn, tid)

    workspace, branch = kb._resolve_worktree_workspace(task)
    assert workspace == own.resolve()
    assert branch == f"wt/{tid}"


def test_resolve_worktree_own_path_on_foreign_branch_keeps_legacy_reuse(
    kanban_home, tmp_path
):
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="foreign-branch checkout",
            workspace_kind="worktree", detached=True,
        )
        own = _add_worktree(repo, repo / ".worktrees" / tid, "wt/foreign")
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(own), tid),
        )
        conn.commit()
        task = kb.get_task(conn, tid)

    # The fallback target would be the occupied path itself, so the
    # legacy reuse applies rather than failing dispatch.
    workspace, branch = kb._resolve_worktree_workspace(task)
    assert workspace == own.resolve()
    assert branch == "wt/foreign"


# ---------------------------------------------------------------------------
# Freshness gate: never dispatch a worker into a behind-HEAD per-task worktree.
#
# A per-task worktree is cut off the shared checkout's HEAD. When the shared
# branch then advances but the worktree branch carries zero unique commits, a
# returning dispatch used to reuse the worktree as-is — leaving the worker N
# commits behind, where the shared branch looks AHEAD and a worker concludes
# the work is already staged (false completion, PR head never moves).
# ---------------------------------------------------------------------------


def _head(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _advance_base(repo: Path, filename: str = "advance.txt") -> str:
    """Add a commit to the repo's primary (base-branch) working tree."""
    (repo / filename).write_text("more\n", encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", f"advance {filename}")
    return _head(repo)


def test_resolve_worktree_behind_head_clean_is_fast_forwarded(
    kanban_home, tmp_path
):
    """A clean, zero-unique-commit worktree behind base is FF'd to the tip."""
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="stale but clean",
            workspace_kind="worktree",
            detached=True,
        )
        # Create the worktree on the branch the resolver derives, so the
        # existing-checkout shortcut (and its freshness gate) is what runs.
        branch_name = kb._derive_worktree_branch_name(tid, "stale but clean")
        own = _add_worktree(repo, repo / ".worktrees" / tid, branch_name)
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(own), tid),
        )
        conn.commit()
        task = kb.get_task(conn, tid)

    # Shared branch advances; the worktree stays behind with no unique commits.
    base_tip = _advance_base(repo)
    assert _head(own) != base_tip  # precondition: behind

    workspace, branch = kb._resolve_worktree_workspace(task)

    assert workspace == own.resolve()
    assert branch == branch_name
    # The worker's starting HEAD now equals the branch tip.
    assert _head(own) == base_tip


def test_resolve_worktree_behind_head_with_unique_commits_is_untouched(
    kanban_home, tmp_path
):
    """A worktree carrying real work is never rewound/rebased by the gate."""
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="stale but has work",
            workspace_kind="worktree",
            detached=True,
        )
        branch_name = kb._derive_worktree_branch_name(tid, "stale but has work")
        own = _add_worktree(repo, repo / ".worktrees" / tid, branch_name)
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(own), tid),
        )
        conn.commit()
        task = kb.get_task(conn, tid)

    # The worktree has a real, unpushed commit of its own.
    (own / "work.txt").write_text("real work\n", encoding="utf-8")
    _git(own, "add", "work.txt")
    _git(own, "commit", "-m", "agent work")
    work_head = _head(own)

    # Base advances after the work commit was made.
    _advance_base(repo)

    workspace, branch = kb._resolve_worktree_workspace(task)

    assert workspace == own.resolve()
    assert branch == branch_name
    # The work commit is preserved verbatim — the gate must not touch it.
    assert _head(own) == work_head


def test_resolve_worktree_behind_head_dirty_is_refused(kanban_home, tmp_path):
    """A dirty, behind-HEAD worktree cannot be FF'd, so dispatch refuses it."""
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="stale and dirty",
            workspace_kind="worktree",
            detached=True,
        )
        branch_name = kb._derive_worktree_branch_name(tid, "stale and dirty")
        own = _add_worktree(repo, repo / ".worktrees" / tid, branch_name)
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(own), tid),
        )
        conn.commit()
        task = kb.get_task(conn, tid)

    # Base advances; the worktree is behind AND has uncommitted changes,
    # so a fast-forward would clobber local edits — refuse instead.
    base_tip = _advance_base(repo)
    (own / "README.md").write_text("dirty edit\n", encoding="utf-8")
    assert _head(own) != base_tip  # precondition: behind

    with pytest.raises(Exception) as excinfo:
        kb._resolve_worktree_workspace(task)
    # The refusal must name staleness so the spawn-failure record is legible.
    assert "behind" in str(excinfo.value).lower()


def test_resolve_worktree_fresh_worktree_needs_no_fast_forward(
    kanban_home, tmp_path
):
    """A worktree already at the base tip is reused untouched (no-op gate)."""
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="already fresh",
            workspace_kind="worktree",
            detached=True,
        )
        branch_name = kb._derive_worktree_branch_name(tid, "already fresh")
        own = _add_worktree(repo, repo / ".worktrees" / tid, branch_name)
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(own), tid),
        )
        conn.commit()
        task = kb.get_task(conn, tid)

    fresh_head = _head(own)
    workspace, branch = kb._resolve_worktree_workspace(task)

    assert workspace == own.resolve()
    assert branch == branch_name
    assert _head(own) == fresh_head
