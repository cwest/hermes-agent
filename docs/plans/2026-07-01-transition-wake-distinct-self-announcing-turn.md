# Transition wake: a distinct turn even when the origin session is busy

Card: t_376048fd. Base: cwest/integration @ a2705180d (#28).

SCOPE: this card is ONLY the distinct-turn delivery (the busy-session swallow).
The self-announcing wake banner is a sibling card (t_dfafdefd); latency tightening
is another (t_940f9205). Neither is implemented here.

## Problem — busy-session swallow (root-caused from source)

The event-driven autonomy loop already fires and routes: a lane MOVE emits a
transition, the `kanban-transition` webhook accepts it (202), origin-routing (#26)
targets the ORIGIN session, and `_build_origin_source` (webhook.py) shapes the
inbound so it lands in the origin thread's own session.

The remaining gap: the wake POST becomes a plain TEXT `MessageEvent` and is
dispatched via the adapter's `handle_message` (base.py). When the origin session
is mid-turn (`session_key in self._active_sessions`), the event falls through to
the busy branch (base.py ~4733-4770) and is either:

- text-debounced (`_queue_text_debounce`), or
- merged into any existing pending TEXT via
  `merge_pending_message_event(..., merge_text=True)`.

Both destroy the wake's identity: it is silently folded into an unrelated
in-flight/queued turn instead of producing its own identifiable turn. Live
2026-07-01: the wake hit a session already running a turn → no distinct wake
turn; Casey had to paste the notice manually.

## Design — tag the wake; never merge/debounce it; drain it as its own turn

The busy path uses a **single-slot** `_pending_messages[session_key]`, drained as
a distinct `_process_message_background` turn by the existing in-band cascade
(base.py ~5164). The fix keeps the wake in that slot but forbids it from ever
losing its identity:

1. **Tag** the wake `MessageEvent` on the webhook side:
   `event.metadata["kanban_transition_wake"] = True`, set only on the
   origin-routed transition path (detected via the emitter-stamped `origin_*`
   fields — the same discriminator `_build_origin_source` uses, so ordinary
   webhook routes like `github-prs` are never tagged). A pure predicate
   `is_transition_wake_event(event)` reads the flag.
2. **Bypass debounce + text-merge** in `handle_message`'s busy branch: when the
   event is a transition wake, queue it un-merged (never appended into pending
   text, never debounced) so the in-band drain runs it as a **distinct turn**.
3. **Precedence in `merge_pending_message_event`**: a transition wake and a
   non-wake must never merge into one another. The single slot can hold only one
   event; the wake WINS (a dropped autonomy wake is worse than a user follow-up
   the user can resend). Concretely: refuse to overwrite a pending wake with a
   non-wake; a wake replaces a pending non-wake intact.

This preserves every race-guard invariant (no parallel queue, no new drain site,
no synthetic mid-loop user message, no change to `_active_sessions` lifecycle).
Prompt-cache + role-alternation are untouched: the wake is delivered as an
ordinary next-turn user message via the same cascade every follow-up uses.

## Files

- `gateway/platforms/base.py` — `is_transition_wake_event`, busy-branch bypass,
  `merge_pending_message_event` wake-precedence.
- `gateway/platforms/webhook.py` — `_is_transition_wake_payload` + tag the wake
  event.
- tests — `tests/gateway/test_transition_wake_busy_session.py` (predicate, merge
  precedence, busy-branch queue) + one added case in
  `tests/gateway/test_webhook_origin_routing.py` (wake event is tagged).
- PATCHES.md row.

## Out of scope (sibling cards)

- Self-announcing wake banner (marker + task_id + from→to lane) — t_dfafdefd.
- Latency tightening (notifier interval lag) — t_940f9205.
