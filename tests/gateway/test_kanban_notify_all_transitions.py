"""Tests: the notifier must fire on ALL card transitions, not just terminal ones.

The original notifier gate only delivered five *terminal* event kinds
(``completed``/``blocked``/``gave_up``/``crashed``/``timed_out``). Every other
lane change — ``assigned``, ``unblocked``, and critically
``block_loop_detected`` (the auto-escalate-to-``triage`` signal, the system
asking for a human) — was silently dropped: no chat ping, no orchestrator wake.
A card could escalate to triage and sit silent.

Behavior contract these tests pin (invariants, not snapshots):
  (a) A non-terminal lane change (``assigned``) delivers a chat ping.
  (b) A ``block_loop_detected`` / triage escalation delivers a chat ping AND
      fires the agent-run bridge (the highest-value wake).
  (c) A card with unseen notifiable events but NO subscription still surfaces —
      delivered to the fallback Casey channel so a transition is NEVER silent.

The existing terminal-kind + F1/F2/F3 origin-payload contract must not regress
(covered by test_kanban_transition_emit.py / test_thread_origin_autonomy.py).
"""

from __future__ import annotations

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb

# The default Casey channel a transition falls back to when a card carries no
# origin subscription (the thread-origin autonomy default channel).
FALLBACK_CHANNEL = "1515879019269197885"


class _RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata})
        from gateway.platforms.base import SendResult

        return SendResult(success=True)


async def _run_one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep
    # First sleep = cold-start settle (now capped to the interval, not a fixed
    # 5s); pass it through. Next sleep = post-tick cadence sleep — stop the loop.
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


def _base_config(emit_enabled=False, emit_kinds=None):
    emit_cfg = {"enabled": emit_enabled, "route": "kanban-transition"}
    if emit_kinds is not None:
        emit_cfg["emit_kinds"] = emit_kinds
    return {"kanban": {"dispatch_in_gateway": True, "transition_emit": emit_cfg}}


def _install_config(monkeypatch, cfg, secret="test-secret"):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: cfg)
    if secret is not None:
        monkeypatch.setenv("HERMES_KANBAN_TRANSITION_SECRET", secret)
    else:
        monkeypatch.delenv("HERMES_KANBAN_TRANSITION_SECRET", raising=False)


def _running_task(conn, title="t"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# --- (a) a non-terminal lane change delivers ---------------------------------


def test_assigned_transition_delivers_chat_ping(tmp_path, monkeypatch):
    """An ``assigned`` event (a lane change, NOT a terminal kind) must ping."""
    db_path = tmp_path / "assigned.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _install_config(monkeypatch, _base_config())

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="assign me", assignee="worker")
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            notifier_profile="default",
        )
        # Reassign -> emits an `assigned` transition event.
        assert kb.assign_task(conn, tid, "other-worker")
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.sent) == 1, (
        "an `assigned` lane change must deliver a chat ping, not be dropped"
    )
    assert adapter.sent[0]["chat_id"] == "chat-1"
    assert tid in adapter.sent[0]["text"]


# --- (a2) a card landing in CASEY'S lane delivers a DISTINCT notification -----


def test_assigned_to_casey_lane_delivers_distinct_ready_for_you_notification(
    tmp_path, monkeypatch
):
    """When a card is reassigned to ``casey`` (the human acceptance/merge lane),
    the chat ping must be a DISTINCT, unmistakable "it's in your lane / ready for
    you" notification — not the generic "reassigned -> @casey" line every
    worker-to-worker handoff produces. It must also surface the PR URL inline so
    Casey can act without digging. This is the clear lane signal that was missing
    (a card sat PASS'd in acceptance with only a generic reassign ping).
    """
    db_path = tmp_path / "casey_lane.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _install_config(monkeypatch, _base_config())

    pr_url = "https://github.com/cwest/office/pull/14"
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="feat: something",
            assignee="lamport",
            body=f"Work item.\nPR: {pr_url}\n",
        )
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            notifier_profile="default",
        )
        # PASS handoff: reviewer hands the card to casey's acceptance lane.
        assert kb.assign_task(conn, tid, "casey")
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.sent) == 1, "a casey-lane assignment must deliver a ping"
    text = adapter.sent[0]["text"]
    # DISTINCT from the generic worker reassign ping ("reassigned -> @…").
    assert "reassigned" not in text, (
        "the casey-lane notification must NOT be the generic reassign ping"
    )
    # A clear, unmistakable human-lane signal.
    assert ("READY FOR YOU" in text) or ("🔔" in text), (
        "the casey-lane notification must carry a clear 'ready for you' signal"
    )
    # Actionable: the PR URL inline so Casey can act without digging.
    assert pr_url in text, (
        "the casey-lane notification must include the PR URL inline"
    )
    assert tid in text


# --- (b) triage escalation delivers AND wakes --------------------------------


def test_block_loop_detected_delivers_and_wakes(tmp_path, monkeypatch):
    """The triage-escalation signal is the highest-value ping: chat + wake."""
    db_path = tmp_path / "loop.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    # Emit enabled so the agent-run bridge can fire for this kind.
    _install_config(monkeypatch, _base_config(emit_enabled=True))

    conn = kb.connect()
    try:
        tid = _running_task(conn, title="escalate me")
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            notifier_profile="default",
        )
        # Drive the unblock loop to the recurrence limit -> triage +
        # block_loop_detected event.
        kb.block_task(conn, tid, reason="need creds", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="still need creds", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"
        loop_events = [
            e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"
        ]
        assert loop_events, "precondition: a block_loop_detected event must exist"
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

    # A chat ping for the escalation was delivered ...
    texts = " ".join(d["text"] for d in adapter.sent)
    assert any("triage" in d["text"] or "escalat" in d["text"].lower()
               or tid in d["text"] for d in adapter.sent), (
        "block_loop_detected must produce a chat ping"
    )
    # ... AND the agent-run bridge fired for the block_loop_detected kind.
    assert any(p["kind"] == "block_loop_detected" for p in calls), (
        "block_loop_detected must wake the orchestrator (agent-run bridge)"
    )


# --- (c) no-subscription card still surfaces to the fallback channel ----------


def test_transition_without_subscription_delivers_to_fallback(tmp_path, monkeypatch):
    """A card with NO origin subscription must still surface on transition."""
    db_path = tmp_path / "nosub.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _install_config(monkeypatch, _base_config())

    conn = kb.connect()
    try:
        tid = _running_task(conn, title="orphan")
        # Deliberately NO add_notify_sub — the exact case that sat silent.
        kb.block_task(conn, tid, reason="no origin here", kind="capability")
        assert kb.list_notify_subs(conn, task_id=tid) == []
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.sent) >= 1, (
        "a transition on a card with no subscription must not be silent"
    )
    assert any(d["chat_id"] == FALLBACK_CHANNEL for d in adapter.sent), (
        "no-subscription transition must fall back to the default Casey channel"
    )
    assert any(tid in d["text"] for d in adapter.sent)


# --- (d) transition_emit config hot-reloads mid-run (no restart) --------------


async def _run_two_ticks(monkeypatch, runner, between):
    """Run exactly two notifier ticks, invoking ``between()`` after tick 1.

    Mirrors ``_run_one_tick`` but lets a test mutate live config / board state
    in the gap between the first and second tick, proving per-tick re-reads.
    """
    real_sleep = asyncio.sleep
    # sleep #1 = cold-start settle (pass through); sleep #2 = post-tick-1 cadence
    # (run `between`, keep looping); sleep #3 = post-tick-2 cadence (stop).
    state = {"count": 0}

    async def fake_sleep(delay):
        state["count"] += 1
        if state["count"] == 1:
            return None  # settle
        if state["count"] == 2:
            between()  # mutate config/board between tick 1 and tick 2
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def test_emit_kinds_config_hot_reloads_mid_run(tmp_path, monkeypatch):
    """Flipping kanban.transition_emit.emit_kinds takes effect on the NEXT tick.

    The notifier used to cache transition_emit_cfg ONCE before its poll loop, so
    an emit_kinds change was silently restart-gated. This pins the fix: with a
    ``blocked`` event whose kind is initially OUTSIDE emit_kinds (so the wake
    bridge stays quiet), widening emit_kinds mid-run must let the SAME unseen
    event wake the orchestrator on the next tick — no gateway restart.
    """
    db_path = tmp_path / "hotreload.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    # A fresh load_config() must return a FRESH dict each call (as reading
    # config.yaml from disk does) — otherwise a shared-reference mutation would
    # mask the very restart-gating bug under test. `holder` is what the current
    # config resolves to; the flip REBINDS it to a brand-new object.
    holder = {"cfg": _base_config(emit_enabled=True, emit_kinds=["unblocked"])}
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda *a, **k: holder["cfg"]
    )
    monkeypatch.setenv("HERMES_KANBAN_TRANSITION_SECRET", "test-secret")

    conn = kb.connect()
    try:
        tid = _running_task(conn, title="hot reload me")
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            notifier_profile="default",
        )
        kb.block_task(conn, tid, reason="gate on emit_kinds", kind="capability")
    finally:
        conn.close()

    calls = []

    async def fake_emit(cfg_arg, payload, secret):
        calls.append(payload)
        return True

    monkeypatch.setattr(
        "gateway.kanban_transition_emit.emit_transition", fake_emit
    )

    def widen_emit_kinds():
        # Hot-reload the gate to include `blocked` between the two ticks — a
        # fresh config object, as an on-disk config.yaml edit would produce.
        holder["cfg"] = _base_config(
            emit_enabled=True, emit_kinds=["unblocked", "blocked"]
        )

    adapter = _RecordingAdapter()
    asyncio.run(_run_two_ticks(monkeypatch, _runner(adapter), widen_emit_kinds))

    assert any(p["kind"] == "blocked" for p in calls), (
        "widening transition_emit.emit_kinds mid-run must wake the orchestrator "
        "for `blocked` on the next tick with NO restart (config was cached once "
        "before the loop -> restart-gated)"
    )
