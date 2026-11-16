"""``kanban.review_skill``: the skill the review lane force-loads on every reviewer run.

Read at both places a review run's skills are fixed: the dispatcher's claim
(``kanban_db_dispatch``) and the reviewer's session start (``gateway.session_kanban``).
The result lives on that one spawn only; it is never written back to the card.

- unset: ``sdlc-review`` (the bundled review skill);
- a string: that skill instead; empty or ``null``: nothing;
- a mapping keyed by reviewer profile: ``mapping[reviewer]``, else ``mapping["default"]``,
  else nothing. Each value follows the string rules.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Optional

DEFAULT_REVIEW_SKILL = "sdlc-review"
_DEFAULT_KEY = "default"

_log = logging.getLogger(__name__)


def _skill_name(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    _log.warning("kanban.review_skill must be a string, null or a mapping of them (got %s); using %s",
                 type(value).__name__, DEFAULT_REVIEW_SKILL)
    return DEFAULT_REVIEW_SKILL


def forced_review_skill(config: Optional[Mapping[str, Any]] = None,
                        reviewer: Optional[str] = None) -> Optional[str]:
    """The skill to force-load on ``reviewer``'s review run, or None. ``config=None`` loads
    the merged CLI config, as ``review_dispatch_enabled`` does."""
    if config is None:
        try:
            from hermes_cli.config import load_config
            config = load_config() or {}
        except Exception:  # health: allow BLE001 -- a config error keeps the upstream default
            _log.warning("kanban.review_skill: config unreadable, using %s", DEFAULT_REVIEW_SKILL, exc_info=True)
            return DEFAULT_REVIEW_SKILL
    kanban = config.get("kanban")
    if not isinstance(kanban, Mapping) or "review_skill" not in kanban:
        return DEFAULT_REVIEW_SKILL
    value = kanban["review_skill"]
    if isinstance(value, Mapping):
        key = reviewer if reviewer in value else _DEFAULT_KEY
        return _skill_name(value[key]) if key in value else None
    return _skill_name(value)


def with_review_skill(skills: Optional[Iterable[str]], config: Optional[Mapping[str, Any]] = None,
                      reviewer: Optional[str] = None) -> list[str]:
    """``skills`` plus the forced review skill (deduplicated, order kept)."""
    forced = forced_review_skill(config, reviewer)
    return list(dict.fromkeys([*(skills or ()), *((forced,) if forced else ())]))
