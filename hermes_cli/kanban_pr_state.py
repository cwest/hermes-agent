"""Live GitHub state of a PR URL, for the ``active_pr`` respawn guard.

A PR URL in a card comment is a snapshot: the PR behind it can be closed,
merged or deleted later, or never have existed. :func:`resolve_pr_state` asks
``gh`` what the PR is now, so the guard holds a card only while its PR can
still be worked.
"""
from __future__ import annotations

import json
import re
import subprocess

# gh's GraphQL error for a PR GitHub does not have (never created, or deleted).
_NOT_FOUND_RE = re.compile(r"could not resolve to a pullrequest", re.IGNORECASE)

# States that are never active work, so they never hold a card. ``open`` and
# ``unknown`` (gh missing, logged out, offline, timed out, unparseable) both
# hold: an unresolvable state is never less safe than the guard without a lookup.
INACTIVE_PR_STATES = frozenset({"closed", "merged", "not_found"})

_GH_TIMEOUT_SECONDS = 20


def resolve_pr_state(pr_url: str) -> str:
    """``open`` | ``closed`` | ``merged`` | ``not_found`` | ``unknown``."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "state"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_GH_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "not_found" if _NOT_FOUND_RE.search(result.stderr or "") else "unknown"
    try:
        state = json.loads(result.stdout or "{}").get("state")
    except (ValueError, AttributeError):
        return "unknown"
    state = state.strip().lower() if isinstance(state, str) else ""
    return state if state in ("open", "closed", "merged") else "unknown"
