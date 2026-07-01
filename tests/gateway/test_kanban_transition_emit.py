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
    should_emit_transition,
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

