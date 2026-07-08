# Woken kanban-transition turns must carry the origin session's history

## Problem

A kanban-transition wake wakes a turn that authors a report-back into the origin
thread. That woken turn was observed reasoning from STALE card/summary state
rather than the origin session's actual conversation history — so it posted
messages that CONTRADICT ground truth the live session already established with
the human (e.g. telling the human to "restart the gateway" when the gateway was
already running, and "PR #N is already fixed" when its status had been
re-established live). The reply reads exactly like the persona (it IS that
persona's run) but is context-blind.

This spec fixes WHAT THE WOKEN TURN KNOWS — it must load and reason from the
origin session's persisted conversation history. It is the sibling of the
addressing fix (WHERE the wake points); addressing is unchanged here.

## Root cause (verified against running code + gateway.log)

The wake runs inside the origin platform's turn loop (not a separate process).
Both the busy and idle paths funnel the wake into the normal turn flow, which
loads history at `gateway/run.py` `_handle_message_with_agent`:

    session_entry = self.session_store.get_or_create_session(source)   # ~10203
    ...
    history = self.session_store.load_transcript(session_entry.session_id)  # ~10424

`get_or_create_session` resolves the transcript **entirely by the session key
derived from `source`** (`build_session_key`). The transcript that gets loaded
is whatever that key maps to.

The transition-wake payload already carries the authoritative
`origin_session_id` (`gateway/kanban_watchers.py` ← `task.session_id`; stamped
into the body by `build_transition_payload` at
`gateway/kanban_transition_emit.py:285-286`). **But the webhook handler never
reads it.** `WebhookAdapter._build_origin_source`
(`gateway/platforms/webhook.py`) reconstructs the target `SessionSource` purely
from `origin_chat_id` / `origin_thread_id`.

For a Discord thread the LIVE session key is
`agent:main:discord:thread:<thread_id>:<thread_id>` — Discord thread inbound sets
`chat_id == thread_id` (confirmed by `tests/gateway/test_discord_slash_commands.py`
and the origin-sub convention in `tests/gateway/test_wake_origin_thread_routing.py`,
where the correct origin sub is `chat==thread`). When the persisted notify-sub's
`chat_id` is the PARENT channel while `thread_id` is the thread (which happens —
e.g. a Home-channel re-subscribe, or a sub stored with the parent chat), the
coordinate-derived key becomes `thread:<parent_channel>:<thread_id>` — a
DIFFERENT key. `get_or_create_session` then resolves a fresh/empty session and
`load_transcript` returns no live history → the woken turn is context-blind.

### Log proof (2026-07-07)

    21:23:59 origin wake: dispatching ... chat=1515879019269197885 thread=1523994741836873811
    21:23:59 inbound message ... chat=1515879019269197885 msg='[IMPORTANT: ... kanban-transition-orchestrate ...'
    21:24:38 Sending response (1993 chars) to 1515879019269197885

The turn ran on the parent-channel session `1515879019269197885`, NOT the live
thread session `1523994741836873811` where the entire human conversation lives.

## Behavior contract

- **C1 (busy path)** — a wake queued while the origin session is mid-turn
  cascades as a distinct turn on the SAME `session_key`, so it inherits the full
  prior conversation history. (Already true structurally; locked by a regression
  test.)
- **C2 (idle path)** — a wake dispatched to an idle origin session RESUMES that
  session's persisted history keyed on the authoritative `origin_session_id`,
  rather than trusting coordinate-derived key resolution that can diverge. The
  woken turn's context == what the human would see continuing that thread.
- **C3 (no-contradiction invariant)** — given a seeded history that established
  fact X, a woken turn's model input contains those prior messages (so it cannot
  contradict X from stale card/summary state alone).
- **C4 (no regression to addressing)** — delivery still routes to the correct
  origin thread; this changes CONTEXT LOADING only, not routing.

## Design

Two small, surgical changes that REUSE the existing resume path — no parallel
context loader, no synthetic mid-loop user message, no extra cache bust beyond a
normal next turn.

1. **Carry the id through the event.** In `WebhookAdapter._handle_webhook`, when
   the payload carries `origin_session_id`, stamp it onto
   `event.metadata["kanban_origin_session_id"]` (alongside the existing
   `kanban_transition_wake` tag). Pure addition; non-origin webhooks untouched.

2. **Resume the authoritative session before loading history.** In
   `run.py._handle_message_with_agent`, immediately after
   `get_or_create_session(source)` and before `load_transcript`, if the event is
   a transition wake carrying `kanban_origin_session_id` that differs from the
   resolved `session_entry.session_id`, call the EXISTING
   `SessionStore.switch_session(session_key, origin_session_id)` — the same
   `/resume` mechanism already used for the Telegram-topic-binding heal
   (run.py ~10245). It re-points the session_key at the authoritative id so
   `load_transcript` loads the live thread's transcript. When the ids already
   match (the working chat==thread case) `switch_session` is a no-op that returns
   the existing entry, so there is no behavior change for the healthy path.

`switch_session` requires the session_key to exist in `_entries`;
`get_or_create_session` (run just above) guarantees that, so the switch always
applies. Guard the whole block so a missing/blank `origin_session_id`, a
non-wake event, or a `switch_session` returning `None` all fall through to
today's behavior (backward-compatible).

## Why this is the right seam

- The authoritative id (`origin_session_id = task.session_id`) is already
  computed and shipped; we stop discarding it.
- `switch_session` is the canonical resume path — cache-safe, transcript-loading,
  strict-role-alternation-preserving — so the woken turn's context is byte-for-
  byte what a human `/resume` of that session would produce.
- Coordinate-derived addressing (`_build_origin_source`) stays exactly as-is, so
  the proven delivery routing (C4) is untouched.

## Tests (TDD)

- **Unit (webhook):** a wake payload with `origin_session_id` stamps
  `event.metadata["kanban_origin_session_id"]`; absent field → not stamped.
- **Behavior-contract (run.py):** with a seeded persisted session (session_id S,
  transcript establishing fact X) reachable only via `origin_session_id` and a
  DIVERGENT coordinate source, the resolved turn loads S's transcript (C2/C3);
  the model input contains the seeded prior messages. Matching ids → no-op
  (healthy path unchanged). Busy path cascades on the same key with full history
  (C1). Addressing/source coordinates unchanged (C4).
