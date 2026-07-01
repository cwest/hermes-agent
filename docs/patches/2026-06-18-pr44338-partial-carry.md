# PR #44338: partial carry (kanban notifier)

Date: 2026-06-18
Branch: cwest/integration
Base tag: v2026.6.5 (commit 3c231eb)
Upstream PR: https://github.com/NousResearch/hermes-agent/pull/44338

This is a decision record. It exists so that whoever next touches the kanban
notifier on this fork understands exactly what we took from PR #44338, what we
deliberately left behind, and why the line falls where it does. If you are here
because something about kanban notifications looks half-finished, that is by
design, and this document explains the boundary.

## TL;DR

PR #44338 upstream does two distinct things under one PR number. We carried the
first and skipped the second.

- Carried: the SendResult non-delivery fix. This is the bug the fork actually
  needed fixed, and it is small and surgical.
- Not carried: a child-to-ancestor escalation feature that rides along in the
  same PR. It is large, it depends on infrastructure our base tag does not have,
  and recreating it by hand would mean rebuilding a feature with no reliable way
  to check the result against the original.

All of this disappears on its own once #44338 merges upstream and we rebase past
it. Nothing here is meant to live forever.

## What we carried

Two upstream commits, ported by hand:

- `b6fe83ffa` — retry failed kanban notifications instead of dropping them
- `45459de8c` — back off retries for repeatedly failing sends

The underlying bug: the notifier loop treated any return from `adapter.send` as
a successful delivery, including a `SendResult(success=False)`. A send that
failed (dead chat, transient gateway error) was recorded as delivered. The
pre-send cursor claim was never rewound, so the terminal blocked/completed event
that failed to send was never retried. The notification was simply gone, and no
one knew.

The fix treats `SendResult(success=False)` as a real failure: keep the
subscription alive, rewind the claim so the event is retried on a later tick,
and back off per subscription (exponential, capped at one hour) so a chat that
keeps failing is not hammered every tick.

This matched a pattern the codebase already used elsewhere — the restart
notification path already inspected `SendResult(success=False)` — so the port
landed cleanly against existing conventions.

It shipped as one signed commit on top of the manifest:

- `70bb7153f` — fix(gateway): retry failed kanban notifications instead of
  dropping them

Plus the manifest row in PATCHES.md.

## What we left behind, and why

Two upstream commits, not carried:

- `cc328e2e6` — repair follow-up notification delivery
- `2021d6bc4` — report create subscriptions accurately

These are not the named bug. Together they implement a different feature:
child-to-ancestor escalation. When a child task gets blocked or gives up, the
escalation surfaces that to whoever subscribed at the root of the task tree;
follow-up cards inherit subscriptions from completed parents; and `/sub`
reporting is corrected to match.

It is a real feature and a good one. It is just not what this card was about, and
the base makes carrying it expensive.

The recon for this card assumed all four PR commits patched files that exist on
our base, and that the large commit would merely conflict. That assumption was
wrong. Our base tag v2026.6.5 predates upstream's notifier refactor. The files
the PR edits — `gateway/kanban_watchers.py` and `gateway/slash_commands.py` — do
not exist on our base at all. On v2026.6.5 the notifier loop lives inline as
`GatewayRunner._kanban_notifier_watcher` inside `gateway/run.py`, which is a
single ~20k-line module. So nothing cherry-picks. Every line is a hand-port.

For the small fix that was fine. For the escalation feature it is not, because
the feature stands on a stack of prerequisites that simply are not present on the
base:

- `is_root_task`
- `EscalatedEvent`
- `repair_root_notify_sub`
- `inherit_notify_subs_from_completed_parents`
- `claim_escalated_child_events_for_sub` / `rewind_escalated_child_events_for_sub`
- a new `kanban_notify_escalations` table and its migration

None of those exist on v2026.6.5. Carrying the escalation feature would mean
hand-writing a new database schema, a new migration, roughly five new DB
functions, the watcher escalation path, and the `/sub` repair logic — on the
order of 900 lines, with about 333 tests to recreate. That is not porting a
conflicting hunk. That is rebuilding a feature from scratch on a divergent base,
with no dependable way to verify the result against the upstream original. The
risk of a subtle, silent divergence is exactly the kind of thing a fork manifest
is supposed to prevent, not introduce.

So we drew the line at the named bug.

## How the split maps to commits

| upstream commit | what it does | carried? |
| --- | --- | --- |
| `b6fe83ffa` | retry failed kanban notifications | yes |
| `45459de8c` | back off retries for repeatedly failing sends | yes |
| `cc328e2e6` | repair follow-up notification delivery (escalation) | no |
| `2021d6bc4` | report create subscriptions accurately (escalation) | no |

## Re-evaluation trigger

If anyone needs child-to-ancestor escalation on this fork before #44338 merges
upstream, do not quietly widen this work. File a dedicated follow-up card for the
escalation feature so the cost and the divergent-base risk are tracked in the
open. The boundary in this document is a deliberate scope decision, not an
oversight, and reopening it should be a deliberate decision too.

## Auto-retire

This whole arrangement is temporary. When PR #44338 merges upstream and ships in
a tagged release at or above our base, the rebase that moves us onto that tag
drops the carried commit and deletes the PATCHES.md row, per the auto-retire rule
in PATCHES.md. At that point the base contains the full PR — fix and escalation
feature both — and this document becomes history rather than guidance.
