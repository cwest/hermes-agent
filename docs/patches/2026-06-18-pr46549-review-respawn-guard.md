# PR #46549: review tasks bypass dup-PR respawn guards

Date: 2026-06-18
Branch: cwest/integration
Base tag: v2026.6.5 (commit 3c231eb)
Upstream PR: https://github.com/NousResearch/hermes-agent/pull/46549

This is a decision record. It explains why this fork carries upstream PR #46549,
exactly what was carried (the whole thing, unmodified), and — importantly — the
one place the upstream fix deliberately does *not* reach, why that boundary is
correct, and how we resolved the live card that fell on the far side of it.

## TL;DR

- Carried: PR #46549 whole and unmodified, via `git cherry-pick -x`. Clean
  apply, no hand-port. Upstream author (`demi`) preserved on the commit.
- The bug: the kanban dispatcher's respawn guard wedged **review** tasks out of
  spawning for up to 24h, because two guards meant only for the **build** lane
  also fired on review spawns.
- The boundary: the fix keys its bypass on `status='review'`. A review card
  hand-parked in `status='ready'` is out of scope by design. We resolved our one
  such card by staging it into `review` (where review cards belong), not by
  widening the patch.
- All of this auto-retires when #46549 merges upstream and we rebase past it.

## The bug

`hermes_cli/kanban_db.py :: check_respawn_guard(conn, task_id)` runs in
`dispatch_once` for *every* ready/review task before a spawn. Four guards can
defer a spawn. Two of them exist solely to stop a **builder** from re-running
and opening a **duplicate PR**:

- `recent_success` — a `completed` run exists within the guard window.
- `active_pr` — a GitHub PR URL appears in a recent task comment.

A review pass runs through the same `dispatch_once -> check_respawn_guard` path.
For a card in `status='review'`, the build run that produced the artifact under
review is itself a recent `completed` run **and** it left a PR-URL comment — so
both guards fire and the reviewer never spawns. The review lane silently wedges
for up to `_RESPAWN_GUARD_PR_WINDOW` (24h).

This was the live symptom on our boards: the dispatcher logged
`kanban dispatcher stuck` every five minutes while a review card sat unspawned,
and `respawn_guarded {reason: active_pr}` fired on it every tick.

## What we carried

One upstream commit, cherry-picked unmodified:

- `32502fb01` — `fix(kanban): review tasks bypass dup-PR respawn guards
  (active_pr/recent_success)`

The fix reads `tasks.status` alongside `last_failure_error`, computes
`is_review = status == 'review'`, and skips the `recent_success` and `active_pr`
checks when the task is in review. The `rate_limit_cooldown` and `blocker_auth`
guards still apply — a rate-limited or auth-blocked reviewer should defer like
any other spawn. The PR also adds four invariant tests: review bypasses the
dup-PR guards, a build-lane (`ready`) task in the same shape stays guarded, and
review still honors `blocker_auth`.

The cherry-pick applied cleanly: our base tag (v2026.6.5) already has the
`check_respawn_guard` shape the PR edits, so no hand-port was needed (contrast
PR #44338, which required a hand-port because our base predated a refactor).

Verification on this fork:

- `pytest tests/hermes_cli/test_kanban_db.py -k 'respawn_guard or active_pr or
  review'` — 35 passed (includes the 4 new invariants).
- `pytest tests/hermes_cli/test_kanban_db.py` — 215 passed.

## The boundary we deliberately did NOT cross

The upstream bypass keys on `status='review'`. That is the canonical review-lane
state: a worker finishes a build, opens a PR, and moves the card to `review`,
where the dispatcher spawns a reviewer. Evidence that this is the real lifecycle:
of 24 review-titled cards across all of this homestead's boards, **23 flowed
through `status='review'` and completed** (19 archived, 4 done). The path works,
and #46549 fixes exactly the guard that was wedging it.

Our one live wedge was the outlier. Card `t_219e7f29` on the `hermes-agent`
board (`review: T9 fork-update runbook + hermes-update wrapper (PR #17)`) was
**hand-created by a human directly in `status='ready'`** with assignee `lamport`
and no workflow template. It had zero build runs — just the seed comment carrying
the PR URL. So it tripped `active_pr` on the **ready** spawn path, which #46549's
`status='review'` bypass does not cover.

We chose **not** to widen the patch to also bypass `ready`-lane review cards,
for two reasons:

1. **Footprint / auto-retire.** Modifying the carried commit would make our copy
   diverge from upstream's, breaking the clean cherry-pick provenance and the
   automatic retire when #46549 ships. The fork's governing rule is to bias
   capability to the edges and keep the patch stack tiny and upstream-shaped.
2. **Correctness of convention.** A review card belongs in `status='review'`.
   The 23/24 cards that completed already follow this. Our card was simply
   mis-staged at creation; the right fix is to put it in the state the rest of
   the system uses, not to teach the guard a second, ad-hoc review-detection
   path keyed on title or assignee.

## How the live card was resolved

The card was staged from `ready` into `review` (its correct lane), at which
point the carried #46549 bypass applies and the dispatcher spawns the reviewer
normally. No patch widening; the operational fix matches the convention the rest
of the board already follows.

## Re-evaluation trigger

If hand-parking review cards in `ready` turns out to be a pattern people
genuinely want (not a one-off mis-stage), that is its **own** card and its own
upstream conversation — a `ready`-lane review-detection bypass would need a
robust review-task signal (workflow step / role), not a title heuristic, and
should be proposed upstream rather than carried as fork divergence. Do not
silently widen this patch to cover it.

## Auto-retire

This row is `upstream-pending`. When #46549 merges upstream and we rebase onto a
release tag at or above the merge, the cherry-picked commit applies empty (the
base already contains it), `scripts/fork_retire_patches.py` detects the merged
PR, and both the code and the PATCHES.md row drop. Nothing here is meant to live
forever.
