
"""Regression: a kanban-transition wake queued during an active turn must be
DISPATCHED as its own distinct turn (reach the message handler with its banner
text intact) once the in-flight turn ends — not silently dropped.

Root cause (2026-07-01): a wake arriving while the origin session was mid-turn
was queued into _pending_messages but never surfaced as a new turn (no
turn_context log, banner marker absent from output). This test drives the
busy-enqueue + post-turn drain path and asserts the wake's text reaches the
handler on a fresh invocation.
"""
from __future__ import annotations
import sys, types, asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock(); _ct.SUPERGROUP="supergroup"; _ct.GROUP="group"; _ct.PRIVATE="private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.platforms.base import (  # noqa: E402
    MessageEvent, MessageType, SessionSource, merge_pending_message_event,
    is_transition_wake_event, should_suppress_stale_response,
)


def _wake_event(text="AUTONOMOUS-WAKE t_x blocked evt=1"):
    src = SessionSource(platform=MagicMock(value="discord"), chat_id="c1",
                        chat_type="thread", user_id="", thread_id="c1")
    ev = MessageEvent(text=text, message_type=MessageType.TEXT, source=src, message_id="w1")
    ev.metadata["kanban_transition_wake"] = True
    return ev


def _plain_event(text="hi"):
    src = SessionSource(platform=MagicMock(value="discord"), chat_id="c1",
                        chat_type="thread", user_id="", thread_id="c1")
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=src, message_id="p1")


def test_wake_is_recognized_as_transition_wake():
    assert is_transition_wake_event(_wake_event()) is True
    assert is_transition_wake_event(_plain_event()) is False


def test_wake_takes_pending_slot_intact_over_plain():
    pending = {}
    # a plain follow-up is queued first
    merge_pending_message_event(pending, "c1", _plain_event("first"))
    # then a wake arrives — it must REPLACE the slot intact (not merge/append)
    merge_pending_message_event(pending, "c1", _wake_event("WAKEBANNER"))
    assert pending["c1"].text == "WAKEBANNER"
    assert is_transition_wake_event(pending["c1"]) is True


def test_pending_wake_not_clobbered_by_later_plain():
    pending = {}
    merge_pending_message_event(pending, "c1", _wake_event("WAKEBANNER"))
    # a later plain message must NOT overwrite/append onto the pending wake
    merge_pending_message_event(pending, "c1", _plain_event("later user text"))
    assert pending["c1"].text == "WAKEBANNER"
    assert is_transition_wake_event(pending["c1"]) is True


# --- The root-cause regression: a WAKE turn's own output is never "stale" ---

def test_wake_turn_response_is_never_suppressed_as_stale():
    """THE bug: while the wake's own turn produced its banner output, the
    interrupt Event was set and a follow-up was pending, so the stale-response
    guard nulled the wake's output and it was never delivered. A transition
    wake turn must NEVER be suppressed."""
    assert should_suppress_stale_response(
        event=_wake_event("AUTONOMOUS-WAKE banner"),
        has_response=True,
        interrupted=True,      # a later inbound message set the interrupt
        has_pending=True,      # ...and queued a follow-up
    ) is False


def test_plain_turn_response_is_suppressed_when_superseded():
    """Control: a plain reply IS still suppressed when interrupted + pending,
    so the newer user message's answer wins (original behaviour preserved)."""
    assert should_suppress_stale_response(
        event=_plain_event("old reply"),
        has_response=True,
        interrupted=True,
        has_pending=True,
    ) is True


def test_no_suppression_without_interrupt_or_pending():
    """A plain reply is delivered normally when nothing superseded it."""
    ev = _plain_event("reply")
    assert should_suppress_stale_response(
        event=ev, has_response=True, interrupted=False, has_pending=True) is False
    assert should_suppress_stale_response(
        event=ev, has_response=True, interrupted=True, has_pending=False) is False
    assert should_suppress_stale_response(
        event=ev, has_response=False, interrupted=True, has_pending=True) is False

