"""Skills named by open kanban cards, so the curator's idle-age archival never removes a skill queued
work is about to load or edit.

Usage telemetry only bumps when a skill is actually loaded, so a skill a card will need next week can
sit idle long enough to age out the day before a worker picks the card up. Being named by live work
counts as use: the card's ``skills`` field, its body and its comments, matched against the candidate
skill names as whole tokens.
"""

from __future__ import annotations

import re
from typing import Iterable

# Terminal lanes. Every other status (triage, todo, scheduled, ready, running, blocked, review, and any
# lane a custom workflow adds) is live work, so an unknown status keeps the skill rather than freeing it.
_CLOSED_STATUSES = frozenset({"done", "archived"})

# Skill names are kebab/snake tokens: a hyphen is part of the name, so "git" must not match "git-helpers".
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


class BoardUnreadable(RuntimeError):
    """The board exists but could not be read; the caller must archive nothing this run."""


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    # A sentence-final "skill-name." still names the skill.
    return {tok.lower().rstrip(".-") for tok in _TOKEN_RE.findall(text)}


def _open_cards(board: str) -> list[tuple[str, list[str], str]]:
    """``(card_id, skills_field, body + comments)`` for every open card on *board*. A board whose DB file
    does not exist has no cards (an install that never used kanban); any other failure raises."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    path = kb.kanban_db_path(board=board)
    if not path.exists():
        return []
    try:
        with kbc.connect_closing(db_path=path) as conn:
            cards = []
            for task in kb.list_tasks(conn):
                if task.status in _CLOSED_STATUSES:
                    continue
                comments = "\n".join(c.body or "" for c in kb.list_comments(conn, task.id))
                text = "\n".join((task.title or "", task.body or "", comments))
                cards.append((task.id, list(task.skills or []), text))
            return cards
    except Exception as e:  # any failure to read the board must hold every archive, not crash the pass
        raise BoardUnreadable(f"kanban board {board!r} at {path}: {e}") from e


def card_referenced_skills(skill_names: Iterable[str], *, board: str = "default") -> dict[str, list[str]]:
    """Map each of *skill_names* named by an open card on *board* to the sorted ids of the cards naming it.
    Raises :class:`BoardUnreadable` when the board cannot be read."""
    wanted = {name.lower(): name for name in skill_names if name}
    refs: dict[str, set[str]] = {}
    for card_id, skills_field, text in _open_cards(board):
        named = (_tokens(text) | {s.strip().lower() for s in skills_field}) & wanted.keys()
        for key in named:
            refs.setdefault(wanted[key], set()).add(card_id)
    return {name: sorted(ids) for name, ids in refs.items()}
