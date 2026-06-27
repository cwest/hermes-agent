# Plan: `move_task` helper + `hermes kanban move` CLI (one-card model, slice 3 Build B)

Date: 2026-06-27
Owner: eckert (implementer)
Card: t_e2583203 (slice 3 of t_8fb5741d — eliminate the multi-card review chain)
Repo: hermes-agent (this PR). Build A (close-pr-card MOVE->done) ships separately
in hermes-homestead.

## Problem

The one-card review model (parent t_8fb5741d) requires a card to physically MOVE
through lanes (todo -> doing -> review -> merge -> done), reassigned at each move,
on its own board. Slices 1 and 2 (hermes-homestead PRs #56, #58) implement the
webhook-driven inner moves by doing an inline `UPDATE tasks SET status=?, ...`
inside `kb.write_txn(conn)` plus hand-emitted `status_changed` + `assigned`
events. That inline transition has NO sanctioned home in `kanban_db.py`:

- `assign_task` changes assignee only (no status).
- `complete_task` -> `done` only.
- `archive_task` -> `archived` only.
- There is no general status-move helper, and the CLI has no `move` subcommand.

Casey's OUTER loop (card body of t_8fb5741d) needs ONE command to move a card
from the merge lane back to `doing`+eckert:
`hermes kanban move <id> --lane <status> --assignee eckert`.

## Decision

Add a small, sanctioned `move_task(conn, task_id, *, status, assignee=None,
reason=None)` to `kanban_db.py` and wire a `hermes kanban move` CLI subcommand.
This is the proper home for the status transition the three webhook skills
currently perform inline. Per hermes-agent AGENTS.md narrow-waist rule, this is a
CLI command + a library helper, NOT a new core model tool — nothing is added to
the model tool schema.

## Design

### `move_task(conn, task_id, *, status, assignee=None, reason=None) -> bool`

Mirrors the canonical inline pattern the skills already use (slice-1
`move_card_to_review`, slice-2 `_move_card`) and the shape of `assign_task`:

- Validate `status in VALID_STATUSES` up front (raise `ValueError` otherwise) —
  same guard `create_task`/`edit` use.
- Inside `write_txn(conn)`:
  - SELECT current `status`, `assignee`, `claim_lock` for the task; return
    `False` if the task does not exist.
  - Refuse to move a task that is currently running (claim_lock set AND
    status == 'running'), raising `RuntimeError` — same safety `assign_task` has,
    so we never yank a card out from under a live worker.
  - If `assignee` is provided, canonicalize it (`_canonical_assignee`) and set it;
    when the assignee actually changes, reset `consecutive_failures` /
    `last_failure_error` (the operator-intervention reset `assign_task` does) and
    emit an `assigned` event.
  - UPDATE `status` to the target. When the status actually changes, emit a
    `status_changed` event `{from, to, by: "cli:move", reason}` — the same event
    kind the slice-1/2 skills emit, so the audit trail is uniform whether the
    move came from a webhook or the CLI.
  - No-op safe: if neither status nor assignee changes, still return `True`
    (idempotent) but emit no spurious events.
- Return `True` on a found+transitioned task.

Note: `move_task` is the LIBRARY primitive. The webhook skills keep their own
copies (each skill is a self-contained standalone copy that imports kanban_db at
runtime); refactoring their inline UPDATEs to call `move_task` is noted as a
follow-up — it is cross-repo and out of this PR's blast radius.

### CLI: `hermes kanban move <id> --lane <status> [--assignee <p>]`

- New subparser `move` with positional `task_id`, required `--lane` (the target
  status; named `--lane` per the card body / outer-loop UX), optional
  `--assignee`, optional `--reason`.
- Handler `_cmd_move` calls `kb.move_task(...)`, prints a confirmation, returns 0;
  prints to stderr + returns 1 when the task is unknown. `ValueError` /
  `RuntimeError` propagate to the shared CLI error handler (bad status, running).
- `--assignee none|-|null` unassigns (mirrors `_cmd_assign`).

## TDD task list

RED -> GREEN -> REFACTOR for each:

1. `move_task` moves status only (no assignee), emits `status_changed`.
2. `move_task` moves status + reassigns, emits both `status_changed` + `assigned`.
3. `move_task` validates status against VALID_STATUSES (ValueError).
4. `move_task` returns False for unknown task.
5. `move_task` refuses a running (claimed) task (RuntimeError).
6. `move_task` is idempotent: same status+assignee -> True, no duplicate events.
7. `move_task` resets failure streak on assignee change.
8. CLI `move` subcommand: end-to-end via the CLI entrypoint against a temp
   HERMES_HOME — moves the card, prints confirmation, exit 0.
9. CLI `move` unknown id -> exit 1.
10. CLI `move` bad lane -> non-zero (ValueError surfaced).

## Acceptance (Build B slice)

- `hermes kanban move <id> --lane ready --assignee eckert` returns the SAME card
  to doing+eckert in one command (the outer loop).
- No new model tool added (narrow-waist rule honored).
- Relevant kanban_db + kanban_cli suites green, zero regressions.

## Out of scope (this PR)

- Refactoring the 3 webhook skills' inline UPDATEs to call `move_task` (cross-repo
  follow-up; noted in the PR + a comment).
- Build A (close-pr-card MOVE->done) — separate hermes-homestead PR.
- Orchestration-skill rewrites — separate follow-up card if budget-bound.
