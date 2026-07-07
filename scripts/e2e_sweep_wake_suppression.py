#!/usr/bin/env python3
"""Runtime E2E proof for write-time-sweep / no-origin wake suppression.

Drives the REAL gateway notifier tick (``GatewayRunner._kanban_notifier_watcher``)
— the exact loop the running gateway executes each tick — against a seeded
throwaway board carrying the card classes from the production incident
(card ``t_4f402284`` = ``curate: write-time sweep @ 9c63aa9`` posting 992 chars to
Home ``1515879019269197885``). It records, from inside the tick, every outbound
agent-run wake POST (the real ``emit_transition`` dispatch — the thing that
produces the ``Sending response … to <Home>`` line in the gateway log) and every
chat-ping send, then asserts:

  1. The ``curate: write-time sweep @ 9c63aa9`` card fires its chat ping but emits
     NO agent-run wake POST — i.e. NO ``Sending response … to 1515879019269197885``
     for that wake.
  2. A no-origin card whose only sub is the thread-less Home-fallback sub emits NO
     wake POST to Home either (no origin => no human post).
  3. A real ``#research``-thread-origin card STILL emits its wake POST to its
     origin thread (regression guard for the working synopsis path).

This is the runtime path, not a fresh-Python re-import of a pure function: the
decision runs inside ``_kanban_notifier_watcher`` via the wired ``should_emit_wake``
gate, with ``fallback_chat_id`` resolved from real config (default Home). It is the
strongest pre-merge proof available; the live-gateway variant additionally requires
merge → ``git pull`` deploy into ``~/.hermes/hermes-agent`` → a Casey gateway restart
(the code is restart-gated and undeployed until then).

Run:  uv run python scripts/e2e_sweep_wake_suppression.py
Exit: 0 = all assertions held (suppression proven); non-zero = a wake leaked.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

# Home fallback channel from the incident (default kanban.notify_fallback.chat_id).
HOME = "1515879019269197885"
# A real #research thread origin (from the card: Casey's thread in the research channel).
RESEARCH_THREAD = "1523894059851186186"
RESEARCH_CHANNEL = "1515909683171557416"

SWEEP_TITLE = "curate: write-time sweep @ 9c63aa9"
SYNOPSIS_TITLE = "curate: audit & steward the intent-driven-development brief"
NOORIGIN_TITLE = "some no-origin bookkeeping card"


def _log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    # Isolate to a throwaway DB with a scrubbed env so we NEVER touch the live board.
    tmpdir = tempfile.mkdtemp(prefix="e2e-sweep-wake-")
    db_path = os.path.join(tmpdir, "board.db")
    os.environ["HERMES_KANBAN_DB"] = db_path
    os.environ["HERMES_KANBAN_TRANSITION_SECRET"] = "e2e-secret"

    from hermes_cli import kanban_db as kb
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    import gateway.kanban_transition_emit as kte
    import hermes_cli.config as hcfg

    kb.init_db()

    # --- Seed the board with the three card classes, each BLOCKED (a wake kind) ---
    conn = kb.connect()
    try:
        # 1. The sweep card, given a REAL non-Home origin sub, to prove suppression
        #    is by CARD CLASS, not merely the no-origin fallback path.
        sweep_tid = kb.create_task(conn, title=SWEEP_TITLE, assignee="avram")
        kb.add_notify_sub(
            conn, task_id=sweep_tid, platform="discord",
            chat_id=RESEARCH_CHANNEL, notifier_profile="default",
        )
        kb.block_task(conn, sweep_tid, reason="internal sweep bookkeeping")

        # 2. A no-origin card whose ONLY sub is the thread-less Home-fallback sub
        #    (exactly what the notifier persists for a sub-less transition).
        noorigin_tid = kb.create_task(conn, title=NOORIGIN_TITLE, assignee="worker")
        kb.add_notify_sub(
            conn, task_id=noorigin_tid, platform="discord",
            chat_id=HOME, notifier_profile="default",
        )
        kb.block_task(conn, noorigin_tid, reason="no human origin")

        # 3. A real #research-thread-origin card: MUST still wake its origin.
        synopsis_tid = kb.create_task(conn, title=SYNOPSIS_TITLE, assignee="avram")
        kb.add_notify_sub(
            conn, task_id=synopsis_tid, platform="discord",
            chat_id=RESEARCH_CHANNEL, thread_id=RESEARCH_THREAD,
            notifier_profile="default",
        )
        kb.block_task(
            conn, synopsis_tid, reason="awaiting-casey-signoff: research synopsis",
        )
    finally:
        conn.close()

    # --- Enable the transition-emit feature via real config load ---
    emit_cfg = {"enabled": True, "route": "kanban-transition"}
    cfg = {"kanban": {"dispatch_in_gateway": True, "transition_emit": emit_cfg}}
    hcfg.load_config = lambda *a, **k: cfg  # type: ignore[assignment]

    # --- Intercept the REAL outbound wake dispatch (the gateway-log source) ---
    wake_posts: list[dict] = []

    async def recording_emit(cfg_arg, payload, secret):
        # This is the function that, in production, results in the
        # "origin wake: dispatching … chat=<X> thread=<Y>" + Discord "Sending
        # response … to <X>" log lines. Capturing here == capturing that egress.
        dest_chat = payload.get("origin_chat_id")
        dest_thread = payload.get("origin_thread_id")
        wake_posts.append({
            "task_id": payload.get("task_id"),
            "title": payload.get("title"),
            "chat_id": dest_chat,
            "thread_id": dest_thread,
        })
        _log(
            f"  [WAKE POST] origin wake dispatching task={payload.get('task_id')} "
            f"chat={dest_chat} thread={dest_thread}  (title={payload.get('title')!r})"
        )
        return True

    kte.emit_transition = recording_emit  # type: ignore[assignment]

    # --- A recording chat adapter (proves chat-ping accounting is UNAFFECTED) ---
    chat_sends: list[dict] = []

    class _RecordingAdapter:
        async def send(self, chat_id, text, metadata=None):
            chat_sends.append({"chat_id": chat_id})
            _log(f"  [CHAT PING] Sending response to {chat_id}")
            from gateway.platforms.base import SendResult
            return SendResult(success=True)

    adapter = _RecordingAdapter()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
    runner._kanban_sub_fail_states = {}
    runner._kanban_notifier_profile = "default"

    # --- Drive exactly ONE real notifier tick ---
    async def run_one_tick():
        real_sleep = asyncio.sleep
        state = {"settled": False}

        async def fake_sleep(delay):
            if not state["settled"]:
                state["settled"] = True
                return None
            runner._running = False
            await real_sleep(0)

        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            await runner._kanban_notifier_watcher(interval=1)
        finally:
            asyncio.sleep = real_sleep  # type: ignore[assignment]

    _log("=== Driving one real GatewayRunner._kanban_notifier_watcher tick ===")
    _log(f"    board DB: {db_path}")
    _log(f"    fallback (Home) chat: {HOME}")
    _log("")
    asyncio.run(run_one_tick())
    _log("")

    # --- Assertions (ground truth from the runtime path) ---
    waked_task_ids = {w["task_id"] for w in wake_posts}
    home_wakes = [w for w in wake_posts if w["chat_id"] == HOME]

    _log("=== Results ===")
    _log(f"chat pings delivered: {len(chat_sends)} -> {[c['chat_id'] for c in chat_sends]}")
    _log(f"agent-run wake POSTs: {len(wake_posts)}")
    for w in wake_posts:
        _log(f"    wake -> task={w['task_id']} chat={w['chat_id']} thread={w['thread_id']}")
    _log("")

    ok = True

    # 1. Sweep card: NO wake POST at all (chat ping is fine).
    if sweep_tid in waked_task_ids:
        _log(f"FAIL: sweep card {sweep_tid} fired an agent-run wake (must be suppressed)")
        ok = False
    else:
        _log(f"PASS: sweep card {sweep_tid} fired NO agent-run wake")

    # 2. No-origin card: NO wake POST to Home.
    if noorigin_tid in waked_task_ids:
        _log(f"FAIL: no-origin card {noorigin_tid} fired a wake (must be suppressed)")
        ok = False
    else:
        _log(f"PASS: no-origin card {noorigin_tid} fired NO wake to Home")

    # 3. The load-bearing incident assertion: NO wake POST to the Home channel.
    if home_wakes:
        _log(f"FAIL: {len(home_wakes)} wake POST(s) landed on Home {HOME} (the incident)")
        ok = False
    else:
        _log(f"PASS: NO agent-run wake POST landed on Home {HOME} (no 'Sending response … to {HOME}')")

    # 4. Regression guard: the real #research origin card STILL wakes its thread.
    synopsis_wakes = [w for w in wake_posts if w["task_id"] == synopsis_tid]
    if (
        len(synopsis_wakes) == 1
        and synopsis_wakes[0]["chat_id"] == RESEARCH_CHANNEL
        and synopsis_wakes[0]["thread_id"] == RESEARCH_THREAD
    ):
        _log(
            f"PASS: real #research origin card {synopsis_tid} STILL woke its "
            f"origin thread ({RESEARCH_CHANNEL}/{RESEARCH_THREAD})"
        )
    else:
        _log(
            f"FAIL: real #research origin card {synopsis_tid} did not wake its "
            f"origin thread as expected (got {synopsis_wakes})"
        )
        ok = False

    # Chat-ping accounting must be unaffected: all three cards ping.
    if len(chat_sends) != 3:
        _log(f"FAIL: expected 3 chat pings (accounting unaffected), got {len(chat_sends)}")
        ok = False
    else:
        _log("PASS: chat-ping accounting unaffected (all 3 cards pinged)")

    _log("")
    _log("=== OVERALL: %s ===" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
