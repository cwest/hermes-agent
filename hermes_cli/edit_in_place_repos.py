#!/usr/bin/env python3
"""Single source of truth for which repo roots are EDIT-IN-PLACE.

Some repo checkouts are not build targets you cut a branch/PR from — they ARE
the running deploy. The canonical case is the Hermes home checkout (``~/.hermes``
is the working tree of ``cwest/hermes-config``): the checkout is the live
install, changes land directly in it, and an hourly ``backup_commit`` cron owns
commit+push to ``main``. There is no PR flow for such a repo.

The kanban dir-workspace resolver (:func:`hermes_cli.kanban_db._resolve_dir_workspace`)
redirects a ``dir`` card whose ``workspace_path`` is a repo ROOT to a per-task
linked worktree on a ``wt/<id>`` branch — correct for a shared deploy clone
(``~/src/office``), wrong for an edit-in-place root, where it makes the worker
commit to a branch and open an unwanted PR against a repo that has no PR flow.

This module makes "which roots are edit-in-place" a DECLARED fact both core and
the homestead filing side assert against, rather than a heuristic ("path is under
$HOME/.hermes", "repo has no remote") that would silently change behavior for
repos nobody audited, or prose restated across several skills that can drift.
It mirrors the shape of ``scripts/merge_authority.py`` on the homestead side:
a small explicit set resolved to real paths, plus a predicate.

The set is resolved at call time (not import time) so it honours the active
profile / ``HERMES_HOME`` — the edit-in-place root of a profiled install is that
profile's home, and a Docker/custom deployment with ``HERMES_HOME`` outside
``~/.hermes`` is still classified correctly. This is a declaration ("the Hermes
home checkout is edit-in-place"), resolved to a real path — not a guess.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "edit_in_place_roots",
    "is_edit_in_place_root",
    "WorkspaceFilingError",
]


class WorkspaceFilingError(ValueError):
    """A DETERMINISTIC workspace mis-filing — never a transient failure.

    Raised when a card's declared workspace is a contradiction that no retry can
    resolve: the canonical case is a ``workspace_kind='worktree'`` card aimed at
    an edit-in-place repo ROOT (``~/.hermes`` — the live install), which is the
    deploy target itself and must be edited in place, not from an isolated
    worktree/branch. Attempt N+1 is guaranteed to fail identically.

    It subclasses :class:`ValueError` so every existing ``except ValueError`` /
    ``except Exception`` caller is unaffected — the resolver still refuses loudly.
    The dedicated type only lets the dispatcher CLASSIFY the refusal (by type,
    not by matching a message string): a filing error must NOT decrement the
    retry budget or emit a false ``gave_up`` "retries exhausted" alarm; it is
    surfaced once as a cleanly-blocked card naming the correct filing.
    """


def edit_in_place_roots() -> frozenset[Path]:
    """Return the resolved absolute paths that are edit-in-place repo roots.

    Currently the Hermes home checkout(s). Both the ``HERMES_HOME``-honouring
    root (``get_default_hermes_root()``, correct for Docker / custom / profiled
    deployments) and the canonical ``~/.hermes`` under ``$HOME`` are included, so
    the classification matches ``_card_requires_pr``'s existing edit-in-place
    exclusion regardless of how the process resolved its home. Paths that cannot
    be resolved are simply omitted rather than raising — this predicate must
    never crash the dispatcher.
    """
    roots: set[Path] = set()

    try:
        from hermes_constants import get_default_hermes_root

        roots.add(Path(get_default_hermes_root()).expanduser().resolve(strict=False))
    except Exception:
        pass

    try:
        roots.add((Path.home() / ".hermes").expanduser().resolve(strict=False))
    except Exception:
        pass

    return frozenset(roots)


def is_edit_in_place_root(path: Path | str | None) -> bool:
    """True when *path* is a DECLARED edit-in-place repo root.

    The comparison is on the fully-resolved (symlink-normalized) path against the
    resolved declared roots, so a symlinked or ``~``-relative spelling of the
    Hermes home still matches. A subdirectory of an edit-in-place root is NOT a
    match — only the root itself, because the carve-out is about handing the
    worker the deploy checkout as-is, not about scoping into it.
    """
    if path is None:
        return False
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except Exception:
        return False
    return resolved in edit_in_place_roots()
