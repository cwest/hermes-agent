"""Regression test: a kanban-transition WAKE must bypass the user-authorization
gate on the COLD / IDLE path (``_handle_message``), not just the busy path.

Root cause (2026-07-02, live): an origin-routed transition wake carries a
user-less ``SessionSource`` (the webhook builds it with ``user_id=None`` for a
shared thread; it is HMAC-authenticated upstream). When the origin session is
IDLE (no active turn), the wake is dispatched down the cold path
``GatewayRunner._handle_message``. There the authorization gate

    elif source.user_id is None:
        if not self._is_user_authorized(source):
            logger.debug("Ignoring message with no user_id ...")
            return None          # <-- SILENT DROP (debug, no exception)

fires for a wake targeting an unsubscribed/fallback channel — the wake is
dropped with no turn, no ``turn_context`` line, no logged exception. The busy
path already exempts system events (``_handle_active_session_busy_message``
sets ``_is_system_event = ... or is_transition_wake_event(event)``); the cold
path had no equivalent exemption. This is the regression that stopped idle
origin sessions from ever being woken on a kanban card transition.

This test drives the cold path with an idle-session, unauthorized, user-less
wake and asserts execution PROCEEDS PAST the auth gate instead of returning
``None`` there.
"""
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# Minimal stubs for gateway imports (mirrors test_busy_session_auth_bypass.py)
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

from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    Platform,
    SessionSource,
)


class _GatePassed(Exception):
    """Raised past the auth gate to prove the cold path did NOT drop the wake."""


def _make_idle_runner():
    """Minimal GatewayRunner whose auth check rejects the wake's (empty) user.

    Everything AFTER the auth gate is stubbed to raise ``_GatePassed`` so the
    test can distinguish 'dropped at the gate (returns None)' from 'proceeded'.
    """
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._startup_restore_in_progress = False
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    # Fallback/unsubscribed channel: NOBODY is authorized here.
    runner._is_user_authorized = lambda source: False
    runner._scale_to_zero_note_real_inbound = lambda: None
    # First call past the auth gate — proves the gate was bypassed.
    def _boom(_source):
        raise _GatePassed()
    runner._session_key_for_source = _boom
    return runner


def _make_wake_event(chat_id="c1", thread_id=None):
    """An origin-routed transition wake: user-less source, wake metadata tag."""
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_type="channel",
        user_id=None,       # webhook builds a user-less source
        user_name=None,
        thread_id=thread_id,
    )
    evt = MessageEvent(
        text="AUTONOMOUS WAKE banner",
        message_type=MessageType.TEXT,
        source=source,
        message_id="wake-idle-1",
    )
    evt.metadata["kanban_transition_wake"] = True
    return evt


def _make_plain_userless_event(chat_id="c1", thread_id=None):
    """Control: a NON-wake user-less message (no wake tag)."""
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_type="channel",
        user_id=None,
        user_name=None,
        thread_id=thread_id,
    )
    return MessageEvent(
        text="ordinary service message",
        message_type=MessageType.TEXT,
        source=source,
        message_id="plain-1",
    )


class TestWakeIdleAuthBypass:
    @pytest.mark.asyncio
    async def test_idle_transition_wake_not_dropped_by_auth_gate(self):
        """A kanban-transition wake to an IDLE, unauthorized, user-less session
        must NOT be dropped at the cold-path auth gate — it must proceed."""
        from gateway.run import GatewayRunner

        runner = _make_idle_runner()
        wake = _make_wake_event()

        with pytest.raises(_GatePassed):
            await GatewayRunner._handle_message(runner, wake)

    @pytest.mark.asyncio
    async def test_idle_plain_userless_message_still_dropped(self):
        """Control: a NON-wake user-less message to an unauthorized channel is
        still dropped at the gate (returns None) — the exemption is specific to
        system/wake events, not a blanket bypass."""
        from gateway.run import GatewayRunner

        runner = _make_idle_runner()
        plain = _make_plain_userless_event()

        result = await GatewayRunner._handle_message(runner, plain)
        assert result is None, "unauthorized user-less non-wake message must be dropped"
