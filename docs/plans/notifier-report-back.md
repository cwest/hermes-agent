# Report-back gap: webhook-spawned cards subscribe a configured target

Task: t_d3cd25a9 (fork cwest/hermes-agent). Branch `topic/notifier-report-back`
off `origin/cwest/integration`.

## Problem (final, corrected scope)

Webhook-spawned review cards (stage-pr-review via `gateway/platforms/webhook.py`
-> `kanban_create(initial_status='review')`) carry **no session context**:
neither `HERMES_SESSION_PLATFORM`/`HERMES_SESSION_CHAT_ID` nor
`HERMES_SESSION_KEY` is set. So `tools/kanban_tools.py::_maybe_auto_subscribe`
hits the `return False` at the CLI/cron/test branch and writes **no**
`kanban_notify_subs` row. With no subscription, when Lamport later **completes**
the review card, the existing notifier (which already watches the `completed`
terminal kind) has no target to deliver to. Casey never learns there's a verdict.

### What this is NOT (premise correction, confirmed with Casey via Hollis)

- There is **no into-review transition** to hook an event onto. Review cards are
  *born* in `review` (`create_task(initial_status='review')`); they never
  transition into it. Verified: no `kanban_db.py` function writes
  `status='review'`; the only writers in-tree are test files via raw SQL.
- Therefore **GAP 1 collapses into GAP 2**: the signal Casey needs ("Lamport has
  a verdict") is the review card *completing*, which **already** fires the
  `completed` event the notifier **already** watches. No new event kind, no new
  transition, no dashboard allow-list change, no `kanban.notify_watched_statuses`
  config key. Pure bias-to-edges.

The whole fix = give webhook-spawned cards a subscription via a configured
fallback target.

## Design (pre-approved shape)

### Config key (config.yaml only — AGENTS.md: not a HERMES_ env var)

```yaml
kanban:
  report_back_target:
    platform: telegram      # required for the fallback to fire
    chat_id: "-1001234567"  # required
    thread_id: "42"         # optional
```

When `platform` AND `chat_id` are both present, webhook/CLI/cron cards (cards
created with no session context) subscribe this target. When the key is absent
or incomplete, behavior is **identical to today** (returns False, no sub).

### Code change — single site

`tools/kanban_tools.py::_maybe_auto_subscribe` (the `:909` "CLI / cron / test —
no persistent channel" branch). Before returning False, attempt a config
fallback:

1. Read `kanban.report_back_target` from the already-loaded `cfg`
   (`load_config()` is already called at the top of the function;
   `cfg_get` is already imported in-scope).
2. If `platform` and `chat_id` are both truthy, set `platform`/`chat_id`/
   `thread_id` from the configured target (user_id stays None) and fall through
   to the existing `add_notify_sub(...)` call. Otherwise keep `return False`.

This reuses the identical `add_notify_sub(...)` write the gateway/TUI branches
use; the notifier consumes those rows unchanged.

### Why this is correct / safe

- **Idempotent**: `add_notify_sub` is `INSERT OR IGNORE` on
  (task, platform, chat, thread).
- **Gate preserved**: still short-circuits on
  `kanban.auto_subscribe_on_create=false`.
- **No default behavior change**: with no config key set, the function returns
  False exactly as before. Verified by keeping the existing tests green.
- **No new core tool, no env var, no cache impact.**

## Tests (TDD RED -> GREEN)

`tests/tools/test_kanban_tools.py`:

1. RED: with `kanban.report_back_target` configured in `config.yaml` and **no**
   session env, `kanban_create` (webhook context) returns `subscribed=True` and
   writes a `kanban_notify_subs` row matching the configured target.
2. Regression guard: with **no** config key and no session env, `subscribed`
   is False and no sub row is written (current behavior preserved).
3. Edge: incomplete target (platform but no chat_id) -> no sub, returns False.

Full suite to run before claiming done:
`tests/tools/test_kanban_tools.py`, `tests/gateway/test_kanban_notifier.py`,
`tests/hermes_cli/test_kanban_notify.py`.

## Out of scope (separate child card)

Retiring the cron poller `team-status-watch.py` (job e6a472e3a604) once this
lands and is cut over.
