"""Refuse a terminal command that would open a missing kanban database with sqlite3.

Opening a path that does not exist (the ``sqlite3`` CLI, or ``sqlite3.connect()`` in
inline Python) creates it as an empty file. Under the kanban tree that file looks like
a board: ``boards/default/kanban.db`` or ``kanban/default.db`` left by a probe that
guessed the default board's location, which is ``<kanban home>/kanban.db``. The
workspace-sharing scan then reads the empty file on every terminal transition.

This is a policy check on the command text, not a sandbox: a path built at run time
(a shell variable, string concatenation in a script) is not seen.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

_SQLITE_RE = re.compile(r"\bsqlite3?\b", re.IGNORECASE)
# A path-like run ending in ``.db``. ``kanban.db-wal`` and ``kanban.db.init.lock`` don't match.
_DB_PATH_RE = re.compile(r"""(?P<path>[^\s'"`;|&()<>=:,]+\.db)(?![\w.-])(?P<query>\?[^\s'"`;|()<>]*)?""")


def _expand(raw: str) -> Optional[str]:
    home = os.environ.get("HOME") or str(Path.home())
    for prefix in ("${HOME}", "$HOME"):
        if raw.startswith(prefix):
            raw = home + raw[len(prefix):]
            break
    if "$" in raw:
        return None  # a variable the guard can't resolve; don't guess
    return os.path.expanduser(raw)


def _kanban_paths() -> tuple[Path, Path]:
    from hermes_cli import kanban_db as kb

    home = Path(os.path.expanduser(str(kb.kanban_home()))).resolve(strict=False)
    return home / "kanban.db", home / "kanban"


def _is_board_level(path: Path, default_db: Path, tree: Path) -> bool:
    # Only where a board db lives or gets guessed: <home>/kanban.db, <home>/kanban/*.db,
    # <home>/kanban/boards/*.db and <home>/kanban/boards/<slug>/*.db. A task workspace
    # under workspaces/ is the task's own tree, where a new db is legitimate.
    boards = tree / "boards"
    return (
        path == default_db
        or path.parent in (tree, boards)
        or path.parent.parent == boards
    )


def _is_read_only_uri(query: Optional[str]) -> bool:
    return "ro" in parse_qs((query or "").lstrip("?")).get("mode", [])


def stray_board_db_refusal(command: str, cwd: str) -> Optional[str]:
    """Refusal text when *command* would open a missing ``*.db`` where a board db would live."""
    if not command or not _SQLITE_RE.search(command):
        return None
    default_db, tree = _kanban_paths()
    for match in _DB_PATH_RE.finditer(command):
        if _is_read_only_uri(match.group("query")):
            continue  # a read-only URI open fails on a missing path instead of creating it
        raw = _expand(match.group("path"))
        if raw is None:
            continue
        path = Path(cwd or os.getcwd(), raw) if not os.path.isabs(raw) else Path(raw)
        path = path.resolve(strict=False)
        if not _is_board_level(path, default_db, tree):
            continue
        if path.exists():
            continue
        return (
            f"{path} does not exist, and opening it with sqlite3 would create an empty file "
            "that the kanban workspace scan then reads as a board. The default board's "
            f"database is {default_db}; a named board's is "
            f"{tree / 'boards' / '<slug>' / 'kanban.db'}. Use `hermes kanban` to read the board, "
            "or open a path read-only with `sqlite3 'file:<path>?mode=ro'`."
        )
    return None
