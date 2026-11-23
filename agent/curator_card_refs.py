"""Skills named by open kanban cards, so no curator archive path removes a skill queued work is about to
load or edit.

Usage telemetry only bumps when a skill is actually loaded, so a skill a card will need next week can
sit idle long enough to age out the day before a worker picks the card up. Being named by live work
counts as use: the card's ``skills`` field, its title, body and comments, matched against the candidate
skill names as whole tokens.

Every board is scanned (archived boards included: they can be unarchived with their cards intact), plus
any ``HERMES_KANBAN_DB`` pin. Board paths are built from the boards tree directly rather than through
``kanban_db_path``, which collapses every slug onto the pinned file inside a worker env. Each board is
opened ``mode=ro``: the kanban connector would open it read-write and run schema migrations.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

# Terminal lanes. Every other status (triage, todo, scheduled, ready, running, blocked, review, and any
# lane a custom workflow adds) is live work, so an unknown status keeps the skill rather than freeing it.
_CLOSED_STATUSES = ("done", "archived")

# Skill names are kebab/snake tokens: a hyphen is part of the name, so "git" must not match "git-helpers".
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


class BoardUnreadable(RuntimeError):
    """A board exists but could not be read; the caller must archive nothing."""


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    # A sentence-final "skill-name." still names the skill.
    return {tok.lower().rstrip(".-") for tok in _TOKEN_RE.findall(text)}


def _skills_field(raw: str | None) -> set[str]:
    """A card's ``skills`` JSON list, normalised the way the worker spawn resolves it (``/name`` is ``name``)."""
    try:
        parsed = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return set()
    if not isinstance(parsed, list):
        return set()
    return {cleaned for item in parsed if (cleaned := str(item or "").strip().lstrip("/").lower())}


def board_db_paths() -> list[Path]:
    """Every board DB the curator must honour: all boards in the tree plus an off-tree ``HERMES_KANBAN_DB``
    pin, deduplicated. Raises :class:`BoardUnreadable` if the boards cannot be enumerated."""
    from hermes_cli import kanban_db as kb

    try:
        slugs = [meta.get("slug") or kb.DEFAULT_BOARD for meta in kb.list_boards(include_archived=True)]
    except Exception as e:  # an unlisted board may hold the only card naming a skill
        raise BoardUnreadable(f"could not enumerate kanban boards: {e}") from e
    paths = [kb.kanban_home() / "kanban.db" if slug == kb.DEFAULT_BOARD else kb.boards_root() / slug / "kanban.db"
             for slug in slugs]
    if pin := os.environ.get("HERMES_KANBAN_DB", "").strip():
        paths.append(Path(pin).expanduser())
    unique: dict[str, Path] = {}
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        unique.setdefault(key, path)
    return list(unique.values())


def _open_cards(path: Path) -> list[tuple[str, set[str], str]]:
    """``(card_id, skills_field, title + body + comments)`` for every open card in the board DB at *path*.
    A missing file, or one with no ``tasks`` table (never initialised), has no cards; any other failure raises."""
    if not path.exists():
        return []
    marks = ",".join("?" for _ in _CLOSED_STATUSES)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "tasks" not in tables:
                return []
            cards = conn.execute(
                f"SELECT id, title, body, skills FROM tasks WHERE status NOT IN ({marks})", _CLOSED_STATUSES,
            ).fetchall()
            comments: dict[str, list[str]] = {}
            if "task_comments" in tables:
                for task_id, body in conn.execute(
                    f"SELECT c.task_id, c.body FROM task_comments c JOIN tasks t ON t.id = c.task_id "
                    f"WHERE t.status NOT IN ({marks})", _CLOSED_STATUSES,
                ):
                    comments.setdefault(task_id, []).append(body or "")
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as e:
        raise BoardUnreadable(f"kanban board at {path}: {e}") from e
    return [(cid, _skills_field(skills), "\n".join((title or "", body or "", *comments.get(cid, []))))
            for cid, title, body, skills in cards]


def card_referenced_skills(skill_names: Iterable[str]) -> dict[str, list[str]]:
    """Map each of *skill_names* named by an open card on any board to the sorted ids of the cards naming
    it. Raises :class:`BoardUnreadable` when any board cannot be read."""
    wanted = {name.lower(): name for name in skill_names if name}
    if not wanted:
        return {}
    refs: dict[str, set[str]] = {}
    for path in board_db_paths():
        for card_id, skills_field, text in _open_cards(path):
            named = skills_field & wanted.keys()
            lowered = text.lower()
            # Tokenise only text that could name a candidate; the token match then rejects substrings.
            if any(key in lowered for key in wanted):
                named |= _tokens(text) & wanted.keys()
            for key in named:
                refs.setdefault(wanted[key], set()).add(card_id)
    return {name: sorted(ids) for name, ids in refs.items()}


def archive_hold_reason(skill_name: str) -> str | None:
    """Why *skill_name* must not be archived now, or None. Fails closed: an unreadable board holds it."""
    try:
        cards = card_referenced_skills([skill_name]).get(skill_name)
    except BoardUnreadable as e:
        return f"kanban board unreadable ({e})"
    return f"referenced by open kanban card(s) {', '.join(cards)}" if cards else None
