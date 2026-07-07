"""Tests for the kanban→webhook transition emit bridge (4c).

The bridge lets a kanban lifecycle transition wake the orchestrator as an agent
RUN (not just a chat message) by POSTing to a loopback webhook route — mirroring
how a GitHub PR event triggers stage-pr-review. The decision logic is a pure
function so it is exercised here without a live gateway or HTTP server.

Behavior contract (invariants, not snapshots):
  - Disabled by config flag => no emit, regardless of event.
  - Enabled => emit ONLY for the configured transition kinds (default: the
    acceptance ping, kind == "blocked").
  - The emitted payload carries the task id, board, event kind, and reason so the
    route's skill has the context to run a scoped orchestrator action.
  - Idempotency key is stable per (task_id, board, kind, event_id) so a webhook
    retry / a duplicate notifier tick never double-triggers an agent run.
"""

from __future__ import annotations

from gateway.kanban_transition_emit import (
    DEFAULT_EMIT_KINDS,
    build_transition_payload,
    is_sweep_card_title,
    should_emit_transition,
    should_emit_wake,
)


def _cfg(enabled=True, kinds=None, route="kanban-transition"):
    c = {"enabled": enabled, "route": route}
    if kinds is not None:
        c["emit_kinds"] = kinds
    return c


def test_disabled_config_never_emits():
    assert should_emit_transition(_cfg(enabled=False), "blocked") is False
    assert should_emit_transition({}, "blocked") is False
    assert should_emit_transition(None, "blocked") is False


def test_enabled_emits_only_configured_kinds():
    cfg = _cfg(enabled=True)  # default kinds
    assert "blocked" in DEFAULT_EMIT_KINDS
    assert should_emit_transition(cfg, "blocked") is True
    # completed is NOT in the default emit set (done is a chat ping, not a run)
    assert should_emit_transition(cfg, "completed") is False
    assert should_emit_transition(cfg, "heartbeat") is False


def test_explicit_kinds_override_default():
    cfg = _cfg(enabled=True, kinds=["completed"])
    assert should_emit_transition(cfg, "completed") is True
    assert should_emit_transition(cfg, "blocked") is False


# --- Sweep-card class discriminator (internal OKF bookkeeping, never a human wake) --


def test_is_sweep_card_title_matches_write_time_sweep_prefix():
    # The exact title the OKF write-time-sweep machinery files.
    assert is_sweep_card_title("curate: write-time sweep @ 9c63aa9") is True
    # Leading/trailing whitespace and a longer sha suffix must still match.
    assert is_sweep_card_title("  curate: write-time sweep @ deadbeef123") is True


def test_is_sweep_card_title_rejects_real_curation_cards():
    # A genuine cycle-terminal curate card (has a human synopsis) is NOT a sweep.
    assert (
        is_sweep_card_title("curate: audit & steward the intent-driven-development brief")
        is False
    )
    assert is_sweep_card_title("research: what is X?") is False
    assert is_sweep_card_title("fix(gateway): something") is False
    # Defensive: never crash on None/empty.
    assert is_sweep_card_title("") is False
    assert is_sweep_card_title(None) is False


# --- should_emit_wake: the belt for the human-facing wake POST ------------------


def test_should_emit_wake_suppresses_sweep_card_even_with_real_origin():
    # A sweep card must NOT wake a human channel regardless of its (accidental)
    # origin routing — internal bookkeeping never posts.
    assert (
        should_emit_wake(
            title="curate: write-time sweep @ 9c63aa9",
            sub_thread_id="1523894059851186186",
            sub_chat_id="1515909683171557416",
            fallback_chat_id="1515879019269197885",
        )
        is False
    )


def test_should_emit_wake_suppresses_home_fallback_no_origin_card():
    # A card with no real origin: the notifier synthesizes a thread-less
    # fallback sub pointed at the Home channel. That is NOT a human origin —
    # no origin => no human post.
    assert (
        should_emit_wake(
            title="curate: some non-sweep bookkeeping card",
            sub_thread_id="",
            sub_chat_id="1515879019269197885",  # == fallback_chat_id (Home)
            fallback_chat_id="1515879019269197885",
        )
        is False
    )


def test_should_emit_wake_allows_real_origin_thread_card():
    # A card born in a real Discord thread (thread-bearing sub, non-Home) is a
    # legitimate human origin: the wake MUST fire (regression guard for the
    # working synopsis/commissioning path).
    assert (
        should_emit_wake(
            title="curate: audit & steward the intent-driven-development brief",
            sub_thread_id="1523894059851186186",
            sub_chat_id="1515909683171557416",
            fallback_chat_id="1515879019269197885",
        )
        is True
    )


def test_should_emit_wake_allows_real_channel_origin_even_if_thread_less():
    # A genuinely channel-born card (thread-less) whose channel is NOT the Home
    # fallback is a real origin and should still wake.
    assert (
        should_emit_wake(
            title="research: normal card",
            sub_thread_id="",
            sub_chat_id="9999999999",  # a real, non-fallback channel
            fallback_chat_id="1515879019269197885",
        )
        is True
    )


def test_payload_carries_routing_context():
    payload = build_transition_payload(
        task_id="t_abc123",
        board="default",
        kind="blocked",
        reason="awaiting-casey-signoff: merge PR #99",
        event_id=4242,
        title="Adopt event-driven orchestration",
    )
    assert payload["task_id"] == "t_abc123"
    assert payload["board"] == "default"
    assert payload["kind"] == "blocked"
    assert payload["reason"] == "awaiting-casey-signoff: merge PR #99"
    assert payload["title"] == "Adopt event-driven orchestration"
    # A stable idempotency key prevents double agent-runs on retry.
    assert payload["idempotency_key"] == "kanban-transition:default:t_abc123:blocked:4242"


def test_idempotency_key_is_stable_and_distinct():
    p1 = build_transition_payload(
        task_id="t_x", board="default", kind="blocked", reason="r", event_id=1,
    )
    p2 = build_transition_payload(
        task_id="t_x", board="default", kind="blocked", reason="r", event_id=1,
    )
    p3 = build_transition_payload(
        task_id="t_x", board="default", kind="blocked", reason="r", event_id=2,
    )
    assert p1["idempotency_key"] == p2["idempotency_key"]
    assert p1["idempotency_key"] != p3["idempotency_key"]


def test_missing_reason_yields_empty_string_not_none():
    payload = build_transition_payload(
        task_id="t_x", board="default", kind="blocked", reason=None, event_id=1,
    )
    assert payload["reason"] == ""


def test_payload_event_type_is_extractable_by_the_webhook_adapter():
    """Regression: the built payload MUST carry the transition kind in a field
    the webhook adapter's event-type extraction reads, or the route ignores it.

    The adapter (gateway/platforms/webhook.py) resolves the incoming event type
    from, in precedence order:

        X-GitHub-Event header -> X-GitLab-Event header
        -> payload["event_type"] -> payload["type"] -> "unknown"

    The loopback emitter sends no GitHub/GitLab header, so the ONLY way the
    adapter can classify a kanban transition is a body field. If the payload
    lacks ``event_type``/``type``, extraction falls through to ``"unknown"``,
    which is not in the route's ``events`` allowlist ([blocked, completed]) —
    so the adapter returns ``{"status": "ignored"}`` with a 200 and NEVER
    spawns the orchestrator run. That is the exact production symptom this
    guards against (200 received, no agent run).

    This test replicates the adapter's extraction precedence against the built
    payload (no HTTP headers, mirroring the loopback POST) and asserts the
    transition kind is recovered — i.e. the emitter and the adapter agree on
    the wire contract.
    """
    kind = "blocked"
    payload = build_transition_payload(
        task_id="t_evt", board="default", kind=kind, reason="r", event_id=7,
    )

    # Replicate the adapter's exact extraction chain with NO request headers,
    # which is how the loopback bridge POSTs (it uses X-Kanban-Event, a header
    # the adapter does not consult for event classification).
    headers: dict[str, str] = {}
    event_type = (
        headers.get("X-GitHub-Event", "")
        or headers.get("X-GitLab-Event", "")
        or payload.get("event_type", "")
        or payload.get("type", "")
        or "unknown"
    )

    assert event_type == kind, (
        "adapter would classify the transition as %r, so a route filtering on "
        "[blocked, completed] ignores it and no orchestrator run spawns" % event_type
    )

    # And an explicit allowlist check mirroring the adapter's filter:
    allowed_events = ["blocked", "completed"]
    assert event_type in allowed_events, (
        "extracted event %r must be in the route allowlist so the POST "
        "dispatches an agent run instead of returning {'status': 'ignored'}"
        % event_type
    )


# --- Integration: the wired notifier loop actually invokes the bridge ----------

import asyncio  # noqa: E402

from gateway.config import Platform  # noqa: E402
from gateway.run import GatewayRunner  # noqa: E402
from hermes_cli import kanban_db as kb  # noqa: E402


class _RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})
        from gateway.platforms.base import SendResult
        return SendResult(success=True)


async def _run_one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep
    # The first sleep is the cold-start settle (now capped to the interval, not a
    # fixed 5s); pass it through. The next sleep is the post-tick cadence sleep —
    # stop the loop so exactly one tick runs.
    state = {"settled": False}

    async def fake_sleep(delay):
        if not state["settled"]:
            state["settled"] = True
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_states = {}
    runner._kanban_notifier_profile = "default"
    return runner


def _enable_emit(monkeypatch, kinds=None):
    """Make the watcher load a config with transition_emit enabled."""
    emit_cfg = {"enabled": True, "route": "kanban-transition"}
    if kinds is not None:
        emit_cfg["emit_kinds"] = kinds
    cfg = {"kanban": {"dispatch_in_gateway": True, "transition_emit": emit_cfg}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: cfg)
    monkeypatch.setenv("HERMES_KANBAN_TRANSITION_SECRET", "test-secret")


def _make_blocked_sub(reason="awaiting-casey-signoff: merge PR #1"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="emit me", assignee="worker")
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            notifier_profile="default",
        )
        kb.block_task(conn, tid, reason=reason)
        return tid
    finally:
        conn.close()


def test_notifier_fires_bridge_on_blocked_when_enabled(tmp_path, monkeypatch):
    db_path = tmp_path / "emit-on.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _enable_emit(monkeypatch)
    tid = _make_blocked_sub()

    calls = []

    async def fake_emit(cfg, payload, secret):
        calls.append((cfg, payload, secret))
        return True

    monkeypatch.setattr(
        "gateway.kanban_transition_emit.emit_transition", fake_emit
    )

    adapter = _RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    # The chat ping was delivered AND the agent-run bridge fired once.
    assert len(adapter.sent) == 1
    assert len(calls) == 1
    _cfg_arg, payload, secret = calls[0]
    assert payload["task_id"] == tid
    assert payload["kind"] == "blocked"
    assert "merge PR #1" in payload["reason"]
    assert secret == "test-secret"


def test_notifier_does_not_fire_bridge_when_disabled(tmp_path, monkeypatch):
    db_path = tmp_path / "emit-off.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    # Default config: no transition_emit block at all => disabled.
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **k: {"kanban": {"dispatch_in_gateway": True}},
    )
    monkeypatch.delenv("HERMES_KANBAN_TRANSITION_SECRET", raising=False)
    _make_blocked_sub()

    calls = []

    async def fake_emit(cfg, payload, secret):
        calls.append(payload)
        return True

    monkeypatch.setattr(
        "gateway.kanban_transition_emit.emit_transition", fake_emit
    )

    adapter = _RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    # Chat ping still delivered; the bridge never fired (feature off).
    assert len(adapter.sent) == 1
    assert calls == []


def test_notifier_skips_bridge_for_unconfigured_kind(tmp_path, monkeypatch):
    db_path = tmp_path / "emit-completed.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _enable_emit(monkeypatch)  # default emit_kinds == ("blocked",)

    # A COMPLETED event (done) — not in the default emit set.
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="done not emitted", assignee="worker")
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            notifier_profile="default",
        )
        kb.complete_task(conn, tid, summary="finished")
    finally:
        conn.close()

    calls = []

    async def fake_emit(cfg, payload, secret):
        calls.append(payload)
        return True

    monkeypatch.setattr(
        "gateway.kanban_transition_emit.emit_transition", fake_emit
    )

    adapter = _RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.sent) == 1  # done chat ping delivered
    assert calls == []  # but no agent-run bridge for completed


# --- Suppress human-facing wakes for internal bookkeeping / no-origin cards ----
#
# Two production noise sources this closes (root-caused from card t_4f402284,
# a `curate: write-time sweep @ 9c63aa9` card that posted 992 chars to Casey's
# Home channel): a write-time-sweep bookkeeping card, and any card with no real
# origin whose wake falls back to the Home channel. Both must NOT fire the
# agent-run wake, while the chat-ping accounting is unaffected.

# The notifier's default fallback (Home) channel — see _resolve_transition_cfg's
# kanban.notify_fallback.chat_id default. A thread-less sub pointed here is a
# synthesized no-origin fallback, not a human origin.
_HOME_FALLBACK_CHAT_ID = "1515879019269197885"


def test_notifier_suppresses_wake_for_write_time_sweep_card(tmp_path, monkeypatch):
    """A `curate: write-time sweep @ <sha>` card fires the chat ping but NOT the
    agent-run wake — internal OKF bookkeeping never wakes a human channel, even
    when the card carries an (accidental) origin subscription."""
    db_path = tmp_path / "emit-sweep.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _enable_emit(monkeypatch)  # default emit_kinds == ("blocked",)

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="curate: write-time sweep @ 9c63aa9", assignee="avram",
        )
        # A real (non-Home) origin sub — proves the suppression is by CARD CLASS,
        # not merely by the no-origin fallback path.
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            notifier_profile="default",
        )
        kb.block_task(conn, tid, reason="internal sweep bookkeeping")
    finally:
        conn.close()

    calls = []

    async def fake_emit(cfg, payload, secret):
        calls.append(payload)
        return True

    monkeypatch.setattr(
        "gateway.kanban_transition_emit.emit_transition", fake_emit
    )

    adapter = _RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    # Chat-ping accounting is unaffected; the human-facing agent-run wake is
    # suppressed at the source.
    assert len(adapter.sent) == 1
    assert calls == [], "a write-time-sweep card must not fire a human-facing wake"


def test_notifier_suppresses_wake_for_home_fallback_no_origin_card(
    tmp_path, monkeypatch
):
    """A card whose ONLY subscription is a thread-less Home-fallback sub has no
    real human origin: no origin => no human post. The agent-run wake must not
    fire to Home."""
    db_path = tmp_path / "emit-home-fallback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _enable_emit(monkeypatch)

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="some no-origin bookkeeping card", assignee="worker",
        )
        # Thread-less sub pointed at the Home fallback channel — the synthesized
        # no-origin fallback the notifier persists for a sub-less transition.
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram",
            chat_id=_HOME_FALLBACK_CHAT_ID,
            notifier_profile="default",
        )
        kb.block_task(conn, tid, reason="no human origin")
    finally:
        conn.close()

    calls = []

    async def fake_emit(cfg, payload, secret):
        calls.append(payload)
        return True

    monkeypatch.setattr(
        "gateway.kanban_transition_emit.emit_transition", fake_emit
    )

    adapter = _RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.sent) == 1  # chat ping still delivered
    assert calls == [], "a no-origin card must not fire a wake to the Home channel"


def test_notifier_still_fires_wake_for_real_origin_card(tmp_path, monkeypatch):
    """Regression guard: a genuinely origin-born card (real, non-Home channel)
    STILL fires the agent-run wake — the suppression must not break the working
    synopsis / commissioning wake path."""
    db_path = tmp_path / "emit-real-origin.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _enable_emit(monkeypatch)

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="curate: audit & steward the intent-driven-development brief",
            assignee="avram",
        )
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="real-thread-chat",
            notifier_profile="default",
        )
        kb.block_task(conn, tid, reason="awaiting-casey-signoff: merge PR #99")
    finally:
        conn.close()

    calls = []

    async def fake_emit(cfg, payload, secret):
        calls.append(payload)
        return True

    monkeypatch.setattr(
        "gateway.kanban_transition_emit.emit_transition", fake_emit
    )

    adapter = _RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(calls) == 1, "a real origin-born card must still fire its wake"
    assert calls[0]["task_id"] == tid

