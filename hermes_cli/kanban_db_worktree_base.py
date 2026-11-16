"""Which commit a per-task worktree starts from, and keeping a reused one in step.

``_ensure_git_worktree`` cuts a new per-task branch from the repo root's HEAD.
When that base moves on, a reused worktree with no commits of its own still
sits on the old tip; from inside it the base looks ahead, so a worker can take
the work as already done and complete with no commits. :func:`ensure_fresh`
fast-forwards such a worktree before it is handed out.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def _rev(path: Path, rev: str) -> Optional[str]:
    return _kb._git_out(path, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")


def _has_commits(path: Path, base: str, tip: str) -> Optional[bool]:
    """Whether ``tip`` carries commits ``base`` lacks; ``None`` when git cannot say."""
    out = _kb._git_out(path, "rev-list", "--count", f"{base}..{tip}")
    try:
        return int(out) > 0 if out is not None else None
    except ValueError:
        return None


def _is_clean(path: Path) -> bool:
    """Unprovable counts as dirty."""
    result = _kbw._git(path, "status", "--porcelain", timeout=30)
    return result.returncode == 0 and not (result.stdout or "").strip()


def base_commit(repo_root: Path) -> Optional[str]:
    """The commit a new per-task branch in ``repo_root`` is cut from."""
    return _rev(repo_root, "HEAD")


def ensure_fresh(worktree: Path, repo_root: Path, *, task_id: str) -> None:
    """Fast-forward a reused worktree that is behind its base and has no work of
    its own. One carrying its own commits is never touched. One that is behind
    with uncommitted changes raises, so the dispatcher books a spawn failure
    instead of starting a worker in a stale tree. When git cannot answer, the
    worktree is left as it is."""
    base = base_commit(repo_root)
    head = _rev(worktree, "HEAD")
    if base is None or head is None or head == base:
        return
    if _has_commits(worktree, base, head) is not False:
        return  # its own work, or unknown
    if not _has_commits(worktree, head, base):
        return
    stale = f"task {task_id} worktree {worktree} is behind its base ({head[:9]} vs {base[:9]})"
    if not _is_clean(worktree):
        raise RuntimeError(f"{stale} and has uncommitted changes; refusing to dispatch into it")
    result = _kbw._git(worktree, "merge", "--ff-only", "--quiet", base, timeout=60)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{stale} and could not be fast-forwarded: {detail}")


# Late-bound, as in kanban_db_workspace: imported last so both are populated.
from hermes_cli import kanban_db as _kb
from hermes_cli import kanban_db_workspace as _kbw
