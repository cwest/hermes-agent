# Thread-origin autonomy: close the loop back to the origin thread + wake Hollis

## Problem (Casey, verbatim intent)

> "Any work originating from a particular thread should always report back and
> do work in the open on that thread. And it's not just me that needs
> information. You do, too. Hollis needs to know when a subagent is done with
> its work in order to proceed."

Today the plumbing exists but is broken end-to-end, so movement through the
system does NOT proactively reach either Casey (in his thread) or Hollis (to
proceed). Casey must run around saying "status / update / check it." That is the
opposite of the value proposition.

## Root-cause findings (ground truth, integration head `8bbf8b8e6`)

Three distinct defects, all real, verified in code + live data:

### F1 — notify-sub profile mismatch (the silent-drop bug)
- The notifier delivers a subscription's ping ONLY when the sub's
  `notifier_profile` equals the running gateway notifier's profile
  (`kanban_watchers.py:241-242`, owner-profile gate).
- BUT subscriptions are stamped with **the profile of whoever CREATED them**
  (`_active_profile_name()` = process-global `get_active_profile_name()`), not
  the profile of the gateway that will DELIVER them.
- The gateway notifier runs as `default`. Subscriptions created by workers /
  CLI under other profiles get stamped `salton`, `avram`, `hollis`, … and are
  **silently dropped**. Live `notify-list` today: 61×salton, 5×default,
  4×hollis, 2×avram — only the 5 `default` ones can ever deliver.
- This is why Lamport's PASS ping never reached Casey, and why thread report-back
  is broken in general.

### F2 — cards do not carry their origin session
- `tasks` has a `session_id` column and `create_task(..., session_id=...)`
  accepts it — but the gateway `/kanban create` path (`slash_commands.py:342-380`)
  never passes it. Every thread-created card has `session_id = None`.
- Without it there is no way to wake "the session that owns this work" when the
  card later transitions.

### F3 — the transition wake targets a throwaway session, not the origin
- The 4c loopback route (`kanban-transition`) delivers to `log` and spins an
  isolated `webhook:kanban-transition:<delivery_id>` agent with NO thread
  context. So even though the wake fires (proven: 202 → run), it neither posts
  into the origin thread nor wakes the origin session. It dies in the log.

## Design (locked with Casey)

One path, no human-ping vs agent-wake split:

**card carries origin (session + thread source) → on ANY terminal transition,
a synthetic message is delivered INTO the origin thread's session → Hollis wakes
there with full context, notices the transition, and either acts or waits for
Casey.**

Casey's answers that fix the design:
1. Every transition wakes Hollis on the origin session; Hollis decides if there's
   anything to do. (notice-everything)
2. When something is waiting for Casey (acceptance/merge gate, genuine fork),
   Hollis waits — never acts past those gates.
3. All card pings route to Hollis on the session (single wire).
4. Wake = **message into the thread** (cache-safe; also what Casey sees in the
   open). Reuse the existing `notify_on_complete`-style synthetic-message
   injection; NEVER interrupt/rebuild a live session mid-turn.
5. Non-thread-origin work (cron/webhook/direct) → default channel
   `1515879019269197885`, unless the cron/hook explicitly specifies elsewhere.
6. Hollis owns noise control (collapse/dedupe before anything reaches Casey).
7. Full dev-workflow: TDD, PR → cwest/integration, Casey merges.

## Scope — three fixes, one PR (they are one feature)

### Fix 1 (F1): stamp subs with the NOTIFIER's profile, not the creator's
The subscription must record the profile of the gateway that will deliver it.
- In the gateway auto-subscribe path, stamp `notifier_profile` from the running
  gateway's notifier profile (`self._kanban_notifier_profile`), which is the
  same value the notifier gates on — guaranteeing match by construction.
- Broader: the owner-profile gate exists to stop a multi-gateway fan-out from
  double-delivering. The correct invariant is "a sub is owned by the gateway
  that will deliver it." A sub created under a worker profile but intended for
  the `default` gateway must be stamped `default`. Fix at the create site(s):
  the gateway slash path and any orchestrator/skill subscribe helper default to
  the delivering gateway's profile, not `get_active_profile_name()`.
- Reconcile existing mis-stamped live subs (data migration / one-shot re-stamp)
  is an OPS step, not code — handled at deploy, out of PR scope.

### Fix 2 (F2): stamp origin session_id on thread-created cards
- `slash_commands.py` `/kanban create`: pass `session_id` = the origin session
  key (derived from `event.source`: platform+chat+thread → the session id the
  gateway uses for that thread) into the create call.
- Also auto-subscribe the origin thread (already happens) — keep, but with the
  corrected profile from Fix 1.

### Fix 3 (F3): transition wake delivers INTO the origin session/thread
- The transition emitter/route already has task_id+board+kind. On wake,
  resolve the card's `session_id` (+ its origin thread source from the sub) and
  deliver the synthetic "card X transitioned" message INTO that session/thread,
  not a throwaway webhook session.
- If the card has no origin session (cron/webhook/direct origin), fall back to
  the default channel `1515879019269197885` (Casey's #5), unless the route/cron
  explicitly set a target.
- Delivery uses the existing notifier chat-ping path (message into thread) — the
  notifier ALREADY delivers terminal events to the subscribed thread; once Fix 1
  makes the sub deliverable and Fix 2/here ensure the thread is the origin, the
  human-facing half is done. The Hollis-wake half is that same message landing
  in Hollis's session so his next turn processes it.

## Cache / alternation safety (AGENTS.md hard constraints)
- NEVER inject a synthetic user message mid-loop into a live session. The wake
  is a normal inbound message on an IDLE session (exactly how `notify_on_complete`
  and the existing notifier chat-ping already behave) — the next turn consumes
  it, prefix cache and role alternation preserved.
- No new core model tool. No new HERMES_* env var (behavior stays in config.yaml
  / existing route config).

## TDD plan (RED → GREEN per fix)
1. **F1 test**: a sub created via the gateway auto-subscribe path is stamped with
   the notifier's profile, so the notifier's owner-profile gate passes (delivers)
   — assert stamped profile == notifier profile, and that a mismatched-creator
   context still yields a deliverable sub.
2. **F2 test**: `/kanban create` from a thread source persists `session_id` on
   the card (origin session), and `None` when created without a session context.
3. **F3 test**: a terminal transition for a card with an origin session resolves
   that session/thread as the delivery target (not `webhook:kanban-transition:*`);
   with no origin session it falls back to the default channel.
4. **E2E**: create card from thread → drive to `completed` → assert the notifier
   delivery target is the origin thread AND the transition wake targets the origin
   session. (Real imports, temp HERMES_HOME, no mock of the resolution chain.)

## Definition of done (Casey's acceptance test)
Create a card FROM a specific thread, dispatch a real subagent, let it finish,
and — with Casey doing nothing — (1) a report lands in THAT thread, and (2)
Hollis wakes in that thread and takes the next step. "I watch it happen in a
thread, untouched."

## Out of PR scope (ops, at deploy)
- Re-stamp existing mis-owned live subscriptions to `default`.
- Point the `kanban-transition` route's default fallback at `1515879019269197885`.
- Restart to load the merged code (restart-gated).
