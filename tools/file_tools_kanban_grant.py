"""Card-declared grant for a Kanban worker to write a protected instruction file.

A dispatched worker runs headless, so the protected-instruction approval prompt can only fail
closed, and a card whose deliverable IS ``AGENTS.md`` / ``CLAUDE.md`` could never finish. The
card body may declare those deliverables when it is filed::

    protected-writes: AGENTS.md, docs/CLAUDE.md

The human act of filing (and reviewing) the card stands in for the approval tap, so the grant
covers exactly what was filed and nothing a running agent can add:

- only the worker that OWNS the card, on the card's current run: never a delegate child, a cron
  run inside the worker, or a stale worker whose run was superseded;
- only a declaration filed with the card: ANY later edit of its title/body/spec (``edited`` or
  ``specified``) voids the grant permanently, because a worker can rewrite a card through the
  CLI and the edit records no author; re-file the card to restore it;
- only cards a worker did not create (``creator_task_id`` unset), so an injected worker cannot
  file a self-granting child card;
- only the exact relative paths declared, inside the card's own workspace, on this host's disk:
  no globs, no ``..``, no absolute entries; the target's realpath must be exactly
  ``realpath(workspace)/<entry>``, so a symlink anywhere on the way out of the workspace fails.

Residual trust: whoever can file a card on the board already steers what its worker does with
every unprotected file and its terminal; the grant extends that trust to the named instruction
files of that one card's workspace, and no further.

Every grant is recorded as a ``protected_write_granted`` event on the card. Any failure to prove
the grant answers "no grant" and the normal fail-closed prompt runs.
"""

import logging
import os
import re

from agent.delegation_context import owned_kanban_task

logger = logging.getLogger(__name__)

_FIELD = re.compile(r"^\s*(?:[-*]\s+)?protected-writes:\s*(.*?)\s*$", re.IGNORECASE | re.MULTILINE)
_UNSAFE_CHARS = frozenset("*?[]\\~")
# Every task-row write that can change a card's body or spec emits one of these.
_SPEC_CHANGE_EVENTS = ("edited", "specified")


def declared_protected_writes(body: str | None) -> tuple[str, ...]:
    """Relative paths the card body declares under ``protected-writes:``; unsafe entries dropped."""
    entries: list[str] = []
    for match in _FIELD.finditer(body or ""):
        for raw in match.group(1).split(","):
            entry = raw.strip().strip("`").strip()
            if _is_safe_entry(entry):
                entries.append(entry)
    return tuple(dict.fromkeys(entries))


def _is_safe_entry(entry: str) -> bool:
    if not entry or os.path.isabs(entry) or any(c in _UNSAFE_CHARS for c in entry):
        return False
    parts = entry.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _owned_run(conn, kb, task_id: str):
    """The card when this process owns its current run and its spec predates the run, else None."""
    task = kb.get_task(conn, task_id)
    raw_run = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    if task is None or not raw_run.isdigit() or task.current_run_id != int(raw_run):
        return None
    # Any spec edit voids the grant for good: a worker can rewrite a card's body through the CLI
    # (its own mid-run, or an idle sibling's), and the edit event does not say who made it.
    marks = ",".join("?" * len(_SPEC_CHANGE_EVENTS))
    edited = conn.execute(
        f"SELECT 1 FROM task_events WHERE task_id = ? AND kind IN ({marks}) LIMIT 1",
        (task_id, *_SPEC_CHANGE_EVENTS)).fetchone()
    if edited:
        return None
    created = next((e for e in kb.list_events(conn, task_id) if e.kind == "created"), None)
    if created is None or (created.payload or {}).get("creator_task_id"):
        return None
    return task


def grant_declared_protected_writes(targets: dict[str, str]) -> set[str]:
    """Which of ``targets`` (path -> its realpath on this host) the owning worker's card grants.

    Records one ``protected_write_granted`` event listing the granted entries; returns the
    granted keys of ``targets`` (empty on any doubt)."""
    task_id = owned_kanban_task()
    if not task_id or not targets:
        return set()
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        with kbc.connect_closing() as conn:
            task = _owned_run(conn, kb, task_id)
            if task is None or not task.workspace_path:
                return set()
            entries = declared_protected_writes(task.body)
            if not entries:
                return set()
            workspace = os.path.realpath(task.workspace_path)
            by_path = {os.path.join(workspace, entry): entry for entry in entries}
            granted = {key: by_path[real] for key, real in targets.items() if real in by_path}
            if granted:
                with kbc.write_txn(conn):
                    kb._append_event(conn, task_id, "protected_write_granted",
                                     {"paths": sorted(set(granted.values()))},
                                     run_id=task.current_run_id)
            return set(granted)
    except Exception:  # any doubt is "no grant": the caller falls back to the fail-closed prompt
        logger.warning("protected-writes grant check failed for %s; not granting", task_id, exc_info=True)
        return set()
