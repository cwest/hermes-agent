"""Edit-in-place serialization: at most one ``dir`` card running per workspace root.

``resolve_workspace`` hands a ``dir`` card its ``workspace_path`` as-is, so two
such cards on one directory run in place on the same files and one worker's
full-file write can erase the other's edits. Scratch cards get a per-task
directory and worktree cards a per-task checkout, so they never collide and are
never keyed here.

Split out of ``hermes_cli.kanban_db_dispatch``; the dispatcher seeds an
:class:`OccupiedRoots` once per tick and consults it before each claim.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


def in_place_root(workspace_kind: Optional[str], workspace_path: Optional[str]) -> Optional[str]:
    """The shared directory a card occupies while it runs, or None when it is isolated.

    Only ``dir`` cards with an absolute path run in place. Paths are resolved so
    ``/a/../a`` and symlinked spellings of one directory compare equal; a missing
    or relative path is the resolver's error to raise, not a key.
    """
    if workspace_kind != "dir" or not workspace_path:
        return None
    path = Path(workspace_path).expanduser()
    if not path.is_absolute():
        return None
    return str(path.resolve(strict=False))


class OccupiedRoots:
    """In-place roots held by ``running`` cards, extended as this tick claims more."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._roots: set[str] = set()
        for row in conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks "
            "WHERE status = 'running' AND workspace_kind = 'dir'"
        ):
            root = in_place_root(row["workspace_kind"], row["workspace_path"])
            if root is not None:
                self._roots.add(root)

    def busy_root(self, conn: sqlite3.Connection, task_id: str) -> Optional[str]:
        """The root ``task_id`` would occupy when another card already holds it."""
        row = conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return None
        root = in_place_root(row["workspace_kind"], row["workspace_path"])
        return root if root in self._roots else None

    def defer_if_busy(self, conn: sqlite3.Connection, task_id: str, deferred: list) -> bool:
        """Record ``(task_id, root)`` in ``deferred`` and return True when its root is held."""
        root = self.busy_root(conn, task_id)
        if root is None:
            return False
        deferred.append((task_id, root))
        return True

    def take(self, workspace_kind: Optional[str], workspace_path: Optional[str]) -> None:
        root = in_place_root(workspace_kind, workspace_path)
        if root is not None:
            self._roots.add(root)
