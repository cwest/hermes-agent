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

- **Base tag:** `v2026.6.19` (commit `681cd638d`)
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

**Per-row override.** A row may declare its own retire trigger in the `intent`
column when this default PR-merge rule does not apply — e.g. a PR that was
**closed administratively** rather than merged can never satisfy "its PR landed
in a release," so its row must be keyed on observable upstream *behavior*
instead. When a row states a behavior-keyed trigger, that trigger wins over this
default; do **not** retire such a row on a PR-merge signal. See the #44338 row.

## Patches

| upstream-PR# | intent | bucket | base-tag |
| --- | --- | --- | --- |
| [#44023](https://github.com/NousResearch/hermes-agent/pull/44023) | Make `VoiceMixer` a real `discord.AudioSource` subclass so Discord voice playback can consume it directly (adds the `discord` import and changes the class base in `plugins/platforms/discord/voice_mixer.py`). Companion test commit gives the mocked `discord.AudioSource` in the gateway conftest a real base class so the 19 `TestVoiceMixerCore` tests run without the real `discord` package (they skip when `numpy` from the `voice` extra is absent). PR **OPEN** upstream. | upstream-pending | v2026.6.19 |
| [#44338](https://github.com/NousResearch/hermes-agent/pull/44338) | Fix kanban notifier SendResult non-delivery: treat `SendResult(success=False)` from `adapter.send` as a delivery failure (not a delivered ping), keep the subscription alive on send failure, rewind the pre-send claim so the terminal blocked/completed event is retried, and back off per-subscription (exponential, capped at 1h) so a dead chat is not hammered every tick. Re-ported by hand into `GatewayKanbanWatchersMixin._kanban_notifier_watcher` in `gateway/kanban_watchers.py` — upstream's god-file Phase 3 refactor extracted the notifier loop out of `gateway/run.py` (its v2026.6.5 home) into that mixin, so the carry now targets the loop's new location. Carries only the SendResult-non-delivery fix (upstream commits `b6fe83ffa`, `45459de8c`); the PR's later child-event escalation work (`cc328e2e6`, `2021d6bc4`) is deliberately not carried. **Retire trigger (behavior-keyed, NOT PR-merge-keyed):** #44338 was **closed administratively** (fork CI gating), not rejected, so the default "auto-retire when the PR merges" rule can never fire and must not be used for this row. Retire this row **only when upstream `gateway/kanban_watchers.py` implements ALL of: (i) `SendResult(success=False)` failure-detection AND (ii) keep-subscription-alive-on-permanent-failure AND (iii) bounded exponential backoff.** Watch #45940 and #46443, but do **NOT** drop on #45940 merge alone — #45940 only adds detection (i) and would regress behaviors (ii) keep-alive and (iv) backoff if this carry were dropped on its merge. Verify all three behaviors are present in the base tag's `gateway/kanban_watchers.py` before retiring. Full rationale: `docs/patches/2026-06-18-pr44338-partial-carry.md`. | upstream-pending | v2026.6.19 |
| [#46549](https://github.com/NousResearch/hermes-agent/pull/46549) | Fix the kanban dispatcher wedging review tasks out of respawn: `check_respawn_guard` applied the `recent_success` and `active_pr` guards — which exist only to stop a *builder* re-opening a duplicate PR — to review-lane spawns too, so a card in `status='review'` (whose build run already completed and left a PR-URL comment) got blocked from spawning its reviewer for up to 24h. The fix reads `tasks.status` and skips those two guards for `status='review'` while keeping `rate_limit_cooldown` and `blocker_auth` active. Carried whole and unmodified via `git cherry-pick -x` (upstream commit `32502fb01`, author `demi`) — clean apply, no hand-port; our base already has the `check_respawn_guard` shape the PR targets. Auto-retires when #46549 merges upstream. Note: the bypass keys on `status='review'`, the canonical review-lane state (23/24 historical review cards flow through it); a one-off review card hand-parked in `status='ready'` is out of scope by design — it is staged into `review` instead of widening this patch. Full rationale: `docs/patches/2026-06-18-pr46549-review-respawn-guard.md`. PR **OPEN** upstream; auto-retires when #46549 merges. | upstream-pending | v2026.6.19 |
| fork PR [cwest#4](https://github.com/cwest/hermes-agent/pull/4) (no upstream PR yet) | Run the in-process kanban dispatcher on a dedicated single-thread executor so a busy default `ThreadPoolExecutor` (saturated by agent turns + their nested tool/sub-agent/vision/compression fan-out) can never starve the dispatcher tick. Routes every dispatcher offload (zombie reaper, auto-decompose, `_tick_once`, `_ready_nonempty`) through the private executor; `dispatch_interval_seconds` and the bad-tick health window are untouched. Re-applied by hand into `GatewayKanbanWatchersMixin` in `gateway/kanban_watchers.py` (upstream's god-file Phase 3 refactor moved the dispatcher loop there from `gateway/run.py`); preserves upstream's `_release_singleton_lock` in both the CancelledError and normal-exit paths. Ships a starvation regression test (`test_gateway_dispatcher_tick_not_starved_by_busy_default_executor`) that fails on the old shared-pool behaviour. Cherry-picked from `0b97e99e8`. **Retire trigger:** fork PR #4 is OPEN against `main` and has **no upstream NousResearch PR number yet** — so the default PR-merge rule cannot key on an upstream PR. Retire only when the dedicated-executor fix (or an equivalent that keeps the in-process dispatcher off the shared default pool) is present in the base tag's `gateway/kanban_watchers.py`. | upstream-pending | v2026.6.19 |
| fork PR [cwest#6](https://github.com/cwest/hermes-agent/pull/6) (fork-local, MERGED to `cwest/integration`) | Count `skills.external_dirs`-granted skills in the profiles dashboard and `hermes profile list` so the count matches `hermes skills list` exactly. `_count_skills` previously only `rglob`-ed `<profile>/skills` (no symlink following, no `external_dirs`), so fork profiles that source all skills from the shared `~/.hermes/skills` registry showed "Skills: 0". Extracts `_scan_skill_dirs(local, external)`, adds `count_profile_skills` + `get_external_skills_dirs_for`, and makes `_count_skills` delegate to that single source of truth (symlink-following + frontmatter-name dedup). Also adds `casey@geeknest.com → cwest` to the `scripts/release.py` author-map (re-appended at the dict tail after the rebase conflict, all upstream author entries preserved). Tests: `TestCountProfileSkills` (external-only profile, symlinked packages, local-precedence dedup, missing-config). Fork-specific behavior (the fork's `external_dirs` grant) — kept indefinitely; not sent upstream. | permanent-local | v2026.6.19 |
| fork-local (no PR; closed fork PR [cwest#2](https://github.com/cwest/hermes-agent/pull/2)) | Optional per-route action allow-list filter for webhook routes (`feat(webhook)`), a local feature for the fork's webhook deployment. Kept indefinitely as a deliberate local divergence. | permanent-local | v2026.6.19 |
| fork-local (CI infra) | Daily upstream-sync GitHub Actions workflow that fetches/rebases the patch queue onto the newest upstream tag, runs tests, and opens a review PR (`.github/workflows/fork-daily-sync.yml`). Fork-only release engineering; never sent upstream. | permanent-local | v2026.6.19 |
| fork-local (CI infra) | Secret-scan gate (gitleaks) that runs on every PR/push to `cwest/integration`, enforcing the fork's "no secrets, ever" invariant on our own commits (`.github/workflows/fork-secret-scan.yml`). Fork-only release engineering; never sent upstream. | permanent-local | v2026.6.19 |
| fork-local (this manifest) | `PATCHES.md` itself — the manifest of everything the fork carries on top of its upstream base tag, plus the bucket model, the auto-retire rule, and the per-row behavior-keyed override. Kept indefinitely. | permanent-local | v2026.6.19 |
| fork-local (no upstream PR) | Homestead-specific expansion of the `skills/github/github-code-review` skill for the kanban PR-review pipeline: the automated/non-interactive (webhook) review context, post-once idempotency guards (head-SHA dedup, blocked-timeout-is-unknown-not-failed, write-payload-to-file-before-POST), the humanizer/de-claude gate, and four reference files (`responding-to-and-resolving-review-threads` — the AUTHOR-resolves-threads loop incl. the Casey-2026-06-24 author-vs-reviewer decision; `webhook-triggered-reviews`; `consolidating-duplicate-reviews`; `editing-a-posted-review`) plus `scripts/commentable_lines.py`. This is the durable git home for material that previously lived ONLY in the `~/.hermes` deploy (silent drift). Deliberate divergence from upstream's generic review skill — homestead/kanban-specific, never sent upstream. **Retire trigger:** never auto-retires (permanent-local); remove only if the homestead PR-review pipeline is retired. | permanent-local | v2026.6.19 |
