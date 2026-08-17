# Reconciling `cwest/integration` with upstream `main`

The running Hermes install (`~/.hermes/hermes-agent`) runs the fork integration
branch `cwest/integration`, which carries ~110 fork-only commits on top of an
older `origin/main`. Merged upstream work does not reach the running gateway
until the branch is reconciled. This is the mechanical procedure for doing that
without a history rewrite, so it does not have to be re-derived each release.

## Chosen strategy: MERGE, not rebase

We reconcile with `git merge origin/main` (a merge commit), NOT `git rebase`.

Trade-off, stated plainly:

- **Merge (chosen).** One merge commit; no hash rewrite; every attached
  worktree keeps its merge-base; no force-push of a protected branch. The cost
  is a non-linear history on the integration branch — acceptable, because
  `cwest/integration` is an integration branch, not a published-release lineage.
- **Rebase (rejected here).** Produces a linear history but rewrites every hash
  on `cwest/integration`. With ~12 attached topic worktrees whose branches
  descend from the integration tip, that orphans every merge-base and forces a
  force-push of a protected branch. High blast radius for a routine catch-up.
  Reserve rebasing for a deliberate, low-worktree-count history cleanup (see
  the `fork-rebase-tdd` skill), not the per-release reconcile.
- **Upstream the fork commits as PRs (the long game).** The real fix for
  recurring divergence is to shrink the fork: land fork-only commits upstream so
  the delta trends to zero. That is a separate, ongoing effort; it does not
  block a given release's reconcile.

## The mechanical steps

Run these in an ISOLATED worktree, never in `~/.hermes/hermes-agent` directly —
the live install carries stashes and attached worktrees, and a wedged index
there breaks the gateway.

```bash
# 0. Snapshot what must survive (verify identical after).
git -C ~/.hermes/hermes-agent stash list      # record entries
git -C ~/.hermes/hermes-agent worktree list   # record entries

# 1. From a worktree whose branch == cwest/integration tip:
git fetch origin
git merge --no-commit --no-ff origin/main

# 2. Resolve conflicts. The ONLY recurring conflict zone is
#    hermes_cli/kanban_db.py (fork-only kanban helpers vs. upstream kanban
#    work) plus its test files. See "Conflict recipes" below.

# 3. Commit the merge (signed).
git commit -S -F <merge-message-file>

# 4. If upstream re-introduces a commit the fork deliberately reverted,
#    RE-APPLY the fork's revert on top of the merge (see "Deliberate reverts").

# 5. Verify (see "Verification gate").

# 6. Push the branch and open a DRAFT PR against the fork. Casey merges + the
#    gateway RESTART (required for the new code to load) is Casey's, not ours.
```

## Conflict recipes (kanban_db.py)

Conflicts here are almost always **disjoint additive** blocks — fork-only
helpers landing next to new upstream helpers at the same insertion point. The
correct resolution is a **union merge**: keep BOTH sides. Two traps:

1. **Duplicate definitions.** Upstream sometimes adds a symbol the fork already
   defines (e.g. `_OWNER_MAP_RE`). Keep the fork's single definition; DROP the
   incoming duplicate. Verify the two are byte-identical before dropping.
2. **Naive vs. sophisticated readers.** Upstream may add a simple version of a
   reader the fork already implements richly (e.g. upstream's
   `resolve_review_owner` did a naive first-match scan; the fork already has
   `_owner_from_owner_map` that distinguishes an INTENTIONAL submit stamp from
   the chokepoint's `kind_source=defaulted` auto-stamp). Keep the upstream API
   surface the new code depends on, but back it with the fork's reader —
   compose intents, don't ship the naive duplicate. Delete the orphaned naive
   helper so no dead code lands.

Test-file collisions have their own trap: git auto-merges two same-named test
HELPERS into one file with no textual conflict (they live in different regions),
then Python keeps only the last `def`, so the other side's callers fail with a
`TypeError` on signature mismatch. Grep the merged test file for duplicate
`def <helper>` and rename one.

## Deliberate reverts (the #114 class)

If the integration tip is (or contains) a signed revert of a commit that is an
ANCESTOR of `origin/main`, a plain merge will silently resurrect that reverted
code — git's 3-way merge has no notion of "this was deliberately reverted." That
is exactly the antipattern to avoid. After the merge, RE-APPLY the fork's revert
as a separate commit:

```bash
git revert --no-commit <upstream-sha-of-the-reverted-change>
git commit -S -F <revert-message-file>
# Verify the reverted files are tree-identical to the fork's prior revert.
```

`git rev-list --count HEAD..origin/main == 0` still holds (the merge makes all
of origin/main reachable; a subsequent revert does not change reachability).

## Verification gate

```bash
grep -c "def submit_for_review" hermes_cli/kanban_db.py     # >= 1
git rev-list --count HEAD..origin/main                       # == 0
./scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py \
    tests/hermes_cli/test_kanban_cli.py \
    tests/tools/test_kanban_tools.py -q                      # green
git stash list && git worktree list                         # identical to snapshot
```

Then confirm the deliberately-reverted code stayed out (e.g. the #114 marker
`grep -c response_full gateway/hooks.py` returns 0).

## The gateway restart

The reconciled `.py` modules load at gateway startup and do NOT hot-reload, so a
running gateway keeps executing the PRE-merge code until Casey restarts it. State
this as the headline of any handoff. Do not mark the work item done on merge
alone — it is done when Casey merges the PR and restarts the gateway.
