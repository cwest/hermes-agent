# PR #46204: active_pr respawn guard honors an explicit unblock

Date: 2026-06-28
Branch: cwest/integration
Base tag: v2026.6.19
Upstream PR: https://github.com/NousResearch/hermes-agent/pull/46204
Upstream issue: https://github.com/NousResearch/hermes-agent/issues/29458

This is a decision record. It explains why this fork carries the mechanism from
upstream PR #46204, exactly what was carried, how it relates to the #46549
review-bypass we already carry, and how it unblocks the author-rework loop.

## TL;DR

- Carried: the `active_pr` respawn-guard "unblock cutoff" mechanism from
  PR #46204 (author `dannyfranca`). A deliberate `unblocked` task event now
  supersedes earlier PR-handoff comments — only PR URLs at/after the latest
  unblock can trigger `active_pr`.
- The bug it fixes for us: the kanban `active_pr` guard wedges an
  author-**rework** card (review bounced back to the implementer to fix the same
  PR) out of spawning for up to 24h, because the card carries a PR-URL comment
  and sits in `status='ready'` — the lane the guard applies to.
- Why this is additive over #46549 (which we already carry): #46549 bypasses the
  guard for `status='review'` only. The author-rework card is in `ready`, not
  `review`, so #46549 explicitly does NOT cover it (documented in that patch's
  own record). This patch covers the `ready` rework case via the unblock cutoff.
- Adapted, not blindly cherry-picked: our base already carries #46549's
  `is_review` wrapper around the `active_pr` block, so the cutoff was applied
  inside that existing `if not is_review:` block rather than as a free-standing
  edit. Same mechanism, same tests, fitted to our line context.
- Auto-retires when #46204 merges upstream and we rebase past it.

## The bug (our live symptom)

`hermes_cli/kanban_db.py :: check_respawn_guard(conn, task_id)` runs in
`dispatch_once` for every ready+assigned task. Guard #4 (`active_pr`) returns a
guard reason when a GitHub PR URL appears in a task comment within
`_RESPAWN_GUARD_PR_WINDOW` (86400s = 24h). It exists to stop a *builder* from
re-running and opening a *duplicate* PR.

In the one-card review lifecycle, when a reviewer (Lamport) returns COMMENTS or
FAIL, the card bounces back to the author (Eckert) to rework the **same** PR. The
card lands in `status='ready'` carrying the PR-URL comment from the original
build handoff. The `active_pr` guard then fires every dispatch tick and refuses
to spawn the author for 24h — the inner rework loop cannot complete
autonomously. Observed live on card `t_2c6bd5f2` (homestead board, PR #68):
repeated `respawn_guarded {'reason': 'active_pr'}` events, card stuck `ready`.

## What we carried

The mechanism from PR #46204: use the latest `unblocked` task event as an
additional lower bound on the PR-comment scan window. Applied inside the
existing `if not is_review:` block of `check_respawn_guard`:

```python
pr_cutoff = now - _RESPAWN_GUARD_PR_WINDOW
latest_unblock = conn.execute(
    "SELECT MAX(created_at) AS ts FROM task_events "
    "WHERE task_id = ? AND kind = 'unblocked'",
    (task_id,),
).fetchone()
if latest_unblock and latest_unblock["ts"] is not None:
    pr_cutoff = max(pr_cutoff, int(latest_unblock["ts"]))
```

Semantics:

- PR URLs in comments posted **before** the latest explicit unblock no longer
  veto the respawn — an unblock is the operator/orchestrator's green light to
  resume work on that same PR.
- PR URLs posted **at or after** the unblock still trigger `active_pr` —
  preserving duplicate-PR protection.
- Same-second comments stay guarded conservatively (kanban timestamps are
  second-granular, so a same-tick PR URL and unblock cannot be ordered).

Plus the matching docstring + `website/docs/user-guide/features/kanban.md`
updates (the respawn-guard prose and the `respawn_guarded` event-reference row),
mirroring the upstream PR's scope.

### How this makes the rework loop work

The `sdlc-review` skill has the reviewer end its run with a clean
`kanban_block(reason="review-changes-requested: …")` (never `reclaim`/`assign`,
which trips the crash breaker). The orchestrator then routes the blocked card to
the cohort author by `unblock` + assign. With this patch, that `unblocked` event
clears the stale PR-URL veto, so the author spawns immediately to fix the same
PR — no 24h wait. The trigger is `block`/`unblock`, both first-class CLI verbs,
so the CLI-only rule is preserved.

## Adaptation note (vs a clean cherry-pick)

Upstream PR #46204 edits a `check_respawn_guard` whose `active_pr` block is NOT
wrapped in an `is_review` guard. Our integration already carries #46549, which
wraps that block in `if not is_review:`. A raw cherry-pick would conflict on
that context, so the cutoff was hand-applied inside our existing
`if not is_review:` block. The logic is byte-equivalent to upstream's; only the
indentation/placement differs to compose with #46549. This is a deliberate,
documented adaptation — when #46204 merges upstream and we rebase, the upstream
version supersedes this and the row retires (see Auto-retire).

## Verification on this fork

- `pytest tests/hermes_cli/test_kanban_db.py -k "latest_unblock or same_second"`
  — 3 passed (the 3 new invariants: ignores-before-unblock, keeps-after-unblock,
  same-second-conservative).
- `pytest tests/hermes_cli/test_kanban_db.py -k "respawn_guard or active_pr or
  review or unblock or dispatch"` — 62 passed (no regression to #46549's
  review-bypass invariants or the dispatch path).
- `pytest tests/hermes_cli/test_kanban_db.py` — 226 passed (full file).
- Proven live: see the session log — after cutover, card `t_2c6bd5f2` bounced
  review→block→unblock→eckert spawned and reworked PR #68 without the 24h wait.

## Auto-retire

This row is `upstream-pending`. When #46204 merges upstream and we rebase onto a
release tag at or above the merge, the upstream change lands in the base; the
hand-applied cutoff is dropped (or applies empty), `scripts/fork_retire_patches.py`
detects the merged PR, and both the code and the PATCHES.md row drop. Nothing
here is meant to live forever — the goal is to land it upstream and shrink the
stack.
