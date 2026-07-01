import asyncio
import time
from pathlib import Path


from gateway.config import Platform
from gateway.kanban_watchers import _resolve_notifier_interval
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep
    # The first sleep is the cold-start settle (now capped to the interval, so
    # it is no longer a fixed 5s); let it pass through. Any sleep after that is
    # the post-tick cadence sleep — stop the loop so exactly one tick runs.
    state = {"settled": False}

    async def fake_sleep(delay):
        if not state["settled"]:
            state["settled"] = True
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_states = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_states = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


# ---------------------------------------------------------------------------
# Notifier tick cadence — near-real-time transition-emit latency.
#
# The transition-emit / chat-ping delivery both fire from the notifier tick, so
# the notifier's poll gap bounds worst-case emit latency. Historically the tick
# interval was a hardcoded 5.0s with a hardcoded 5s first-tick settle, so a cold
# start could push a transition emit toward ~10s and the value was not tunable
# without editing source. `_resolve_notifier_interval` makes it config-driven
# (kanban.notifier_interval_seconds) with a snappy near-real-time default and a
# hard >=1.0s floor, and the watcher caps the first-tick settle to the interval.
# ---------------------------------------------------------------------------


def test_resolve_notifier_interval_defaults_near_real_time():
    # No config key => a snappy default that keeps worst-case emit latency well
    # under the <10s near-real-time target.
    assert _resolve_notifier_interval(lambda: {}) <= 3.0
    assert _resolve_notifier_interval(lambda: {"kanban": {}}) <= 3.0


def test_resolve_notifier_interval_reads_config():
    cfg = {"kanban": {"notifier_interval_seconds": 7}}
    assert _resolve_notifier_interval(lambda: cfg) == 7.0


def test_resolve_notifier_interval_floors_at_one_second():
    # A sub-second value is a footgun (busy-poll on the WAL); clamp to >=1.0.
    cfg = {"kanban": {"notifier_interval_seconds": 0.1}}
    assert _resolve_notifier_interval(lambda: cfg) == 1.0
    cfg0 = {"kanban": {"notifier_interval_seconds": 0}}
    assert _resolve_notifier_interval(lambda: cfg0) >= 1.0


def test_resolve_notifier_interval_fails_safe_on_bad_values():
    # Garbage value => fall back to the default, never crash the loop.
    cfg = {"kanban": {"notifier_interval_seconds": "nonsense"}}
    assert _resolve_notifier_interval(lambda: cfg) <= 3.0

    def _boom():
        raise RuntimeError("config read failed")

    assert _resolve_notifier_interval(_boom) <= 3.0


def test_notifier_first_tick_settle_is_bounded_by_interval(tmp_path, monkeypatch):
    """Cold-start settle must not exceed the tick interval.

    The first-tick settle used to be a hardcoded 5s regardless of interval, so a
    tightened interval still ate a 5s cold-start delay before the first emit.
    Capping settle to the interval keeps cold-start latency near-real-time too.
    """
    db_path = tmp_path / "settle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay):
        sleeps.append(delay)
        # Stop the loop after the settle + first tick so the test terminates.
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    runner = _make_runner(RecordingAdapter())

    asyncio.run(runner._kanban_notifier_watcher(interval=2))

    # The first sleep is the settle; it must be <= the interval, not a fixed 5s.
    assert sleeps, "expected the notifier to sleep at least once"
    assert sleeps[0] <= 2.0


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


class FalseResultAdapter:
    """Adapter whose send() reports failure without raising."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        from gateway.platforms.base import SendResult

        self.attempts += 1
        return SendResult(success=False, error="simulated false result")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_notifier_rewinds_claim_on_send_false_result(tmp_path, monkeypatch):
    """A SendResult(success=False) is a failed delivery, not a delivered ping.

    Some platform adapters surface API-level delivery failures as a
    SendResult instead of raising. The notifier must treat that exactly like
    an exception: keep the subscription and rewind the pre-send claim so the
    blocked/completed event is retryable instead of silently consumed.
    """
    db_path = tmp_path / "send-false-result.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FalseResultAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_notifier_keeps_subscription_after_repeated_send_failures(tmp_path, monkeypatch):
    """Repeated failures must not silently unsubscribe a terminal notification.

    Dropping the subscription after retries leaves the human with no visible
    blocked/completed notification and no future retry path. Keep it and leave
    the event unseen so delivery recovers when the adapter/chat recovers.
    """
    db_path = tmp_path / "send-repeated-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    for _ in range(3):
        runner._running = True
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()

    assert adapter.attempts >= 3
    assert len(subs) == 1
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_notifier_backs_off_after_repeated_send_failures(tmp_path, monkeypatch):
    """After MAX_SEND_FAILURES, ticks inside the backoff window skip the send.

    Without backoff a permanently dead chat (bot kicked, channel deleted)
    would burn an adapter.send call and a warning log line every tick,
    forever. The backoff window suppresses the attempt entirely — no claim,
    no rewind, no log — while keeping the subscription and the unseen event
    so delivery still recovers if the chat comes back.
    """
    db_path = tmp_path / "send-backoff.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    # Burn through the transient-failure budget (MAX_SEND_FAILURES = 3).
    for _ in range(3):
        runner._running = True
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert adapter.attempts == 3

    sub_key = (tid, "telegram", "chat-1", "")
    state = runner._kanban_sub_fail_states[sub_key]
    assert state["fails"] == 3
    assert state["next_retry_at"] > time.monotonic()

    # Ticks inside the backoff window must not attempt the send at all.
    for _ in range(2):
        runner._running = True
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert adapter.attempts == 3, "backoff window should suppress send attempts"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]

    # Once the window expires, retry resumes and backoff grows.
    state["next_retry_at"] = time.monotonic() - 1
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert adapter.attempts == 4
    assert runner._kanban_sub_fail_states[sub_key]["fails"] == 4


def test_kanban_notifier_recovers_after_backoff_when_chat_comes_back(tmp_path, monkeypatch):
    """A send success during a backoff retry delivers and clears the state."""
    db_path = tmp_path / "send-backoff-recovery.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    failing = FailingAdapter()
    runner = _make_runner(failing)
    for _ in range(3):
        runner._running = True
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    sub_key = (tid, "telegram", "chat-1", "")
    assert runner._kanban_sub_fail_states[sub_key]["fails"] == 3

    # Chat recovers: swap in a working adapter and expire the window.
    recording = RecordingAdapter()
    runner.adapters = {Platform.TELEGRAM: recording}
    runner._kanban_sub_fail_states[sub_key]["next_retry_at"] = time.monotonic() - 1

    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(recording.sent) == 1
    assert tid in recording.sent[0]["text"]
    assert sub_key not in runner._kanban_sub_fail_states
    assert _unseen_terminal_events(tid) == []


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()
