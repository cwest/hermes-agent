# Spec: Refuse to create a card with no resolvable origin

Status: design gate approved via card body; TDD in progress
Base: `cwest/integration` @ `252151c33`
Card: t_b76d0836
Scope: make an origin-less card **impossible to create** at the one
`kanban_db.create_task` chokepoint every filing path funnels through — reject a
call that resolves no origin unless it carries an explicit detached-context
marker. This is the "impossible by construction" half of the origin fix; PR #127
(`252151c33`) only cut off the poisoned *supply* at the worker-spawn boundary
(`sanitize_worker_env` drops the ambient `HERMES_KANBAN_ORIGIN`).

## 1. Ground truth (verified against HEAD)

- `create_task(conn, *, ..., session_id: Optional[str] = None, ...)` passes
  `session_id` straight through to the `INSERT` with **no** resolution and **no**
  guard (`hermes_cli/kanban_db.py`). A card filed with `session_id=None`/`""` is
  created successfully and silently.
- The `tasks.session_id` column IS the card's stamped origin (per its schema
  comment: "originating agent/chat session id ... Lets clients render a
  per-session board"). `parse_origin_session` turns that string into the origin
  `kanban_notify_subs` row a per-card transition wake fires from.
- A card whose `session_id` is empty and which never gets an origin notify-sub
  produces transitions that "can never wake anyone" — the exact symptom the card
  names. Origin is advisory at write time when it should be structurally
  required.
- The `submit_card` gate (skill-side `onecard_common`) DOES enforce an origin,
  but it is one path among several (the raw `create_task` tool call, the CLI
  `create`, the dashboard `POST /tasks`, swarm decomposition) rather than the
  only door.

This mirrors the owner-map defect closed at the same chokepoint
(`docs/specs/kanban-create-task-owner-map-chokepoint.md`, card t_0c8744a1): a
routing-critical property was OPTIONAL at birth, so any caller could produce a
structurally broken card. That fix moved the *owner map* guarantee into
`create_task`. This fix does the same for *origin*.

## 2. Design decisions

### 2.1 The marker is a per-call keyword `detached: bool = False`

`create_task` gains `detached: bool = False`. The guard:

```python
if not (session_id and session_id.strip()) and not detached:
    raise ValueError(
        "create_task: no resolvable origin — pass session_id=<origin> or "
        "detached=True for a genuinely origin-less context (test, cron, "
        "detached CLI, dashboard, diagnostic script)."
    )
```

**Why a keyword, NOT an env var / module-global default.** The card's own
constraint: the legitimate origin-less contexts "need an explicit opt-out marker
rather than being allowed to fall through the same silent default that lets a
real filing path lose its origin." An env var set once (e.g. in conftest) or a
process-global default is *exactly* that silent default — a real filing path that
happened to run with the flag set (a worker under a leaked env, a CLI invoked
from a wrapper) would silently lose its origin again. A per-call keyword cannot
be inherited: every detached context declares itself at its own call site, and
every real filing path that forgets an origin fails LOUD at that call site. That
is the "loud at the call site instead of silent on the board" the card asks for.

### 2.2 Enforcement posture: (c) — hard-fail an origin-less, unmarked create

Unlike the owner-map chokepoint (which chose posture (b), always-write-a-map,
because forcing a *card kind* onto the generic primitive was the wrong break),
this card **explicitly mandates the raise**: "reject a `create_task` call that
resolves no origin ... raises rather than filing a card." Origin is not a
card-kind semantic that only some callers have — every caller either has a human
origin or is genuinely detached, and both are cheap to declare. So (c) is correct
here and the churn (every origin-less caller adds `detached=True`) is the
intended, auditable outcome.

### 2.3 "Resolves no origin" is evaluated at the chokepoint

`create_task` does not read env or ContextVars to resolve origin — `session_id`
is purely the passed argument. So "resolves no origin" at the chokepoint means
`session_id` is falsy (None / empty / whitespace). Callers that resolve origin
from richer sources (the `kanban_create` tool folds
`args → _current_origin_session_id() → HERMES_SESSION_ID`) do that resolution
BEFORE calling and pass the result as `session_id`; the chokepoint sees only the
resolved value. This keeps the guard a pure function of its arguments — no hidden
ambient state, matching §2.1's anti-silent-default posture.

## 3. In-tree callers (each supplies an origin or declares detached)

Real filing paths — pass a resolved `session_id`, MUST NOT be `detached`:

- `tools/kanban_tools.py::_kanban_create` — already resolves `session_id`
  (args / `_current_origin_session_id()` / `HERMES_SESSION_ID`). The tool's
  actual wake mechanism is the notify-sub (`_maybe_auto_subscribe`, resolved from
  the richer inherited/gateway/tui/configured-target origin), NOT the
  `session_id` column — a gateway card legitimately carries a routable sub with a
  NULL `session_id` (`test_thread_origin_autonomy.test_create_task_persists_origin_session_id`).
  So the tool passes `detached = (no session_id resolved)`: with a real origin it
  files origin-stamped; without one it declares detached at the column level while
  the sub still gives the card a wake surface. This is a per-call marker driven by
  the tool's own resolution result, not a silent global default. (PR #127 already
  stopped a worker inheriting a *foreign* ambient origin; together with the sub
  machinery, worker-created cards route correctly.)
- `hermes_cli/kanban.py` CLI `create` — passes `--session-id`. Gains a
  `--detached` flag for the deliberate detached-CLI case; without either, the
  create fails with the guard message (surfaced as a clean CLI error, not a
  traceback).

Legitimate detached contexts — pass `detached=True`:

- `hermes_cli/kanban_swarm.py` (root, workers, verifier, synthesizer) — internal
  decomposition children; the swarm root is the audit anchor, not a human origin.
- `plugins/kanban/dashboard/plugin_api.py` `POST /tasks` — browser UI, no chat
  origin. (A future enhancement could thread a dashboard session origin; out of
  scope here.)
- `scripts/e2e_sweep_wake_suppression.py` — a diagnostic that deliberately
  creates no-origin cards to prove suppression.
- The pytest suite — every fixture that files an origin-less card declares
  `detached=True` at the call site (no global conftest flag, per §2.1).

## 4. Behavior contracts (tests — assert the refusal + negative control)

- **Refusal:** `create_task(conn, title=..., assignee=...)` with no `session_id`
  and no `detached` raises `ValueError` and writes NO row (witnessed negative
  control: `list_tasks` count unchanged across the raising call).
- **Detached opt-out files:** the same call with `detached=True` succeeds and the
  row's `session_id` is `NULL`.
- **Origin supplied files:** a call with a real `session_id` succeeds and the row
  carries that origin (and `parse_origin_session` round-trips it to a sub).
- **Whitespace is not an origin:** `session_id="   "` without `detached` raises
  (a blank string must not launder past the guard).
- **CLI:** `hermes kanban create` without `--session-id` and without `--detached`
  exits non-zero with the guard message; `--detached` succeeds.

These assert the RELATIONSHIP (origin present ⇒ files with origin; absent+unmarked
⇒ raises + no row) rather than freezing any name, per the repo's
no-change-detector rule.
