# Spec: Kanban origin inheritance + reassignability

Status: draft (design gate — review before TDD)
Base: `cwest/integration` @ `45a2a2aeb`
Card: t_81acbc3c
Scope: origin **inheritance** (C) + **reassignability** (D). The active-origin
delivery + progressive fallback (A/B) is already merged (#29/#32/#33/#34/#51/#52)
and is out of scope here — do not rebuild it.

## 1. Ground truth (verified against HEAD)

A kanban card's "origin" — the concrete delivery surface a transition wake or a
terminal-state notification routes to — is **the card's `kanban_notify_subs`
row(s)**. Nothing else. The wake emitter reads the destination verbatim off that
row:

- `gateway/kanban_watchers.py:1011-1023` builds the transition payload with
  `origin_platform / origin_chat_id / origin_thread_id` taken from `e_sub`
  (a `kanban_notify_subs` row) and `origin_session_id` from the task's
  `session_id`.
- `hermes_cli/kanban_db.py:9746 add_notify_sub(...)` is the sole writer of that
  row. It is idempotent on `(task, platform, chat, thread)` and carries a
  thread-less-MISROUTE guard: a thread-less sub is skipped when a thread-bearing
  sub already exists for `(task, platform)` (two rows = two wakes, one dark).
- The row is stamped at card-creation time by
  `tools/kanban_tools.py:953 _maybe_auto_subscribe(conn, task_id)`, which reads
  the **currently running process's** session identity from
  `HERMES_SESSION_PLATFORM / _CHAT_ID / _THREAD_ID / _USER_ID` via
  `gateway.session_context.get_session_env`.

Cron is the one emitter that already stamps an explicit origin
(`tools/cronjob_tools.py:285 _origin_from_env`), for the same reason: a cron run
is detached from any live session, so it must capture the origin at *schedule*
time and replay it at *fire* time.

## 2. The two gaps

### Gap C — inheritance across the spawn boundary

`_maybe_auto_subscribe` reads the **running process's own** session env. That is
correct for a card created *inside a live gateway session* (an orchestrator in a
Discord thread fans out; the child card inherits that thread — works today). It
is **wrong** the moment the workstream crosses a spawn boundary into a detached
context:

- a dispatched kanban worker (fresh subprocess, `default`/webhook session),
- a `delegate_task` subagent,
- a background process,
- any nested `kanban_create` issued from one of the above.

In those contexts `HERMES_SESSION_*` reflects the *detached run's own* identity
(contextless / `webhook:` / empty), not the **human-origin session** that
started the whole workstream. Result: the child card is stamped with an inert
origin, so its wakes have nowhere real to land — "a detached run speaks into the
void" (the card's own framing).

**Hard constraint — do NOT overload `HERMES_SESSION_*` to carry origin.** Those
vars are deliberately session-scoped and reset per message
(`reset_session_vars` at the top of `_handle_message`) precisely to stop one
session's identity leaking into a sibling's subprocess (regression tests:
`tests/gateway/test_session_context_inheritance.py`,
`tests/tools/test_local_env_session_leak.py`; production incident 2026-06-21).
Origin inheritance must therefore ride a **distinct, explicit channel** that is
*intended* to propagate to children — never the general session-identity vars.

### Gap D — reassignability

When a genuinely new workstream forks, an orchestrator wants to mint a new thread
in the right channel and designate it the origin for all future work on that
fork, so subsequent wakes land in the new thread, not the old one. Today the only
levers are `add_notify_sub` (blocked by the thread-less guard in the common case)
and `remove_notify_sub`. There is no atomic "re-point this card's origin to
`(platform, chat, thread)`" operation, and no way to do it for a *set* of related
cards.

## 3. Design

### 3.1 Origin as an explicit, inheritable value — `HERMES_KANBAN_ORIGIN`

Introduce a single explicit origin channel, distinct from session identity:

- A new ContextVar `_KANBAN_ORIGIN` (env name `HERMES_KANBAN_ORIGIN`) holding a
  JSON blob `{"platform","chat_id","thread_id","user_id","session_id"}`.

  **It is deliberately NOT added to `_VAR_MAP`.** `_VAR_MAP` membership subjects a
  var to two behaviours that are correct for session *identity* but wrong for an
  *inheritable* origin: (1) the per-message `reset_session_vars` strip-to-`_UNSET`
  (`_handle_message`), and (2) the `_inject_session_context_env` engaged-strip
  rule (`tools/environments/local.py:322-330`) that DROPS the var from a child env
  whenever THIS task's ContextVar is `_UNSET`. A detached child legitimately has
  an `_UNSET` *session* but MUST keep the *inherited* origin — so origin cannot
  obey the strip rule.

  Instead:
  - `_KANBAN_ORIGIN` lives as a standalone ContextVar with an `os.environ` mirror
    (like `set_current_session_id`), so it crosses the process boundary via the
    already-copied `os.environ` in `_make_run_env` without the strip.
  - `reset_session_vars` does NOT touch it (it is not in `_VAR_MAP`), so it
    survives sibling-message resets. It is overwritten only by an explicit
    `set_kanban_origin` (a new root capture or a reassign) — never implicitly.
  - The one leak risk this reintroduces (a stale `os.environ` origin inherited by
    an unrelated later turn) is bounded because origin is only ever *read* by
    `_maybe_auto_subscribe` at card-create and by the reassign tool — both of
    which run inside a bound turn that has already set its own origin at session
    bind (root capture, §3.1 point 1). A turn that never bound an origin
    (pure-CLI one-shot) has no live surface to leak *to* anyway.

- Helpers in `session_context`:
  - `set_kanban_origin(platform, chat_id, thread_id=None, user_id=None, session_id=None)`
    — sets the var (and mirrors to `os.environ` for the subprocess bridge, like
    `set_current_session_id`).
  - `get_kanban_origin() -> dict | None` — parse the var; `None` when unset.
  - `capture_kanban_origin_from_session() -> dict | None` — if
    `HERMES_KANBAN_ORIGIN` is already set, return it verbatim (INHERIT); else, if
    a live `HERMES_SESSION_PLATFORM`+`_CHAT_ID` exists, snapshot it as the origin
    (this is the ROOT capture at the top of the chain). Detached contexts with
    neither return `None`.

**Binding points (where the origin is captured/propagated):**

1. **Root capture — live gateway session.** At session bind
   (`set_session_vars` in `_handle_message`), also
   `set_kanban_origin(...)` from the just-bound session identity **iff no origin
   is already inherited**. This is the top of every human-initiated workstream.
2. **Inheritance — dispatched kanban worker.** The dispatcher already passes the
   card's context into the worker subprocess. When a card carries an origin
   notify-sub, the dispatcher seeds `HERMES_KANBAN_ORIGIN` in the worker's env
   from that sub, so anything the worker creates re-inherits the human origin.
3. **Inheritance — delegate_task / background process.** These build a child run
   env; add `HERMES_KANBAN_ORIGIN` to the inherited set so the subagent/bg
   process carries the origin (the subprocess-env bridge already carries
   `HERMES_SESSION_*`; we add this one var to the carried set).

### 3.2 `_maybe_auto_subscribe` prefers the inherited origin

`_maybe_auto_subscribe` (`tools/kanban_tools.py:953`) changes its source of
truth to:

    origin = get_kanban_origin() or capture_kanban_origin_from_session()

- If `origin` is present → stamp the notify-sub from it
  (`platform/chat_id/thread_id/user_id`) via the existing `add_notify_sub`.
  This is the inheritance fix: a child card created in a detached worker now
  subscribes the **human origin**, not the detached run's inert identity.
- If `origin` is `None` → fall back to the existing behaviour verbatim
  (current-session env → TUI key → configured `report_back_target` → no sub).
  No regression for today's live-session-created cards (their inherited origin ==
  their own session identity, so the stamped row is byte-identical).

The thread-less MISROUTE guard in `add_notify_sub` is unchanged and still
protects against a second dark sub.

### 3.3 Reassignability — `reassign_task_origin`

Add a DB primitive + a worker tool:

- `hermes_cli/kanban_db.py: reassign_task_origin(conn, *, task_id, platform,
  chat_id, thread_id=None, user_id=None, notifier_profile=None)` — inside a
  single `write_txn`: DELETE the task's existing `kanban_notify_subs` rows **for
  that platform**, then INSERT the new `(platform, chat_id, thread_id)` row,
  seeding `last_event_id` to the latest already-existing notifiable event id (so
  the re-point does NOT replay history — reuse the same cursor-seed logic
  `add_notify_sub` uses for the lazy fallback). Idempotent: re-pointing to the
  same surface is a no-op. Returns the new row.
  - Deleting only the same-platform rows preserves multi-platform fan-out subs
    while atomically moving the origin for the platform being repointed, and
    sidesteps the thread-less guard (guard only blocks *adds*, not this
    delete+insert).
- Optional cascade: `reassign_task_origin(..., include_descendants=True)` walks
  the card's child links and repoints each, for "move the whole fork to the new
  thread." (Ship single-card first; cascade behind the same function's flag.)
- Worker tool `kanban_reassign_origin` (in `tools/kanban_tools.py`) exposing the
  primitive, so an orchestrator can mint a thread and designate it the origin.
  Also refresh `HERMES_KANBAN_ORIGIN` in the caller's context to the new surface
  so *subsequently* created child cards inherit the reassigned origin too.

## 4. Behaviour contract (what the tests assert)

Behaviour-contract tests, not snapshots. Resolution ORDER and invariants:

- **C1 (root capture):** a live-session card-create stamps a notify-sub whose
  `(platform, chat_id, thread_id)` == the live session's surface. (No regression —
  identical to today.)
- **C2 (inheritance across boundary):** with `HERMES_KANBAN_ORIGIN` set to a
  human origin and `HERMES_SESSION_*` set to a *detached/foreign* identity, a
  card-create stamps the sub from the **inherited origin**, NOT the detached
  identity. This is the core fix.
- **C3 (no-origin fallback):** with neither origin nor a live session,
  `_maybe_auto_subscribe` behaves exactly as today (report_back_target or no sub).
- **C4 (no identity leak):** binding a kanban origin does NOT mutate any
  `HERMES_SESSION_*` var; the existing inheritance/leak guards still pass
  unchanged.
- **D1 (reassign):** `reassign_task_origin` to a new thread replaces the origin
  sub for that platform; a subsequent transition wake resolves to the new
  `(chat, thread)`; no historical-event replay (cursor seeded to latest).
- **D2 (reassign idempotent):** repointing to the current surface is a no-op
  (row unchanged, cursor not rewound).
- **D3 (fork inheritance after reassign):** after `kanban_reassign_origin`, a
  newly created child card inherits the reassigned surface.
- **E2E:** temp `HERMES_HOME`, real notifier tick + a simulated live origin
  session; assert an inherited-origin card's wake enters the origin session's
  turn loop (owning-adapter dispatch), and a reassigned card's wake lands on the
  new thread.

## 5. Guard rails honoured

- Reuse the existing async-delivery + owning-adapter dispatch + wake-precedence
  path (A/B); no parallel delivery path invented.
- Prompt caching + strict role alternation preserved — no synthetic mid-loop user
  message; delivery is unchanged (this card only fixes *where the origin points*,
  not *how* the wake is delivered).
- Wake-banner retained as grep/ID key.
- `HERMES_SESSION_*` identity semantics + leak guards untouched; origin rides a
  separate, intentionally-inherited var.
- Config-flagged consistent with the existing gate: inheritance is gated by the
  same `kanban.auto_subscribe_on_create` that already governs stamping; reassign
  is an explicit tool call (no passive behaviour change).

## 6. Files touched (planned)

- `gateway/session_context.py` — `_KANBAN_ORIGIN` var + `_VAR_MAP` entry +
  `set_kanban_origin` / `get_kanban_origin` / `capture_kanban_origin_from_session`.
- `tools/environments/local.py` — carry `HERMES_KANBAN_ORIGIN` in the subprocess
  run env.
- `tools/kanban_tools.py` — `_maybe_auto_subscribe` prefers inherited origin;
  new `kanban_reassign_origin` tool.
- `hermes_cli/kanban_db.py` — `reassign_task_origin` primitive.
- Root capture at session bind (in `_handle_message` / `set_session_vars`
  caller) + dispatcher/delegate/background env seeding.
- Tests: `tests/gateway/test_kanban_origin_inheritance.py`,
  `tests/gateway/test_kanban_origin_reassign.py`, plus an E2E under the existing
  gateway E2E harness.
