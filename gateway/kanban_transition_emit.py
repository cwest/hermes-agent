"""Kanban → loopback-webhook transition emit bridge (event-driven orchestration 4c).

Purpose
-------
The native gateway notifier (`_kanban_notifier_watcher`) delivers a kanban
lifecycle event as a *chat message* to subscribed sources. That covers
"ping Casey when a card reaches acceptance / done." It does NOT wake the
orchestrator as an *agent run* — i.e. let a transition trigger reasoning
(verify the head SHA, compose the exact merge command, post acceptance context).

This module is the thin, additive bridge that turns a transition into an agent
run by POSTing to a loopback webhook route (`/webhooks/kanban-transition`),
mirroring exactly how a GitHub `pull_request` event triggers `stage-pr-review`.

Footprint / safety
------------------
- **Pure decision logic** (`should_emit_transition`, `build_transition_payload`)
  lives here and is fully unit-testable without a gateway or HTTP server.
- The actual POST is a tiny coroutine (`emit_transition`) the notifier calls.
- **Config-flagged, default OFF.** `kanban.transition_emit.enabled` defaults to
  False, so this ships dark and goes live only when Casey enables it + restarts
  (restart-gated, like every gateway-loop config). Until then the notifier path
  is byte-for-byte unchanged.
- **No new core tool**, no new model surface. It reuses the existing webhook
  adapter (the route + its skill), HMAC-signed and loopback-bound, with an
  idempotency key so a retry never double-triggers an agent run.

The route + its orchestrator skill are wired separately (see
``scripts/webhook-ensure-kanban-transition-route.py`` and the
``kanban-transition-orchestrate`` skill); this module is the gateway-side emitter.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Optional

logger = logging.getLogger("gateway.run")

# Kinds that should wake an AGENT RUN (not merely a chat ping). Two proven-painful
# cases lead: the acceptance signal (`blocked` = block+casey), where the
# orchestrator posts the exact merge context; and `block_loop_detected` — the
# auto-escalate-to-`triage` signal, i.e. the system asking for a human. That
# escalation is the HIGHEST-value wake (a card gave up retrying and needs a
# decision), and it used to fire NOTHING at all. `completed`/done is deliberately
# NOT here — done is a close-loop chat ping, not a reasoning task. Override via
# config `kanban.transition_emit.emit_kinds`.
DEFAULT_EMIT_KINDS: tuple[str, ...] = (
    # Terminal / escalation kinds (always woke the orchestrator).
    "blocked",
    "block_loop_detected",
    # Terminal-FAILURE escalation kinds. A worker that gives up, crashes, or
    # times out is the failure-path twin of ``blocked``: the notifier's
    # NOTIFY_KINDS pings chat for these (via TERMINAL_KINDS), so the wake must
    # cover them too or the orchestrator is woken on a clean handoff but stays
    # asleep when a worker actually fails — the same silent-escalation gap this
    # fix closes, on the failure side.
    "gave_up",
    "crashed",
    "timed_out",
    # Actionable lane-move kinds. A card moving to review fires
    # ``status_changed`` + ``assigned``; ``unblocked`` returns a card to the
    # ready lane. These are handoffs the orchestrator must act on — they were
    # silent pre-fix (live miss: card t_278408f5 review handoff woke nothing).
    # ``completed``/done stays OUT deliberately: done is a close-loop chat
    # ping, not a reasoning task.
    "status_changed",
    "assigned",
    "unblocked",
)

DEFAULT_ROUTE = "kanban-transition"
# The webhook adapter binds loopback by default; the bridge POSTs to itself.
DEFAULT_WEBHOOK_HOST = "127.0.0.1"
DEFAULT_WEBHOOK_PORT = 8644

# Fixed, greppable sentinel that leads every transition-wake banner. It is
# deliberately upper-case, hyphenated, and unlikely to occur in ordinary prose,
# so a woken turn that leads with it is UNMISTAKABLE — both the orchestrator and
# Casey can identify (and grep for) the wake, and it can never be conflated with
# a late-delivered prior reply (the ambiguity that cost a live session on
# 2026-07-01). Keep this stable: the E2E probe and any log/dashboard grep key on
# this exact string.
WAKE_BANNER_PREFIX = "AUTONOMOUS-WAKE"
# Stable placeholder for a lane end when the transition carries no from/to pair
# (most kinds don't — crashed/timed_out/gave_up/blocked). Never emit a Python
# ``None`` into the wire text.
_UNKNOWN_LANE = "?"


def build_wake_banner(
    *,
    task_id: str,
    kind: str,
    from_lane: Optional[str] = None,
    to_lane: Optional[str] = None,
    event_id: int = 0,
) -> str:
    """Build the unique, greppable self-announce banner for a woken turn.

    Shape (single source of truth — the route renders it via ``{wake_banner}``
    and the woken orchestrator leads its in-thread reply with it verbatim):

        ``AUTONOMOUS-WAKE <task_id> <kind> <from>-><to> evt=<event_id>``

    e.g. ``AUTONOMOUS-WAKE t_abc blocked ready->blocked evt=4242``.

    The banner is deterministic for a given (task_id, kind, from, to, event_id):
    identical inputs yield a byte-identical string (stable to grep), and a
    different ``event_id`` yields a different banner (so each wake is unique and
    a delivery can never be mistaken for a prior one). When a lane end is unknown
    it degrades to ``?`` rather than leaking ``None`` — most transition kinds do
    not carry a from->to lane pair.
    """
    src = from_lane if from_lane else _UNKNOWN_LANE
    dst = to_lane if to_lane else _UNKNOWN_LANE
    return (
        f"{WAKE_BANNER_PREFIX} {task_id} {kind} {src}->{dst} evt={int(event_id)}"
    )


def should_emit_transition(cfg: Optional[dict], kind: str) -> bool:
    """True when a transition of ``kind`` should POST to the orchestrator route.

    Disabled (falsy cfg or ``enabled`` not True) => always False, so the feature
    ships dark. Enabled => emit only for the configured kinds (default:
    ``DEFAULT_EMIT_KINDS``).
    """
    if not cfg or not isinstance(cfg, dict):
        return False
    if cfg.get("enabled") is not True:
        return False
    kinds = cfg.get("emit_kinds")
    if not kinds:
        kinds = DEFAULT_EMIT_KINDS
    return kind in set(kinds)


# Title prefix of the automatic OKF write-time-sweep bookkeeping card. The
# curation machinery files exactly one ``curate: write-time sweep @ <sha>`` card
# on every push to the knowledge base (see the ``stage-curation-sweep`` skill).
# These are internal corpus-health passes — NOT a report any human needs — so
# their transition wakes must never post to a Discord channel.
_SWEEP_CARD_TITLE_PREFIX = "curate: write-time sweep @"


def is_sweep_card_title(title: Optional[str]) -> bool:
    """True when ``title`` is an automatic write-time-sweep bookkeeping card.

    The write-time-sweep card class (``curate: write-time sweep @ <sha>``) is
    internal OKF curation bookkeeping spawned automatically on every knowledge
    base push. It carries no human origin and its completion is not a synopsis
    trigger, so a human-facing transition wake for it is pure noise (it lands in
    Casey's Home channel). Matching is on the title prefix, tolerant of leading
    whitespace; ``None``/empty never matches.
    """
    if not title:
        return False
    return title.strip().startswith(_SWEEP_CARD_TITLE_PREFIX)


def should_emit_wake(
    *,
    title: Optional[str],
    sub_thread_id: Optional[str],
    sub_chat_id: Optional[str],
    fallback_chat_id: Optional[str],
) -> bool:
    """True when a transition wake for this card+sub should POST a human-facing run.

    This is the belt-and-suspenders gate the notifier applies before firing the
    origin-wake POST (``should_emit_transition`` already decided the *kind* is
    wake-eligible; this decides whether the *destination* is a legitimate human
    origin). Two classes are suppressed at the source so nothing downstream can
    mishandle them:

    1. **Write-time-sweep bookkeeping cards** (``is_sweep_card_title``) — internal
       curation accounting, never a human report. Suppressed unconditionally,
       regardless of any (accidental) origin routing.
    2. **No-origin cards routed to the Home fallback** — a card with no real
       thread origin gets a synthesized, thread-less fallback subscription
       pointed at the Home channel (``fallback_chat_id``). That is not a human
       origin: "no origin => no human post." A thread-less sub whose ``chat_id``
       equals ``fallback_chat_id`` is exactly this case and is suppressed.

    A genuinely human-born card — a thread-bearing sub (real origin thread), or a
    thread-less sub on a real, non-fallback channel — is allowed through, so the
    working synopsis / commissioning wakes keep firing to their origin thread.

    Why the no-origin test is structural (``chat == fallback_chat_id``) rather
    than a read of the ``is_fallback`` flag ``resolve_transition_target`` stamps:
    that flag lives only on the *synthesized delivery sub* the notifier builds
    in-memory for the chat-ping path. The wake this gate protects is fired from a
    *persisted* notify-sub row (``add_notify_sub`` writes a thread-less Home-
    channel sub the first time a no-origin card has no subscription), and a plain
    DB row carries no ``is_fallback`` field. On the next tick that persisted row
    is enumerated as an ordinary sub and feeds the emit-wake path — which is
    exactly the F3 leak that put a sweep card's wake into Home. Deriving the
    condition from the row's own shape (no thread + chat is the fallback channel)
    is what catches that persisted row; the flag would silently miss it.
    """
    if is_sweep_card_title(title):
        return False
    thread = (sub_thread_id or "").strip()
    chat = (sub_chat_id or "").strip()
    fb = (fallback_chat_id or "").strip()
    # No real thread AND targeting the Home fallback channel => synthesized/
    # persisted no-origin fallback, not a human origin. Suppress the wake.
    if not thread and fb and chat == fb:
        return False
    return True


def build_transition_payload(
    *,
    task_id: str,
    board: str,
    kind: str,
    reason: Optional[str],
    event_id: int,
    title: str = "",
    from_lane: Optional[str] = None,
    to_lane: Optional[str] = None,
    origin_session_id: Optional[str] = None,
    origin_platform: Optional[str] = None,
    origin_chat_id: Optional[str] = None,
    origin_thread_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build the JSON body POSTed to the kanban-transition route.

    The idempotency key is stable per ``(board, task_id, kind, event_id)`` so a
    webhook retry or a duplicate notifier tick converges on one agent run.

    ``from_lane``/``to_lane`` are the lane hop this transition represents (when
    the source event carries a ``{"from", "to"}`` pair — ``status_changed`` and
    ``assigned`` do); they feed the self-announce ``wake_banner`` and are
    surfaced to the woken run so it can lead with the banner verbatim.

    The ``origin_*`` fields carry the thread/session this work was born in, so
    the woken orchestrator reports back to that origin thread (the autonomy
    contract) instead of a contextless webhook session. They are omitted from
    the body when unknown (cron/webhook/direct-origin work), and the route's
    handler falls back to the default channel.
    """
    body: dict[str, Any] = {
        "task_id": task_id,
        "board": board,
        "kind": kind,
        # The webhook adapter classifies an incoming event from (in order)
        # the X-GitHub-Event / X-GitLab-Event headers, then body ``event_type``,
        # then body ``type``. The loopback bridge sends none of those headers
        # (its X-Kanban-Event header is not consulted for classification), so
        # ``event_type`` in the BODY is what lets the adapter match this POST
        # against the route's ``events`` allowlist and spawn the orchestrator
        # run. Without it the adapter falls through to "unknown", returns
        # {"status": "ignored"} with a 200, and no run fires. Mirror ``kind``.
        "event_type": kind,
        "reason": reason or "",
        "title": title or "",
        "event_id": event_id,
        # Lane hop (empty string when unknown — keeps the body JSON-stable and
        # never renders a Python ``None`` into the route's prompt template).
        "from_lane": from_lane or "",
        "to_lane": to_lane or "",
        # The unique, greppable self-announce banner. Computed here so the
        # emitter is the single source of truth for its exact shape; the route
        # renders it via ``{wake_banner}`` and the woken orchestrator leads its
        # in-thread reply with it verbatim.
        "wake_banner": build_wake_banner(
            task_id=task_id,
            kind=kind,
            from_lane=from_lane,
            to_lane=to_lane,
            event_id=event_id,
        ),
        "idempotency_key": (
            f"kanban-transition:{board}:{task_id}:{kind}:{event_id}"
        ),
    }
    # Origin routing (only when known — keeps the body byte-stable for the
    # no-origin case and lets the route fall back to the default channel).
    if origin_session_id:
        body["origin_session_id"] = origin_session_id
    if origin_platform:
        body["origin_platform"] = origin_platform
    if origin_chat_id:
        body["origin_chat_id"] = origin_chat_id
    if origin_thread_id:
        body["origin_thread_id"] = origin_thread_id
    return body


def _sign(secret: str, body: bytes) -> str:
    """GitHub-style HMAC-SHA256 hex signature (``sha256=<hex>``)."""
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def resolve_transition_target(
    *,
    session_id: Optional[str],
    sub: Optional[dict],
    default_channel: str,
) -> dict[str, Any]:
    """Resolve WHERE a transition wake should be delivered.

    The autonomy contract: work born in a thread reports back to THAT thread and
    wakes THAT session (so both Casey sees it in the open AND Hollis resumes with
    context). Only when a card has no origin (cron/webhook/direct work) do we
    fall back to the default channel.

    Precedence:
      1. The card's origin session + its subscribed thread source (the thread it
         was born in). ``is_fallback=False``.
      2. The default channel (Casey's #5), when there is no origin session and no
         subscribed thread. ``is_fallback=True``.

    Never targets a throwaway ``webhook:kanban-transition:*`` session — that is
    the F3 bug this replaces (the wake fired into a contextless session and died
    in the log).
    """
    if session_id or sub:
        s = sub or {}
        return {
            "session_id": session_id or None,
            "platform": s.get("platform") or None,
            "chat_id": s.get("chat_id") or None,
            "thread_id": s.get("thread_id") or None,
            "is_fallback": False,
        }
    return {
        "session_id": None,
        "platform": None,
        "chat_id": default_channel,
        "thread_id": None,
        "is_fallback": True,
    }


def dedupe_wake_subs(subs: list[dict]) -> list[dict]:
    """Collapse a card's notify-subs to a single wake target per ``task_id``.

    The wake destination is taken verbatim from the subscription row (the
    notifier reads ``chat_id``/``thread_id`` straight off ``e_sub``), so a card
    that carries BOTH a concrete-origin sub (``discord:<chat>:<thread>``, stamped
    at filing) AND a thread-less channel/fallback sub (``discord:<home>:``, e.g.
    the review-stage defensive re-subscribe) fires TWO wakes: one to the origin
    thread (correct) and one to Home/thread=None — the wake that "goes dark" to
    Casey. Deduping here guarantees exactly ONE wake per card, preferring the
    thread-bearing sub, even on a legacy DB that already has both rows.

    Contract:
    - Group by ``(task_id, platform)``. Within a group, if ANY sub carries a
      non-empty ``thread_id`` (a real origin thread), DROP the thread-less
      duplicates and keep only the thread-bearing one(s).
    - A group with NO thread-bearing sub (a genuinely channel-born card) keeps
      its single channel sub — the fallback stays functional.
    - DISTINCT real threads for the same card are all preserved; only the
      thread-LESS duplicate of a thread-born card is discarded.
    - Input order is irrelevant to the outcome; relative order of surviving subs
      is preserved for determinism.
    """
    # Which (task_id, platform) groups have at least one real thread sub?
    has_thread: set[tuple] = set()
    for s in subs:
        if (s.get("thread_id") or "").strip():
            has_thread.add((s.get("task_id"), s.get("platform")))

    out: list[dict] = []
    for s in subs:
        key = (s.get("task_id"), s.get("platform"))
        thread = (s.get("thread_id") or "").strip()
        # Drop a thread-less sub only when the SAME card+platform also has a
        # real thread sub; otherwise keep it (channel-born card / fallback).
        if not thread and key in has_thread:
            continue
        out.append(s)
    return out


def route_url(cfg: dict) -> str:
    host = cfg.get("webhook_host", DEFAULT_WEBHOOK_HOST)
    port = int(cfg.get("webhook_port", DEFAULT_WEBHOOK_PORT))
    route = cfg.get("route", DEFAULT_ROUTE)
    return f"http://{host}:{port}/webhooks/{route}"


async def emit_transition(cfg: dict, payload: dict, secret: str) -> bool:
    """POST the transition payload to the loopback webhook route.

    Returns True on a 2xx response, False otherwise. Never raises — a bridge
    failure must not break the notifier tick (the chat-ping delivery already
    happened; this is the additive agent-run leg). Requires ``aiohttp`` (the
    same dependency the webhook adapter already needs); if unavailable, logs and
    returns False.
    """
    try:
        from aiohttp import ClientSession, ClientTimeout
    except Exception:  # pragma: no cover - aiohttp present wherever webhook runs
        logger.warning("kanban transition emit: aiohttp unavailable; skipping")
        return False

    url = route_url(cfg)
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        # The route's HMAC validation header (the webhook adapter accepts the
        # GitHub-style X-Hub-Signature-256). The route also filters on event type.
        "X-Hub-Signature-256": _sign(secret, body),
        "X-Kanban-Event": payload.get("kind", ""),
        "X-Idempotency-Key": payload.get("idempotency_key", ""),
    }
    try:
        timeout = ClientTimeout(total=float(cfg.get("timeout_seconds", 10)))
        async with ClientSession(timeout=timeout) as session:
            async with session.post(url, data=body, headers=headers) as resp:
                if 200 <= resp.status < 300:
                    logger.info(
                        "kanban transition emit: POST %s -> %s (task %s, %s)",
                        url, resp.status, payload.get("task_id"), payload.get("kind"),
                    )
                    return True
                logger.warning(
                    "kanban transition emit: POST %s -> %s (task %s)",
                    url, resp.status, payload.get("task_id"),
                )
                return False
    except Exception as exc:
        logger.warning("kanban transition emit: POST %s failed: %s", url, exc)
        return False
