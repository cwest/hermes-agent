# Carried Patches

This file is the manifest of every change this fork carries on top of its
upstream base tag. It exists so that anyone — human or agent — can answer two
questions at a glance: *what are we carrying, and why*, and *when is it safe to
drop*.

The fork tracks tagged upstream releases, not `main`. The integration branch
(`cwest/integration`) is built off a known-good release tag and every local
change is layered on top. Each such change gets exactly one row in the table
below.

## Base

- **Base tag:** `v2026.6.5` (commit `3c231eb`)
- **Upstream:** `NousResearch/hermes-agent`
- **Integration branch:** `cwest/integration`

## Buckets

Every patch belongs to exactly one bucket:

- **`upstream-pending`** — We have an open pull request upstream and want this
  change to land there. We carry it locally only until the PR ships in a
  release. This is the default and preferred state: contribute the fix, then
  retire the local copy.
- **`permanent-local`** — A change we intend to keep indefinitely because it is
  specific to this fork and will not be sent upstream (local config, branding,
  a deliberate divergence). These rows never auto-retire.

## Auto-retire rule

A patch in the **`upstream-pending`** bucket is **retired automatically** — its
code is dropped from the fork and its row is deleted from this table — the
moment its upstream PR lands in a tagged release at or above our base tag.

Mechanically, at each rebase onto a newer base tag:

1. For every `upstream-pending` row, check whether its `upstream-PR#` is merged
   and present in the new base tag's release (the upstream changelog lists merged
   PR numbers per release).
2. If it is merged and shipped, **drop the local change** (it is now in the base)
   and **delete the row**. Do not carry a patch the base already contains —
   keeping it risks a conflict or a silent double-apply.
3. If it is still open, keep both the change and the row, and update `base-tag`
   to the new tag once the change is re-applied cleanly.

`permanent-local` rows are never retired by this rule; they are removed only by
an explicit decision.

## Patches

| upstream-PR# | intent | bucket | base-tag |
| --- | --- | --- | --- |
| [#44023](https://github.com/NousResearch/hermes-agent/pull/44023) | Make `VoiceMixer` a real `discord.AudioSource` subclass so Discord voice playback can consume it directly (adds the `discord` import and changes the class base in `plugins/platforms/discord/voice_mixer.py`). | upstream-pending | v2026.6.5 |
| [#44338](https://github.com/NousResearch/hermes-agent/pull/44338) | Fix kanban notifier SendResult non-delivery: treat `SendResult(success=False)` from `adapter.send` as a delivery failure (not a delivered ping), keep the subscription alive on send failure, rewind the pre-send claim so the terminal blocked/completed event is retried, and back off per-subscription (exponential, capped at 1h) so a dead chat is not hammered every tick. Ported by hand into `GatewayRunner._kanban_notifier_watcher` in `gateway/run.py` (upstream refactored this into `gateway/kanban_watchers.py` on a newer base). Carries only the SendResult-non-delivery fix (upstream commits `b6fe83ffa`, `45459de8c`); the PR's later child-event escalation work (`cc328e2e6`, `2021d6bc4`) is deliberately not carried because our base predates upstream's notifier refactor and lacks every prerequisite the escalation feature needs (new schema/migration plus ~5 DB functions). Whole row auto-retires when #44338 merges upstream. Full rationale: `docs/patches/2026-06-18-pr44338-partial-carry.md`. | upstream-pending | v2026.6.5 |
