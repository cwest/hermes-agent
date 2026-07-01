"""Defect 1 (busy-session swallow): a kanban-transition wake must produce a
DISTINCT turn even when the origin session is mid-turn.

Proven live 2026-07-01: the wake POST targets the origin session correctly, but
when that session is busy, ``handle_message`` queue-merges the inbound wake into
the in-progress/queued turn (text debounce + ``merge_pending_message_event``
with ``merge_text=True``). A wake landing during an active turn is absorbed
silently instead of producing its own identifiable turn.

The fix tags a transition-wake ``MessageEvent`` (metadata flag) so the busy path
never text-merges/debounces it — it is queued un-merged and drained by the
existing in-band cascade as a distinct turn, and it takes precedence over an
unrelated pending non-wake message in the single pending slot (a dropped autonomy
wake is worse than a user follow-up the user can resend).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    is_transition_wake_event,
    merge_pending_message_event,
)
from gateway.session import SessionSource, build_session_key


class _StubAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False):
        pass

    async def disconnect(self):
        pass

    async def send(self, chat_id, text, **kwargs):
        return None

    async def get_chat_info(self, chat_id):
        return {}


def _make_adapter():
    adapter = _StubAdapter(PlatformConfig(enabled=True, token="t"), Platform.TELEGRAM)
    adapter._send_with_retry = AsyncMock(return_value=None)
    return adapter


def _src(chat_id="42"):
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm")


def _text_event(text="hi", chat_id="42"):
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_src(chat_id))


def _wake_event(text="⟪AUTONOMOUS-WAKE⟫ t_x", chat_id="42"):
    ev = MessageEvent(text=text, message_type=MessageType.TEXT, source=_src(chat_id))
    ev.metadata["kanban_transition_wake"] = True
    return ev


def _sk(chat_id="42"):
    return build_session_key(_src(chat_id))


# --- the predicate --------------------------------------------------------------

def test_is_transition_wake_event_reads_metadata_flag():
    assert is_transition_wake_event(_wake_event()) is True
    assert is_transition_wake_event(_text_event()) is False


# --- merge precedence: a wake never loses its identity ---------------------------

def test_wake_is_not_text_merged_into_pending_user_text():
    """A pending user TEXT + an incoming wake must NOT newline-merge — the wake
    stays a distinct event (its banner would otherwise be buried in an unrelated
    turn's text)."""
    pending = {}
    sk = _sk()
    merge_pending_message_event(pending, sk, _text_event("user question"), merge_text=True)
    merge_pending_message_event(pending, sk, _wake_event("⟪AUTONOMOUS-WAKE⟫ t_x"), merge_text=True)
    slot = pending[sk]
    # The wake WINS the single slot and is preserved intact (never appended).
    assert is_transition_wake_event(slot)
    assert slot.text == "⟪AUTONOMOUS-WAKE⟫ t_x"
    assert "user question" not in slot.text


def test_pending_wake_is_not_overwritten_by_a_later_user_text():
    """A pending wake must not be clobbered by a subsequent non-wake message —
    the autonomy wake is higher-priority and rarer."""
    pending = {}
    sk = _sk()
    merge_pending_message_event(pending, sk, _wake_event("⟪AUTONOMOUS-WAKE⟫ t_x"), merge_text=True)
    merge_pending_message_event(pending, sk, _text_event("later user text"), merge_text=True)
    slot = pending[sk]
    assert is_transition_wake_event(slot)
    assert slot.text == "⟪AUTONOMOUS-WAKE⟫ t_x"


def test_non_wake_text_still_merges_normally():
    """Regression guard: ordinary text-into-text merge is unchanged."""
    pending = {}
    sk = _sk()
    merge_pending_message_event(pending, sk, _text_event("part one"), merge_text=True)
    merge_pending_message_event(pending, sk, _text_event("part two"), merge_text=True)
    assert pending[sk].text == "part one\npart two"
    assert not is_transition_wake_event(pending[sk])


# --- the busy branch queues a wake un-merged as a distinct turn ------------------

@pytest.mark.asyncio
async def test_busy_session_queues_wake_un_merged_without_debounce():
    """When the session is active, a transition wake is queued as its own
    un-merged pending turn (never debounced, never text-merged), so the in-band
    drain runs it as a DISTINCT turn."""
    import asyncio

    adapter = _make_adapter()
    adapter._message_handler = AsyncMock(return_value="ok")
    # Force the queue/debounce path that would otherwise absorb text.
    adapter._busy_text_mode = "queue"
    sk = _sk()

    # Mark the session active so handle_message takes the busy branch.
    adapter._active_sessions[sk] = asyncio.Event()

    await adapter.handle_message(_wake_event("⟪AUTONOMOUS-WAKE⟫ t_x"))

    # The wake must be queued as a distinct, un-merged pending event —
    # NOT buffered in a debounce timer, NOT merged into other text.
    slot = adapter._pending_messages.get(sk)
    assert slot is not None, "wake was swallowed (debounced or dropped)"
    assert is_transition_wake_event(slot)
    assert slot.text == "⟪AUTONOMOUS-WAKE⟫ t_x"
