# Spike: one-card work-item lifecycle that MOVES through lanes (inner + outer loop)

Status: RECOMMENDATION (do not build from this card — Casey decides go/no-go).
Author: Eckert (implementer). Date: 2026-06-27.
Kanban card: t_09ad154f (homestead board).
Related spike: t_7744f17d (webhook-ROUTE consolidation — shares the repo→board
resolver; keep separate).

---

## Bottom line

The redesign is **smaller than the card assumes, because core already started
it.** The board is *not* a flat status model: `review` is already a first-class
lane in core — `VALID_STATUSES` includes it (kanban_db.py:100), the dispatcher
has a dedicated review-column dispatch loop (kanban_db.py:6532–6580), there is a
purpose-built `claim_review_task` (`review → running`, kanban_db.py:3179), the
respawn guard has explicit `is_review` carve-outs (kanban_db.py:6091, 6138,
6150), and there is review-lane health telemetry (`has_spawnable_review`,
kanban_db.py:6194). Casey's model needs the *same pattern extended* to two more
lanes (`doing`, `merge`) plus one transition primitive — not a greenfield
schema.

Recommended choices:

- **(A) Lane mechanism:** **Option 1 (first-class lanes), done as an EXTENSION of
  the existing `review` lane, not a new column system.** Add `doing` and `merge`
  as sibling statuses to `review`; add a `move` transition; teach the dispatcher
  the two new lanes by cloning the review-column loop. This is the proper model
  Casey wants and it is *cheaper* than Option 2 because half of it already
  exists and works in production.
- **(B) PR↔card link:** **Store the canonical PR URL in a dedicated `pr_url`
  column on the card, written at PR-open, looked up by the existing
  `_canonical_pr_url` matcher.** The link mechanism is ~80% built already (the
  regex, the canonicalizer, the title/body/run-summary search surface). Promote
  it from "scan three text fields" to "one indexed column" so a webhook resolves
  PR→card in one query instead of a full-board scan.
- **(C) Webhooks:** rewrite all three from CREATE to **MOVE** the linked card.
  `stage-pr-review` → move card `doing→review` (lamport); `bounce-review-to-author`
  → move card `review→doing` (eckert); `close-pr-card` → move card `→done`
  (merged) / `→archived` (closed-unmerged). Casey's **outer loop** (merge→doing)
  is a single `kanban move <id> --lane doing --assignee eckert` he runs, OR a
  dashboard lane-drag that calls the same `move`.
- **(D) Board routing:** resolve the board from the repo at PR-open and write the
  card on its OWN board; kill the hardcoded-homestead fallback for *new* work.
  Reuse the repo→board resolver the close/bounce skills already implement.
- **(E) Migration:** **drain, don't migrate.** In-flight 3-card chains finish
  under the old code; new PRs use the one-card flow. One core change in
  `hermes-agent` (kanban_db.py + a thin CLI verb) + a rewrite of the three
  homestead skills + a doc pass on two orchestration skills. No data migration,
  no backfill.

Recommended implement sequence (smallest blast radius first): B → A → C → D → E.

---

## What I actually read (evidence)

- `hermes_cli/kanban_db.py` (canonical repo `/Users/caseywest/src/hermes-agent`,
  7944 lines): schema, statuses, `create_task`, `claim_review_task`,
  `dispatch_once` review-column loop, `check_respawn_guard`,
  `has_spawnable_ready` / `has_spawnable_review`, `_PR_URL_RE` / `_canonical_pr_url`
  / `_review_pr_url`.
- `gateway/kanban_watchers.py`: dispatcher + notifier watchers (per-board tick,
  `has_spawnable_review` health telemetry).
- Skills `stage-pr-review`, `bounce-review-to-author`, `close-pr-card`
  (`/Users/caseywest/src/hermes-homestead/skills/`, deployed to
  `~/.hermes/skills/homestead/`).
- `board.json` for `homestead` (`default_workdir`
  `/Users/caseywest/src/hermes-homestead`) and `hermes-agent`
  (`default_workdir /Users/caseywest/.hermes/hermes-agent`), plus the 12 boards
  on disk.

### The premise correction that changes the whole estimate

The card states: *"No lane/column concept in the schema. The board is a flat
STATUS model: `tasks.status ∈ {ready, running, blocked, done, archived}`."*

That is **not current.** The live `VALID_STATUSES` is:

```python
# kanban_db.py:100
VALID_STATUSES = {"triage", "todo", "scheduled", "ready", "running",
                  "blocked", "review", "done", "archived"}
```

`review` is already a working lane with end-to-end dispatcher support. The
"system FAKES lanes by spawning a differently-titled card" symptom is real, but
its root cause is narrower than "no lane concept exists": it is that **the
`review` lane is the ONLY non-terminal work lane, and the webhooks CREATE into
it instead of MOVING into it.** There is no `doing` lane and no `merge` lane, and
there is no `move` verb, so the skills synthesize lanes by spawning new cards
with new assignees. Add the two missing lanes + a move, and the spawning
disappears.

---

## A. The lane mechanism — RECOMMEND Option 1 (extend the existing review lane)

### Why Option 1 over Option 2

Option 2 (overload `assignee`+status as the de-facto lane) was the *original*
instinct and it is exactly what produced the current mess. The skills already
try to express lanes through assignee — and the result is 600+ lines of
workaround scar tissue across three SKILL.md files: the create-then-transition
fallback, the `ready`-card-with-PR-URL wedge, the archive-not-done dance, the
SHA-stamped idempotency keys, the "NEVER hand-file a CLI card" warnings. Every
one of those is a symptom of *lanes-by-convention* with no first-class lane to
move a card into. Doubling down on Option 2 deepens the hole.

Option 1 is also **cheaper than the card fears**, because `review` proves the
pattern and the cost is mostly cloning it twice:

| Piece | `review` today | `doing` / `merge` to add |
|---|---|---|
| Status enum | present (line 100) | add 2 strings |
| Dispatch loop | review-column loop (6532–6580) | clone for the lanes the dispatcher must spawn into |
| Claim fn | `claim_review_task` (3179) | generalize to `claim_lane_task(lane)` |
| Respawn-guard carve-out | `is_review` skips (6091,6138,6150) | generalize the carve-out to "any work lane that received a PR" |
| Health telemetry | `has_spawnable_review` (6194) | generalize to `has_spawnable_lane(lane)` |

### What `doing` and `merge` actually mean for dispatch eligibility

This is the load-bearing design decision, because "what is dispatchable" changes:

- **`doing` (eckert).** A card a worker should pick up and implement/rework.
  Today this is `status='ready'` + assignee. **Recommendation: `doing` is NOT a
  new dispatch lane — map it onto the existing `ready→running` path.** When a
  card enters the `doing` lane it should be `ready` (claimable by `claim_task`,
  which already does parent-gating). Rationale: the implementer lane needs the
  full `ready` machinery (parent gates, the normal respawn guards *minus* the
  PR-dup guard during rework). Introducing a parallel `doing` dispatch loop would
  duplicate `claim_task`'s parent-dependency logic for no benefit. So **`doing`
  is a *display* lane backed by `ready`/`running`**, and the rework respawn-guard
  fix (below) is what makes the inner loop safe.

- **`review` (lamport).** Already exists. No change to its dispatch.

- **`merge` (casey).** A card waiting for a human to merge. **Recommendation:
  `merge` is a NON-dispatchable lane** — no worker is spawned for it. It is a
  parking lane that the notifier surfaces to Casey and that Casey acts on via
  `move` (merge→done by merging, or merge→doing for the outer loop). The
  dispatcher must explicitly *exclude* `merge` from every spawn loop, the same
  way `blocked` is excluded today. This is the cleanest expression of "merge is
  Casey's lane, always" — the system literally never spawns into it.

So the eligibility map becomes:

```
todo      → parent-gated, not dispatched (promotes to ready)
ready     → dispatched via claim_task (this backs the "doing" lane on entry)
running   → in-flight (any lane's worker is running)
review    → dispatched via claim_review_task (lamport)   [exists today]
merge     → NEVER dispatched (human lane; notifier surfaces to casey)  [NEW]
blocked   → never dispatched (stuck)                       [exists today]
done/archived → terminal
```

The subtle part: a single card moving doing→review→merge→done passes through
`ready`/`running` (doing), `review`/`running` (review), `merge` (parked),
`done`. The lane label is `status` plus a small derivation for display. The
inner loop (review→doing) sets the card back to `ready` and reassigns eckert;
the outer loop (merge→doing) does the same from the parked `merge` lane.

### The respawn-guard fix that makes the inner loop safe (critical)

Today the inner loop can't run on one card because of the `active_pr` /
`recent_success` respawn guards (kanban_db.py:6063, 6055). When a card is moved
back to `doing` (=`ready`) for rework, that card already has (a) a completed
build run and (b) a PR-URL comment — so `check_respawn_guard` returns
`recent_success` then `active_pr` and the rework worker never spawns. This is
*exactly* the wedge the skills warn about ("a `ready`-lane card whose body
carries a GitHub PR URL trips the `active_pr` respawn guard every tick and never
spawns").

The current carve-out only skips these guards when `status == 'review'`
(`is_review`, line 6091). **Recommendation: widen the carve-out from "is the card
in review?" to "is this card already PR-linked?"** Concretely: when the card has a
`pr_url` (the new column from section B) AND it is being dispatched into the
implement/rework path, skip `recent_success` and `active_pr` — the PR already
exists, so "don't spawn a duplicate PR" is the wrong instinct; we *want* the
worker to push more commits to the same branch. The guards still protect the
*first* implement pass (no `pr_url` yet → guards active → no accidental second
PR). This single change is what converts review↔doing from "spawn a new rework
card" into "move the same card back and respawn the worker on it."

### Migration cost / what breaks (Option 1)

- **Schema:** `VALID_STATUSES` gains `doing`?, `merge`. (Note: I recommend
  `doing` be backed by `ready`/`running` for dispatch, so the *new* enum value
  that must be dispatchable-excluded is `merge`; `doing` may be purely a display
  derivation — see the data-model note in the open questions.)
- **CLI:** new `kanban move <id> --lane <lane> [--assignee <p>]` verb (thin
  wrapper over a new `move_task` in kanban_db.py that runs inside `write_txn` and
  emits a `status_changed`/`moved` event — same invariant-preserving path the
  fallback scripts already hand-roll).
- **board.json:** optionally add a `columns`/`lanes` array for dashboard ordering
  (`todo, doing, review, merge, done`). Not required for correctness — the
  dashboard can derive columns from status — but it lets Casey reorder/label.
- **Breaks:** nothing in the data model (additive enum). The risk surface is the
  dispatcher: the `merge` exclusion and the widened respawn-guard carve-out must
  be exact or you either spawn into Casey's lane or re-wedge the inner loop.
  Both are unit-testable against a temp `HERMES_HOME` (the existing
  `test_kanban_db.py` already has `claim_review_task` and respawn-guard tests to
  clone).

Blast radius: **kanban_db.py (core) + one CLI verb.** Medium. Well-bounded.

---

## B. PR↔card linking — RECOMMEND a dedicated `pr_url` column

### What exists already

The link is ~80% built. Core already has:

- `_PR_URL_RE` (kanban_db.py:116) — canonical owner/repo/number capture.
- `_canonical_pr_url(text)` (2078) — normalizes spellings to one identity.
- `_review_pr_url(skills, title, body)` (2096) + the create_task PR-URL dedup
  guard (2242–2269) — already resolves "is there a card for this PR?" for review
  cards.
- The `close-pr-card` skill documents the real search surface: PR URL can live in
  **title, body, result, OR the latest `task_runs.summary`**, plus a repo-scoped
  bare `PR #N` fallback.

### The gap and the fix

The webhooks need to resolve PR→card *to move it*, and they currently do it by
scanning multiple text fields across every non-archived card on every board —
slow, and fragile (the bare-`PR #N` fallback can mismatch). **Recommendation:
add a `pr_url TEXT` column to `tasks`, indexed, written at PR-open.**

- **Creation of the link:** the implement worker already reports
  `review-required: PR #N` / pushes the PR. At PR-open the `stage-pr-review`
  webhook (now a MOVE, section C) writes the canonical PR URL into the card's
  `pr_url` column as part of the move. The worker's own completion handoff can
  also stamp it. Either way the canonical URL is set once, on the originating
  card.
- **Lookup:** every subsequent `pull_request` / `pull_request_review` /
  `pull_request closed` event canonicalizes its PR URL via the existing
  `_canonical_pr_url` and does `SELECT id, status, assignee FROM tasks WHERE
  pr_url = ? AND status NOT IN ('done','archived')` — one indexed query,
  board-scoped, no text scanning, no bare-number guessing.
- **Back-compat:** keep the title/body/run-summary scan as a *fallback* for
  drained old-model cards that predate the column (they never get `pr_url` set).
  New cards use the column.

This also retires the most fragile workaround in `close-pr-card` (the
repo-scoped bare-`PR #N` matcher) for all new work.

Blast radius: one additive column + index + a tiny setter. **Small.** This is
why it leads the implement sequence — everything else keys on the link.

---

## C. Rewrite the three webhooks from CREATE to MOVE

All three currently call `create_task` (directly or via the create-then-transition
fallback). After A+B they call `move_task` on the card resolved by `pr_url`.

### `stage-pr-review` (PR opened/synchronize)

- **Now:** creates `review: PR #N <repo>` card, assignee lamport, into the
  `review` lane (with a 250-line fallback for when the tool can't set
  `initial_status='review'`).
- **Becomes:** resolve the originating card by `pr_url` (set it if the card
  doesn't have it yet), then `move_task(card, lane='review', assignee='lamport')`
  **on the card's own board**. No new card. The card carries its full history
  (the implement run, the diff, the design link) into review — that *is* the
  audit trail Casey wants.
- **Deletes:** the create-then-transition fallback, the duplicate-review-card
  guard, the "NEVER hand-file a CLI card" wedge section, the archive-vs-done
  re-review dance (re-review is just review→doing→review on the same card now),
  the SHA-stamped re-review key. All of it is workaround for not having a move.
- **First-PR-open edge:** if no card resolves (PR opened with no originating
  card — e.g. a human-opened PR), fall back to creating one on the repo's board
  in the `review` lane. This is the *only* legitimate create path left, and it's
  rare.

### `bounce-review-to-author` (pull_request_review submitted)

- **Now:** on changes_requested OR an open thread, files a THREE-card chain
  (rework→re-review→merge) with parent gates and SHA-stamped keys.
- **Becomes:** `should_bounce` logic is unchanged (verdict==changes_requested OR
  any open thread). On a bounce: resolve the card by `pr_url`, then
  `move_task(card, lane='doing', assignee='eckert')`. **One move, no chain.** The
  same card oscillates review↔doing until lamport is satisfied — that is the
  inner loop, expressed directly. The re-review on the next push is
  `stage-pr-review` moving the same card doing→review again.
- **Deletes:** the entire three-card chain, parent-gating, the stale-merge-lane
  reconciliation, the SHA-stamped key. The merge lane is now a *lane the card
  moves into on PASS*, not a separate card to file.
- **No-op case unchanged:** approved + zero open threads → on PASS, move the card
  to the `merge` lane (assignee casey) instead of promoting a separate merge
  card.

### `close-pr-card` (pull_request closed)

- **Now:** finds card(s) by the multi-field scan; archives review cards, completes
  impl/merge cards. Card-type-aware because there are 1–3 cards per PR.
- **Becomes:** resolve THE one card by `pr_url`. Merged → `move_task(card,
  lane='done')`. Closed-unmerged → `move_task(card, lane='archived')` (or
  `doing` if policy says reopen — recommend `archived`; a human reopens via
  `move` if needed). **One card, one move.** No card-type branching (there is
  only one card), no archive-vs-done subtlety (that subtlety existed *only*
  because separate review cards poisoned the dedup — which is gone with one card).
- **Deletes:** the review-card-archive special case, the bare-`PR #N` repo-scoped
  fallback (replaced by the `pr_url` column), the zombie-card cleanup framing
  (one card can't zombie if every webhook moves it).

### Casey's OUTER loop (merge → doing)

**Recommendation: a CLI move Casey runs, mirrored by a dashboard lane-drag.**
`kanban move <card> --lane doing --assignee eckert`. Both the CLI verb and the
dashboard drag call the same `move_task`. This is the override Casey wants after
the card reaches his merge lane: he can send it back to eckert without re-filing
anything. The notifier already surfaces `merge`-lane cards to him (section E), so
the loop is: notifier pings Casey → Casey merges (move→done) OR bounces
(move→doing).

---

## D. Board routing — resolve from repo, kill hardcoded homestead

### The rule

Every transition stays on the work item's **own** project board. The board is
resolved from the repo:

- Prefer a board whose **slug** == repo name (e.g. PR in `cwest/hermes-agent` →
  `hermes-agent` board).
- Else a board whose **`default_workdir` basename** == repo name.
- The `bounce-review-to-author` and `close-pr-card` skills already implement this
  `resolve_board(repo)` — promote it to **one shared resolver** (a helper in
  kanban_db.py or a small shared module the skills import) so all three webhooks
  and any future watcher use identical logic. This is the resolver that overlaps
  with spike t_7744f17d; **build it once, owned by core, consumed by both.**

### Kill the hardcoded homestead fallback (for new work)

`stage-pr-review` today hardcodes every review card onto `homestead` "where
Lamport's review lane runs." That is the section-3 root cause — a `hermes-config`
PR's review evaporates onto a foreign board. With one card that *lives on its
repo's board from creation*, the review never moves boards at all — it's already
on the right board. The homestead fallback should remain ONLY for the genuine
no-board-for-this-repo case (and even then, prefer auto-creating/selecting the
repo's board over dumping on homestead — but that's a section-D follow-up, not
load-bearing for the core change).

Note: a card cannot move *across* boards in SQLite (each board is a separate DB,
kanban_db.py module docstring). This is *fine and desirable* under one-card:
because the card is born on its repo's board and never needs to cross, the
"reviews land on a foreign board" bug is structurally impossible once creation is
repo-routed. The only thing to verify is that PR-open creates/resolves on the
repo board, never homestead-by-default.

Blast radius: shared resolver (small) + creation-routing in `stage-pr-review`.
**Small–medium.** Coordinate the resolver with t_7744f17d.

---

## E. Migration + impact

### Migration: DRAIN, don't migrate

There is **no data migration.** In-flight 3-card chains finish under the existing
skills (they still work — the old code paths aren't deleted until their chains
drain). New PRs opened after deploy use the one-card flow. Within one review
cycle (days) the old chains are terminal and only one-card flows remain. Cutover
is a deploy + gateway restart (skills load at startup, no hot-reload), not a
backfill script. This is the lowest-risk migration and it needs no downtime.

If Casey wants a clean board sooner, a one-time sweep can archive the now-orphaned
extra cards (the separate `review:`/`merge:` cards whose work moved to the parent
card) — but that's optional cleanup, not migration.

### Impact on each named consumer

- **Dispatcher (`dispatch_once`, kanban_db.py:6219):** the real change surface.
  Add `merge`-lane exclusion from every spawn loop; widen the respawn-guard
  carve-out from `is_review` to `is_pr_linked`; if `doing` is backed by `ready`,
  no new dispatch loop is needed (recommended). Clone the review-column telemetry
  for any genuinely new dispatch lane. Unit-test against temp `HERMES_HOME`.
- **Notifier / merge-lane surfacing (`gateway/kanban_watchers.py`):** there is
  **no `merge_lane_notify` today** — the card body confirmed; merge handling is
  implicit (a `merge:` card assigned to casey that the report-back watcher
  surfaces as "needs Casey"). Under one-card, the notifier should surface a card
  *in the `merge` lane* (not a separate merge card). This is a small change to
  the watcher's "what counts as needs-human" predicate: add `status='merge'`
  alongside `blocked`. The `kanban_notify_subs` machinery is unchanged.
- **`team-status-watch`:** not found as code in either repo (likely a cron/skill
  that reads board state). Its queries that count `review`/`merge` as separate
  cards must be updated to count *lanes of one card*. Low effort, but enumerate
  it before cutover so the status view doesn't double-count during the drain.
- **`orchestrating-dispatch-pipelines` / `homestead-dispatch-golden-path`
  skills:** these document the 3-card triad (implement+merge cards, parent the
  merge to the webhook review card, etc.). They need a **doc rewrite**: the
  orchestrator now files ONE card on the repo board and the webhooks move it; no
  triad, no manual review/merge cards, no parent-gating chain. This is the
  largest *documentation* delta but zero *code*.

### Where each piece lands (repo ownership)

| Piece | Repo | Kind |
|---|---|---|
| `pr_url` column + index + setter | `hermes-agent` | core (kanban_db.py) |
| `doing`/`merge` statuses + `move_task` + `kanban move` CLI | `hermes-agent` | core (kanban_db.py + cli) |
| dispatcher: merge-exclusion + widened respawn carve-out | `hermes-agent` | core (kanban_db.py) |
| shared `resolve_board(repo)` | `hermes-agent` | core (shared by webhooks + t_7744f17d) |
| notifier: surface `merge` lane as needs-human | `hermes-agent` | core (gateway/kanban_watchers.py) |
| 3 webhook skills CREATE→MOVE rewrite | `hermes-homestead` | skills |
| orchestration skills doc rewrite | `hermes-homestead` | skills (docs) |
| board.json `columns` (optional) | per-board (data) | config |

So: **one core change in `hermes-agent` + a skills rewrite in
`hermes-homestead`.** The card's hypothesis ("one core change to kanban +
gateway/kanban_watchers + the homestead skills") is confirmed.

---

## Risk / complexity / blast-radius summary

| Item | Complexity | Risk | Blast radius |
|---|---|---|---|
| B. `pr_url` column + lookup | Low | Low | 1 additive column |
| A. `doing`/`merge` + `move` + dispatcher | Medium | Medium (dispatcher correctness) | core kanban + CLI |
| A. widened respawn-guard carve-out | Low | **High if wrong** (re-wedges inner loop or spawns dup PR) | one function, heavily tested |
| C. webhook CREATE→MOVE (×3) | Medium | Medium (deletes lots of workaround) | 3 skills |
| D. shared repo→board resolver | Low | Low (coordinate w/ t_7744f17d) | shared helper |
| E. drain migration | Low | Low | none (no data change) |
| E. notifier merge-lane surfacing | Low | Low | one predicate |
| E. orchestration skills doc rewrite | Low (docs) | Low | 2 skills |

The single highest-risk line is the respawn-guard carve-out — get it wrong and
the inner loop either never spawns (re-wedge) or opens duplicate PRs. It is also
the single most valuable change, because it is what makes one card oscillate
review↔doing at all. It must ship with explicit tests:
`test_rework_card_with_pr_url_respawns` and
`test_first_implement_pass_without_pr_url_still_guarded`.

---

## Proposed implement sequence

1. **B — `pr_url` column + indexed lookup + setter.** Additive, safe, everything
   keys on it. Land first so later pieces have the link to move by.
2. **A — `doing`/`merge` statuses, `move_task`, `kanban move` CLI, dispatcher
   merge-exclusion + widened respawn carve-out.** The lane engine. Ship with the
   two respawn-guard tests + a `merge`-lane-never-dispatched test.
3. **C — rewrite the three webhooks to MOVE.** Now there is a link (B) and a move
   (A) to call. Delete the workaround scar tissue as each skill is rewritten.
4. **D — shared `resolve_board(repo)` + repo-routed creation.** Coordinate the
   resolver with t_7744f17d; kill the homestead default for new work.
5. **E — notifier merge-lane surfacing + orchestration skills doc rewrite +
   announce drain cutover.** Old chains drain naturally.

Each step is independently shippable and reversible; B and A together already
make Casey's inner+outer loop possible even before the skills are rewritten
(Casey could drive the lanes by hand with `kanban move`).

---

## Open questions for Casey (decide before implement)

1. **Is `doing` a stored status or a display derivation of `ready`/`running`?**
   I recommend display-derivation (back it with `ready`/`running` so `claim_task`
   parent-gating is reused). If you want `doing` to be a literal stored status,
   the dispatcher needs a third claim path — more code, no clear benefit. Your
   call on data-model purity vs. minimal core.
2. **Closed-unmerged policy:** archive the card (recommended) or move it back to
   `doing`? Archive is cleaner; reopening is a one-line `kanban move`.
3. **No-board-for-repo fallback:** keep the homestead catch-all, or auto-select/
   create the repo's board? I lean auto-select with homestead as last resort.
4. **board.json `columns`:** add now for explicit dashboard ordering, or derive
   columns from status and defer? Not load-bearing either way.
