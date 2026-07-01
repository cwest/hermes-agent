"""Regression test: a kanban-transition wake must never be interrupted/steered
/folded into a busy session's in-flight turn — it must fall through to the base
adapter so it is queued un-merged and drained as its OWN distinct turn.

Root-caused live 2026-07-01 (card t_3d5f5523, gateway PID 47017): a transition
wake for a probe card fired cleanly (POST 202, origin routing, banner in
event.text, ``kanban_transition_wake=True`` tagged), yet the entire log showed
0 hits for the wake's banner marker AND no ``agent.turn_context`` line — the wake
reached ``handle_message`` but never became a conversation turn.

The cause: the session was mid-turn (its final streaming API call in flight), so
``handle_message`` routed the wake to the runner busy handler
(``_handle_active_session_busy_message``) FIRST. Under the live
``busy_input_mode='interrupt'``, that handler treated the wake like any user
TEXT message — it called ``running_agent.interrupt(wake_text)`` (folding the
banner into the finishing prior turn's response context, where it was discarded)
and returned ``True``. Returning ``True`` short-circuits ``handle_message``
BEFORE the adapter's wake-precedence enqueue (base.py, the
``is_transition_wake_event`` branch) that would have queued the wake un-merged
for the post-turn drain. The wake was silently swallowed.

The fix mirrors the internal-event exemption (#issue: async-delegation
completion): ``_handle_active_session_busy_message`` returns ``False`` early for
a transition wake, so the base adapter queues it un-merged (wake-precedence slot
guard) and the existing post-turn cascade runs it as a DISTINCT turn. This keeps
the defect-1 design intact end to end: a wake arriving during an in-flight turn
is dispatched as its own turn once that turn ends.
"""

from __future__ import annotations

import sys
import threading
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# Minimal telegram stubs so gateway imports cleanly (mirrors sibling tests).
_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP = "supergroup"
_ct.GROUP = "group"
_ct.PRIVATE = "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.platforms.base import (  # noqa: E402
    MessageEvent,
    MessageType,
    SessionSource,
    build_session_key,
    is_transition_wake_event,
    merge_pending_message_event,
)
from gateway.run import GatewayRunner  # noqa: E402


def _make_wake_event(text: str = "⟪AUTONOMOUS-WAKE⟫ ZANZIBAR-7788 t_probe") -> MessageEvent:
    source = SessionSource(
        platform=MagicMock(value="telegram"),
        chat_id="123",
        chat_type="private",
        user_id="user1",
    )
    ev = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="wake1",
    )
    ev.metadata["kanban_transition_wake"] = True
    return ev


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner.adapters = {}
    runner.config = MagicMock()
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda _source: True
    return runner


def _make_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = MagicMock(value="telegram")
    return adapter


def _make_running_parent() -> MagicMock:
    parent = MagicMock()
    parent._active_children = []  # no active subagents at wake time
    parent._active_children_lock = threading.Lock()
    parent.get_activity_summary.return_value = {
        "api_call_count": 4,
        "max_iterations": 150,
        "current_tool": None,
    }
    return parent


@pytest.mark.asyncio
async def test_transition_wake_does_not_interrupt_busy_session() -> None:
    """A transition wake arriving mid-turn under interrupt mode must fall
    through (return False) WITHOUT interrupting the running agent — so the base
    adapter can queue it un-merged and drain it as its own turn."""
    runner = _make_runner()
    runner._busy_input_mode = "interrupt"  # the live default that caused the bug
    adapter = _make_adapter()
    event = _make_wake_event()
    sk = build_session_key(event.source)
    parent = _make_running_parent()
    runner._running_agents[sk] = parent
    runner.adapters[event.source.platform] = adapter

    handled = await runner._handle_active_session_busy_message(event, sk)

    # Falls through so the base adapter's wake-precedence path enqueues it.
    assert handled is False
    # The in-flight turn must survive — the wake is a NEW turn, not a steer.
    parent.interrupt.assert_not_called()
    parent.steer.assert_not_called()
    # No "⚡ Interrupting current task" ack for an autonomy wake.
    adapter._send_with_retry.assert_not_called()
    # The runner must not have swallowed the wake into its FIFO here — the
    # adapter's wake-precedence enqueue owns that (verified end-to-end below).
    assert adapter._pending_messages.get(sk) is None


@pytest.mark.asyncio
async def test_transition_wake_does_not_steer_into_busy_session() -> None:
    """Under steer mode a wake must also fall through, not splice its banner
    into the running turn via steer()."""
    runner = _make_runner()
    runner._busy_input_mode = "steer"
    adapter = _make_adapter()
    event = _make_wake_event()
    sk = build_session_key(event.source)
    parent = _make_running_parent()
    parent.steer.return_value = True
    runner._running_agents[sk] = parent
    runner.adapters[event.source.platform] = adapter

    handled = await runner._handle_active_session_busy_message(event, sk)

    assert handled is False
    parent.steer.assert_not_called()
    parent.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_wake_becomes_distinct_turn_after_prior_turn_ends() -> None:
    """End-to-end drain: with the wake fallen through to the adapter and queued
    un-merged (wake-precedence slot), the post-turn in-band drain must dispatch
    it as its OWN ``_process_message_background`` turn carrying the banner text.

    This is the load-bearing behaviour the card requires: a wake arriving during
    an in-flight turn is dispatched as its own turn once that turn ends.
    """
    import asyncio

    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter

    class _StubAdapter(BasePlatformAdapter):
        async def connect(self, *, is_reconnect: bool = False):
            pass

        async def disconnect(self):
            pass

        async def send(self, chat_id, text, **kwargs):
            return None

        async def get_chat_info(self, chat_id):
            return {}

    adapter = _StubAdapter(PlatformConfig(enabled=True, token="t"), Platform.TELEGRAM)
    adapter._send_with_retry = AsyncMock(return_value=None)

    src = SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="dm")
    sk = build_session_key(src)

    # The prior turn is "in flight": session active. Its handler, when awaited,
    # simulates the wake arriving mid-turn (queued un-merged via the adapter's
    # wake-precedence path) and then returns — mirroring the live sequence where
    # the wake lands during the prior turn's final API call.
    dispatched_turns: list[str] = []
    wake = _make_wake_event(text="⟪AUTONOMOUS-WAKE⟫ ZANZIBAR-7788")
    wake.source = src

    async def _handler(ev: MessageEvent):
        dispatched_turns.append(ev.text or "")
        if is_transition_wake_event(ev):
            # This is the drained wake turn — do not re-queue.
            return "handled wake"
        # Prior turn: the wake arrives now, while we are still running.
        merge_pending_message_event(adapter._pending_messages, sk, wake)
        return "prior turn done"

    adapter._message_handler = _handler

    prior = MessageEvent(text="prior user turn", message_type=MessageType.TEXT, source=src)
    adapter._active_sessions[sk] = asyncio.Event()
    await adapter._process_message_background(prior, sk)

    # Let the spawned drain task run to completion.
    for _ in range(50):
        await asyncio.sleep(0)
        if any(is_transition_wake_event_text(t) for t in dispatched_turns):
            break

    # The wake must have run as its OWN distinct turn carrying the banner.
    assert dispatched_turns[0] == "prior user turn"
    assert any("ZANZIBAR-7788" in t for t in dispatched_turns[1:]), (
        f"wake never dispatched as its own turn; turns={dispatched_turns!r}"
    )


def is_transition_wake_event_text(text: str) -> bool:
    return "ZANZIBAR-7788" in (text or "")
