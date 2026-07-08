"""
Session-scoped context variables for the Hermes gateway.

Replaces the previous ``os.environ``-based session state
(``HERMES_SESSION_PLATFORM``, ``HERMES_SESSION_CHAT_ID``, etc.) with
Python's ``contextvars.ContextVar``.

**Why this matters**

The gateway processes messages concurrently via ``asyncio``.  When two
messages arrive at the same time the old code did:

    os.environ["HERMES_SESSION_THREAD_ID"] = str(context.source.thread_id)

Because ``os.environ`` is *process-global*, Message A's value was
silently overwritten by Message B before Message A's agent finished
running.  Background-task notifications and tool calls therefore routed
to the wrong thread.

``contextvars.ContextVar`` values are *task-local*: each ``asyncio``
task (and any ``run_in_executor`` thread it spawns) gets its own copy,
so concurrent messages never interfere.

**Backward compatibility**

The public helper ``get_session_env(name, default="")`` mirrors the old
``os.getenv("HERMES_SESSION_*", ...)`` calls.  Existing tool code only
needs to replace the import + call site:

    # before
    import os
    platform = os.getenv("HERMES_SESSION_PLATFORM", "")

    # after
    from gateway.session_context import get_session_env
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
"""

from contextvars import ContextVar
from typing import Any

# Sentinel to distinguish "never set in this context" from "explicitly set to empty".
# When a contextvar holds _UNSET, we fall back to os.environ (CLI/cron compat).
# When it holds "" (after clear_session_vars resets it), we return "" — no fallback.
_UNSET: Any = object()

# Process-level flag: has any code in this process bound a session via
# set_session_vars()? Concurrent multi-session hosts (the messaging gateway, the
# ACP adapter, the API server, the TUI, cron) all do; a pure single-process
# CLI/one-shot that never engages the session-context system does not.
#
# The subprocess-env bridge (tools/environments/local.py) reads this to choose
# its leak policy: when engaged, the ContextVars are authoritative and an _UNSET
# var means "no session bound in THIS task" — so a process-global os.environ
# mirror (written last-writer-wins by whatever concurrent session ran most
# recently) must NOT be inherited into a child process. When never engaged, the
# os.environ fallback is preserved (no concurrency to leak across). Monotonic
# latch — once any host binds a session, the process stays engaged for life.
_session_context_engaged: bool = False


def session_context_engaged() -> bool:
    """True if any session has been bound via set_session_vars in this process.

    See the ``_session_context_engaged`` comment for the leak-policy rationale.
    """
    return _session_context_engaged

# ---------------------------------------------------------------------------
# Per-task session variables
# ---------------------------------------------------------------------------

_SESSION_PLATFORM: ContextVar = ContextVar("HERMES_SESSION_PLATFORM", default=_UNSET)
_SESSION_SOURCE: ContextVar = ContextVar("HERMES_SESSION_SOURCE", default=_UNSET)
_SESSION_CHAT_ID: ContextVar = ContextVar("HERMES_SESSION_CHAT_ID", default=_UNSET)
_SESSION_CHAT_NAME: ContextVar = ContextVar("HERMES_SESSION_CHAT_NAME", default=_UNSET)
_SESSION_THREAD_ID: ContextVar = ContextVar("HERMES_SESSION_THREAD_ID", default=_UNSET)
_SESSION_USER_ID: ContextVar = ContextVar("HERMES_SESSION_USER_ID", default=_UNSET)
_SESSION_USER_NAME: ContextVar = ContextVar("HERMES_SESSION_USER_NAME", default=_UNSET)
_SESSION_KEY: ContextVar = ContextVar("HERMES_SESSION_KEY", default=_UNSET)
_SESSION_ID: ContextVar = ContextVar("HERMES_SESSION_ID", default=_UNSET)
# ID of the message that triggered the current turn. Used as a reply anchor
# so background-process notifications stay inside the originating Telegram
# private-chat topic (those lanes route only with thread id + reply anchor).
_SESSION_MESSAGE_ID: ContextVar = ContextVar("HERMES_SESSION_MESSAGE_ID", default=_UNSET)

# Whether the current session's delivery channel can route an ASYNC completion
# back to the agent AFTER the current turn ends (i.e. wake a fresh turn).
#
# True  — CLI (in-process completion_queue drain) and the real gateway
#         platforms (Telegram/Discord/Slack/...), which hold a persistent
#         outbound channel and run the watcher/drain loops.
# False — stateless request/response adapters (the API server: every route,
#         spec and proprietary, tears down its channel when the turn ends, so
#         a background completion that finishes later has nowhere to go).
#
# Tools that promise async delivery (terminal notify_on_complete /
# watch_patterns, delegate_task background=True) read this via
# ``async_delivery_supported()`` and refuse to hand out a promise the channel
# can't keep — turning a silent no-op into an explicit contract.
#
# Default _UNSET => treated as supported, so CLI (which never sets a platform)
# and any contextvar-unaware path keep working. Stateless adapters opt OUT by
# setting ``supports_async_delivery = False`` on the adapter class; the gateway
# propagates that into this contextvar at session-bind time.
_SESSION_ASYNC_DELIVERY: ContextVar = ContextVar("HERMES_SESSION_ASYNC_DELIVERY", default=_UNSET)

# ---------------------------------------------------------------------------
# Kanban origin — the inheritable delivery surface of a workstream
# ---------------------------------------------------------------------------
#
# A kanban card's "origin" is the concrete delivery surface a transition wake
# (or terminal-state notification) routes to. It is stamped onto the card's
# ``kanban_notify_subs`` row at card-create time. For a card created INSIDE a
# live gateway session that is simply the session's own identity. But the moment
# a workstream crosses a spawn boundary into a DETACHED context — a dispatched
# kanban worker, a delegate_task subagent, a background process, or a nested
# ``kanban_create`` from any of those — the running process's ``HERMES_SESSION_*``
# no longer names the human-origin session; it names the detached run itself.
# A card stamped from that inert identity has a wake with nowhere real to land.
#
# 🟢 Why this rides a SEPARATE channel, NOT _VAR_MAP. The session-identity vars
# are deliberately reset per message (reset_session_vars) and stripped from a
# child env when _UNSET (the _inject_session_context_env engaged-strip) — both
# guards exist to stop one session's identity leaking into a sibling's
# subprocess (production incident 2026-06-21). Origin has the OPPOSITE
# requirement: it must SURVIVE the spawn boundary into a detached child that
# legitimately has an _UNSET session. So it cannot obey the identity-strip rule.
# It rides its own ContextVar with an os.environ mirror (like
# ``set_current_session_id``), crossing the process boundary via the
# already-copied os.environ, and is overwritten only by an explicit
# ``set_kanban_origin`` (a root capture or a reassign) — never implicitly.
_KANBAN_ORIGIN: ContextVar = ContextVar("HERMES_KANBAN_ORIGIN", default=_UNSET)

# Cron auto-delivery vars — set per-job in run_job() so concurrent jobs
# don't clobber each other's delivery targets.
_CRON_AUTO_DELIVER_PLATFORM: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_PLATFORM", default=_UNSET)
_CRON_AUTO_DELIVER_CHAT_ID: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_CHAT_ID", default=_UNSET)
_CRON_AUTO_DELIVER_THREAD_ID: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_THREAD_ID", default=_UNSET)

_VAR_MAP = {
    "HERMES_SESSION_PLATFORM": _SESSION_PLATFORM,
    "HERMES_SESSION_SOURCE": _SESSION_SOURCE,
    "HERMES_SESSION_CHAT_ID": _SESSION_CHAT_ID,
    "HERMES_SESSION_CHAT_NAME": _SESSION_CHAT_NAME,
    "HERMES_SESSION_THREAD_ID": _SESSION_THREAD_ID,
    "HERMES_SESSION_USER_ID": _SESSION_USER_ID,
    "HERMES_SESSION_USER_NAME": _SESSION_USER_NAME,
    "HERMES_SESSION_KEY": _SESSION_KEY,
    "HERMES_SESSION_ID": _SESSION_ID,
    "HERMES_SESSION_MESSAGE_ID": _SESSION_MESSAGE_ID,
    "HERMES_CRON_AUTO_DELIVER_PLATFORM": _CRON_AUTO_DELIVER_PLATFORM,
    "HERMES_CRON_AUTO_DELIVER_CHAT_ID": _CRON_AUTO_DELIVER_CHAT_ID,
    "HERMES_CRON_AUTO_DELIVER_THREAD_ID": _CRON_AUTO_DELIVER_THREAD_ID,
}


def set_current_session_id(session_id: str) -> None:
    """Synchronize ``HERMES_SESSION_ID`` across ContextVar and ``os.environ``.

    Long-lived single-process entrypoints like the CLI can rotate sessions via
    ``/new``, ``/resume``, ``/branch``, or compression splits without
    reconstructing the entire agent. Tools still consult
    ``get_session_env("HERMES_SESSION_ID")`` with an ``os.environ`` fallback,
    so both storage paths must move together when the active session changes.
    """
    import os

    os.environ["HERMES_SESSION_ID"] = session_id
    _SESSION_ID.set(session_id)


# ---------------------------------------------------------------------------
# Kanban origin helpers
# ---------------------------------------------------------------------------

_KANBAN_ORIGIN_ENV = "HERMES_KANBAN_ORIGIN"
_KANBAN_ORIGIN_FIELDS = ("platform", "chat_id", "thread_id", "user_id", "session_id")


def set_kanban_origin(
    platform: str,
    chat_id: str,
    thread_id: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Bind the kanban origin for this context and mirror it to ``os.environ``.

    The origin is the concrete delivery surface a transition wake for any card
    created under this context should route to. Unlike the session-identity
    vars, this is *intended* to propagate to child processes/subagents — the
    ``os.environ`` mirror is what crosses the spawn boundary (the child inherits
    the copied environ; its own ContextVar is ``_UNSET`` so it reads the mirror).

    Overwrites any previously-bound origin — call it only for a genuine ROOT
    capture (top of a human-initiated workstream) or a deliberate reassign,
    never implicitly per message.
    """
    import json
    import os

    blob = {
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "user_id": user_id,
        "session_id": session_id,
    }
    encoded = json.dumps(blob)
    _KANBAN_ORIGIN.set(encoded)
    os.environ[_KANBAN_ORIGIN_ENV] = encoded


def get_kanban_origin() -> dict | None:
    """Return the bound kanban origin as a dict, or ``None`` when unset.

    Resolution order mirrors :func:`get_session_env`: the ContextVar wins when
    set in this context; otherwise the ``os.environ`` mirror (the value a child
    process inherits across the spawn boundary). ``None`` when neither is set or
    the stored blob can't be parsed.
    """
    import json
    import os

    raw: Any = _KANBAN_ORIGIN.get()
    if raw is _UNSET:
        raw = os.environ.get(_KANBAN_ORIGIN_ENV)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    return {k: parsed.get(k) for k in _KANBAN_ORIGIN_FIELDS}


def capture_kanban_origin_from_session() -> dict | None:
    """Resolve the origin to stamp on a card being created in this context.

    Resolution order (design §3.1):
      1. **INHERIT** — an origin is already bound (ContextVar or os.environ
         mirror): return it verbatim. This is the spawn-boundary case — a
         detached worker/subagent whose *session* is foreign but which carries
         the human origin it inherited.
      2. **ROOT capture** — no inherited origin but a live session
         (``HERMES_SESSION_PLATFORM`` + ``_CHAT_ID`` bound): snapshot the live
         session identity as the origin. This is the top of a human-initiated
         workstream.
      3. Neither → ``None`` (a truly detached CLI/cron/test context with no
         live surface to route to).
    """
    inherited = get_kanban_origin()
    if inherited is not None:
        return inherited
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
    if not platform or not chat_id:
        return None
    return {
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": get_session_env("HERMES_SESSION_THREAD_ID", "") or None,
        "user_id": get_session_env("HERMES_SESSION_USER_ID", "") or None,
        "session_id": get_session_env("HERMES_SESSION_ID", "") or None,
    }


def capture_root_origin_if_absent() -> dict | None:
    """Bind the ROOT kanban origin from the live session.

    Called at session bind (the top of a gateway turn). It is the point where a
    workstream's origin is captured: the live session's surface becomes the
    origin every card created under this turn (and every child process it spawns)
    routes its wakes back to.

    A live gateway turn is ALWAYS authoritative for its own origin, so when a
    live session (``HERMES_SESSION_PLATFORM`` + ``_CHAT_ID``) is present this
    (re)binds the origin from it — overwriting any value inherited into this
    task's ContextVar from a concurrent sibling (the copy_context leak window).
    The handler-entry reset (``reset_kanban_origin``) drops the sibling value to
    ``_UNSET`` first; this then stamps THIS turn's surface. Returns the bound
    origin dict, or ``None`` when there is no live session to bind (a detached
    context, which keeps whatever legitimately-inherited origin it carries).

    Detached workers never reach this path — they are subprocesses, not gateway
    turns; their origin arrives via the seeded ``HERMES_KANBAN_ORIGIN`` env and
    is read directly by the card-create subscribe.
    """
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
    if not platform or not chat_id:
        return None
    set_kanban_origin(
        platform=platform,
        chat_id=chat_id,
        thread_id=get_session_env("HERMES_SESSION_THREAD_ID", "") or None,
        user_id=get_session_env("HERMES_SESSION_USER_ID", "") or None,
        session_id=get_session_env("HERMES_SESSION_ID", "") or None,
    )
    return get_kanban_origin()


def reset_kanban_origin() -> None:
    """Drop this task's inherited origin ContextVar (handler-entry leak guard).

    Symmetric with :func:`reset_session_vars`: a per-message task spawned via
    ``create_task`` inherits the spawning context's ContextVars, including a
    concurrent sibling's kanban origin. Reset it to ``_UNSET`` at handler entry
    so a live gateway turn rebinds its OWN origin (via
    :func:`capture_root_origin_if_absent`) rather than acting on the sibling's.

    Only the task-local ContextVar is reset — the ``os.environ`` mirror is
    process-global and left intact (a live turn overwrites it when it rebinds;
    a legitimately-inherited worker origin rides the mirror across the spawn
    boundary and must survive).
    """
    _KANBAN_ORIGIN.set(_UNSET)


def set_session_vars(
    platform: str = "",
    source: str = "",
    chat_id: str = "",
    chat_name: str = "",
    thread_id: str = "",
    user_id: str = "",
    user_name: str = "",
    session_key: str = "",
    session_id: str = "",
    message_id: str = "",
    cwd: str = "",
    async_delivery: bool = True,
) -> list:
    """Set all session context variables and return reset tokens.

    Call ``clear_session_vars(tokens)`` in a ``finally`` block when the handler
    exits. Note ``clear_session_vars`` resets every var to ``""`` (to suppress
    the ``os.environ`` fallback) rather than restoring prior values — these
    helpers are not nestable/stack-safe, and the returned tokens are accepted
    only for API compatibility.

    ``cwd`` pins the logical working directory for this context.

    ``async_delivery`` declares whether this session's channel can route a
    background completion back to the agent after the turn ends (see
    ``_SESSION_ASYNC_DELIVERY`` / ``async_delivery_supported``). Stateless
    request/response adapters (the API server) pass ``False``.
    """
    # Mark the session-context machinery engaged for this process. The
    # subprocess-env bridge uses this to switch from "os.environ fallback" to
    # "ContextVar-authoritative, strip on _UNSET" — see session_context_engaged.
    global _session_context_engaged
    _session_context_engaged = True
    tokens = [
        _SESSION_PLATFORM.set(platform),
        _SESSION_SOURCE.set(source),
        _SESSION_CHAT_ID.set(chat_id),
        _SESSION_CHAT_NAME.set(chat_name),
        _SESSION_THREAD_ID.set(thread_id),
        _SESSION_USER_ID.set(user_id),
        _SESSION_USER_NAME.set(user_name),
        _SESSION_KEY.set(session_key),
        _SESSION_ID.set(session_id),
        _SESSION_MESSAGE_ID.set(message_id),
        _SESSION_ASYNC_DELIVERY.set(bool(async_delivery)),
    ]
    try:
        from agent.runtime_cwd import set_session_cwd

        set_session_cwd(cwd)
    except Exception:
        pass
    return tokens


def clear_session_vars(tokens: list) -> None:
    """Mark session context variables as explicitly cleared.

    Sets all variables to ``""`` so that ``get_session_env`` returns an empty
    string instead of falling back to (potentially stale) ``os.environ``
    values.  The *tokens* argument is accepted for API compatibility with
    callers that saved the return value of ``set_session_vars``, but the
    actual clearing uses ``var.set("")`` rather than ``var.reset(token)``
    to ensure the "explicitly cleared" state is distinguishable from
    "never set" (which holds the ``_UNSET`` sentinel).
    """
    for var in (
        _SESSION_PLATFORM,
        _SESSION_SOURCE,
        _SESSION_CHAT_ID,
        _SESSION_CHAT_NAME,
        _SESSION_THREAD_ID,
        _SESSION_USER_ID,
        _SESSION_USER_NAME,
        _SESSION_KEY,
        _SESSION_ID,
        _SESSION_MESSAGE_ID,
    ):
        var.set("")
    # Reset async-delivery capability to the "never set" sentinel rather than a
    # falsy value: a cleared context should fall back to the default-supported
    # behavior (CLI / unaware paths), not be mistaken for an opted-out
    # stateless adapter.
    _SESSION_ASYNC_DELIVERY.set(_UNSET)
    try:
        from agent.runtime_cwd import clear_session_cwd

        clear_session_cwd()
    except Exception:
        pass


def reset_session_vars() -> None:
    """Reset every session context variable to ``_UNSET`` for THIS context.

    Distinct from :func:`clear_session_vars`, which sets the vars to ``""``
    ("explicitly cleared" — suppresses the os.environ fallback and is used when
    a handler *finishes*).  This helper restores the ``_UNSET`` sentinel
    ("never bound in this context"), which is what a freshly-spawned task should
    look like *before* it binds its own session.

    🔴 Why this exists — the cross-session ContextVar inheritance leak.
    Each gateway message is processed in its own ``asyncio`` task, created via
    ``create_task`` (which snapshots the *current* context with
    ``copy_context``).  When message B's task is spawned from a context where a
    concurrent message A had already called :func:`set_session_vars`, B inherits
    A's **set** ContextVars.  Until B calls its own ``set_session_vars`` there is
    a window where any subprocess B spawns (e.g. a tool shelling out) reads
    *A's* ``HERMES_SESSION_*`` identity via the subprocess-env bridge.  The
    bridge's ``_UNSET``-strip guard cannot help: the vars are not ``_UNSET``,
    they are set-to-A.  Calling ``reset_session_vars`` at the top of the
    per-message handler drops the inherited identity so the window strips safe
    (no session) instead of leaking the foreign one; the handler then binds its
    own via ``set_session_vars`` a few steps later.  See
    tests/tools/test_local_env_session_leak.py and
    tests/gateway/test_session_context_inheritance.py.

    Note ``_SESSION_ASYNC_DELIVERY`` lives outside ``_VAR_MAP`` (it is a bool
    capability flag read via :func:`async_delivery_supported`, not a string
    ``HERMES_SESSION_*`` env var read via :func:`get_session_env`), so it is
    reset explicitly below. Without it, a task spawned from a context where a
    sibling adapter bound ``async_delivery=False`` (the stateless API server)
    inherits that ``False`` through the pre-bind window, and
    ``async_delivery_supported`` wrongly reports the new turn's channel as
    unable to route a background completion until ``set_session_vars`` runs.
    """
    for var in _VAR_MAP.values():
        var.set(_UNSET)
    # Reset the async-delivery capability to "never bound here" (_UNSET) for the
    # same inheritance-leak reason as the mapped vars above — see clear_session_vars,
    # which resets this var on the handler-exit path for the symmetric concern.
    _SESSION_ASYNC_DELIVERY.set(_UNSET)
    try:
        from agent.runtime_cwd import clear_session_cwd

        clear_session_cwd()
    except Exception:
        pass


def get_session_env(name: str, default: str = "") -> str:
    """Read a session context variable by its legacy ``HERMES_SESSION_*`` name.

    Drop-in replacement for ``os.getenv("HERMES_SESSION_*", default)``.

    Resolution order:
    1. Context variable (set by the gateway for concurrency-safe access).
       If the variable was explicitly set (even to ``""``) via
       ``set_session_vars`` or ``clear_session_vars``, that value is
       returned — **no fallback to os.environ**.
    2. ``os.environ`` (only when the context variable was never set in
       this context — i.e. CLI, cron scheduler, and test processes that
       don't use ``set_session_vars`` at all).
    3. *default*
    """
    import os

    var = _VAR_MAP.get(name)
    if var is not None:
        value = var.get()
        if value is not _UNSET:
            return value
    # Fall back to os.environ for CLI, cron, and test compatibility
    return os.getenv(name, default)


def async_delivery_supported() -> bool:
    """Whether the current session can deliver a background completion later.

    Returns ``False`` only when the active session was explicitly bound by a
    stateless adapter (the API server) that cannot route a notification back to
    the agent after the turn ends. CLI, cron, and the real gateway platforms —
    and any path that never bound the contextvar — return ``True``.

    Tools that promise async delivery (``terminal`` notify_on_complete /
    watch_patterns, ``delegate_task`` background=True) consult this before
    registering a watcher / dispatching a detached child, so they can refuse a
    promise the channel can't keep instead of silently no-op'ing.
    """
    value = _SESSION_ASYNC_DELIVERY.get()
    if value is _UNSET:
        return True
    return bool(value)
