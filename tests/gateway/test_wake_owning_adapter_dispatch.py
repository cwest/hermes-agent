"""Regression: an origin-routed kanban-transition wake must be dispatched on the
OWNING platform adapter (the one holding the origin session's busy state), NOT on
the webhook adapter.

Root cause (2026-07-01, live): the webhook builds a wake event whose
source.platform is the origin platform (e.g. discord) and calls
self.handle_message(event) — where self is the WEBHOOK adapter. Each adapter
keeps its own _active_sessions / _pending_messages, so the webhook adapter never
sees the origin session as busy: it takes the cold path, spawns a turn via the
shared runner, collides with the already-running origin turn at the runner's
_running_agents guard, and the wake is silently dropped (no turn_context, banner
never surfaces). All prior adapter-level fixes (#29/#32/#33) were on the wrong
adapter instance and never fired.

The fix dispatches an origin wake through gateway_runner.adapters[origin_platform]
so busy-detection, the wake-precedence pending slot, and the post-turn drain all
run on the SAME session state as the running origin turn.

These tests drive the real BasePlatformAdapter.handle_message on two independent
adapter instances (mimicking the webhook vs discord split) and assert the wake,
when the origin session is busy on the DISCORD adapter, is queued as a distinct
pending turn ON THE DISCORD ADAPTER (not lost on the webhook adapter).
"""
from __future__ import annotations
import sys, types, asyncio
from unittest.mock import MagicMock
import pytest

_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock(); _ct.SUPERGROUP = "supergroup"; _ct.GROUP = "group"; _ct.PRIVATE = "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.platforms.base import (  # noqa: E402
    MessageEvent, MessageType, SessionSource, Platform, SendResult,
    BasePlatformAdapter, build_session_key, is_transition_wake_event,
)
from gateway.config import PlatformConfig  # noqa: E402


class _StubAdapter(BasePlatformAdapter):
    async def connect(self): ...
    async def disconnect(self): ...
    async def get_chat_info(self, *a, **k): return {}
    async def send(self, *a, **k): return SendResult(success=True, message_id="m1")


def _make_adapter(platform: Platform) -> _StubAdapter:
    a = _StubAdapter(PlatformConfig(enabled=True, extra={}), platform)

    async def _handler(ev):
        return None
    a.set_message_handler(_handler)

    async def _busy(ev, sk):
        # Mirror the runner's #32 exemption: wakes fall through (return False).
        return False if is_transition_wake_event(ev) else True
    a.set_busy_session_handler(_busy)
    return a


def _wake_event():
    src = SessionSource(
        platform=Platform.DISCORD, chat_id="c1",
        chat_name="kanban-transition/origin", chat_type="thread", thread_id="c1",
    )
    ev = MessageEvent(text="AUTONOMOUS WAKE banner", message_type=MessageType.TEXT,
                      source=src, message_id="w1")
    ev.metadata["kanban_transition_wake"] = True
    return ev, src


def _select_dispatch_adapter(webhook_adapter, origin_source, source, gateway_runner):
    """Pure re-implementation of webhook._handle_webhook's dispatch-adapter
    selection (the fix), so the routing decision is unit-testable without
    standing up the aiohttp request path."""
    dispatch_adapter = webhook_adapter
    if origin_source is not None:
        adapters = getattr(gateway_runner, "adapters", None) if gateway_runner is not None else None
        if adapters:
            owner = adapters.get(source.platform)
            if owner is not None and owner is not webhook_adapter:
                dispatch_adapter = owner
    return dispatch_adapter


def test_origin_wake_selects_owning_adapter_not_webhook():
    webhook = _make_adapter(Platform.WEBHOOK)
    discord = _make_adapter(Platform.DISCORD)
    gw = types.SimpleNamespace(adapters={Platform.DISCORD: discord, Platform.WEBHOOK: webhook})
    _ev, src = _wake_event()
    chosen = _select_dispatch_adapter(webhook, origin_source=src, source=src, gateway_runner=gw)
    assert chosen is discord, "origin wake must dispatch on the owning (discord) adapter"


def test_origin_wake_falls_back_to_webhook_when_owner_missing():
    webhook = _make_adapter(Platform.WEBHOOK)
    gw = types.SimpleNamespace(adapters={Platform.WEBHOOK: webhook})  # no discord
    _ev, src = _wake_event()
    chosen = _select_dispatch_adapter(webhook, origin_source=src, source=src, gateway_runner=gw)
    assert chosen is webhook, "with no owning adapter, fall back to webhook (backward-compatible)"


def test_non_origin_event_stays_on_webhook():
    webhook = _make_adapter(Platform.WEBHOOK)
    discord = _make_adapter(Platform.DISCORD)
    gw = types.SimpleNamespace(adapters={Platform.DISCORD: discord, Platform.WEBHOOK: webhook})
    _ev, src = _wake_event()
    # origin_source None => ordinary webhook event => stays on webhook adapter
    chosen = _select_dispatch_adapter(webhook, origin_source=None, source=src, gateway_runner=gw)
    assert chosen is webhook


def test_wake_on_owning_adapter_while_busy_queues_distinct_turn():
    """End-to-end on the real handle_message: with the origin session BUSY on the
    discord adapter, dispatching the wake THERE queues it as a distinct pending
    turn (the correct behavior). Dispatching on the webhook adapter would instead
    take the cold path and never touch discord's pending slot."""
    async def _run():
        discord = _make_adapter(Platform.DISCORD)
        ev, src = _wake_event()
        sk = build_session_key(src, group_sessions_per_user=True, thread_sessions_per_user=False)
        # origin session is BUSY on the discord adapter (a turn is running)
        discord._active_sessions[sk] = asyncio.Event()
        await discord.handle_message(ev)
        await asyncio.sleep(0.05)
        # The wake must be queued as the distinct pending turn on THIS adapter.
        assert sk in discord._pending_messages, "wake must be queued on the owning adapter"
        assert is_transition_wake_event(discord._pending_messages[sk])
    asyncio.run(_run())


def test_wake_on_wrong_adapter_never_reaches_owner_pending():
    """Demonstrates the BUG being fixed: dispatching the wake on the webhook
    adapter (wrong instance) leaves the DISCORD adapter's pending slot empty —
    the wake never joins the busy origin session's queue."""
    async def _run():
        webhook = _make_adapter(Platform.WEBHOOK)
        discord = _make_adapter(Platform.DISCORD)
        ev, src = _wake_event()
        sk = build_session_key(src, group_sessions_per_user=True, thread_sessions_per_user=False)
        discord._active_sessions[sk] = asyncio.Event()  # busy on discord
        # WRONG: process on webhook adapter (the old behavior)
        await webhook.handle_message(ev)
        await asyncio.sleep(0.05)
        # discord's pending slot is untouched — the wake was lost to the busy turn.
        assert sk not in discord._pending_messages
    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Integration: drive the REAL _handle_webhook selection block end to end.
#
# Lamport's blocking remark: routing tests 1-3 assert against a hand-copied
# _select_dispatch_adapter that can drift from production. This test POSTs an
# origin-wake payload through the live aiohttp route (adapter._handle_webhook)
# with a mocked gateway_runner.adapters, and asserts the SHIPPED selection block
# dispatches to the OWNING adapter's handle_message — not the webhook adapter's.
# ---------------------------------------------------------------------------

import hashlib as _hashlib  # noqa: E402
import hmac as _hmac  # noqa: E402
import json as _json  # noqa: E402
from aiohttp import web as _web  # noqa: E402
from aiohttp.test_utils import TestClient as _TestClient, TestServer as _TestServer  # noqa: E402
from gateway.config import PlatformConfig as _PlatformConfig  # noqa: E402
from gateway.platforms.webhook import WebhookAdapter as _WebhookAdapter  # noqa: E402


def _sig(body: bytes, secret: str) -> str:
    return "sha256=" + _hmac.new(secret.encode(), body, _hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_real_handle_webhook_routes_origin_wake_to_owning_adapter():
    secret = "wake-owning-adapter-test-secret"
    routes = {
        "kanban-transition": {
            "secret": secret,
            "events": ["blocked"],
            "prompt": "WAKE for {task_id}: {status}",
            "deliver": "log",
        }
    }
    webhook = _WebhookAdapter(
        _PlatformConfig(enabled=True, extra={"host": "0.0.0.0", "port": 0, "routes": routes})
    )

    # Owning (discord) adapter — a stub whose handle_message records the call.
    owner_calls: list = []

    class _Owner:
        async def handle_message(self, ev):
            owner_calls.append(ev)

    owner = _Owner()

    # The webhook adapter's OWN handle_message must NOT be called for an origin wake.
    webhook_calls: list = []

    async def _webhook_hm(ev):
        webhook_calls.append(ev)

    webhook.handle_message = _webhook_hm  # type: ignore[method-assign]

    # Wire the gateway_runner.adapters registry (as run.py does at build time).
    webhook.gateway_runner = types.SimpleNamespace(adapters={Platform.DISCORD: owner})

    app = _web.Application()
    app.router.add_post("/webhooks/{route_name}", webhook._handle_webhook)

    # Origin-wake payload: origin_* fields are what mark it a transition wake and
    # drive _build_origin_source (origin platform = discord).
    payload = {
        "task_id": "t_probe",
        "status": "blocked",
        "event_type": "blocked",
        "origin_platform": "discord",
        "origin_chat_id": "1520255822704152666",
        "origin_thread_id": "1520255822704152666",
    }
    body = _json.dumps(payload).encode()

    async with _TestClient(_TestServer(app)) as cli:
        resp = await cli.post(
            "/webhooks/kanban-transition",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sig(body, secret),
                "X-GitHub-Delivery": "wake-int-001",
            },
        )
        assert resp.status == 202
        # Let the fire-and-forget dispatch task run.
        await asyncio.sleep(0.1)

    # The SHIPPED selection block must have routed to the owning discord adapter.
    assert len(owner_calls) == 1, "origin wake must reach the owning adapter's handle_message"
    assert owner_calls[0].metadata.get("kanban_transition_wake") is True
    assert owner_calls[0].source.platform == Platform.DISCORD
    # And must NOT have run on the webhook adapter.
    assert webhook_calls == [], "origin wake must NOT run on the webhook adapter"


@pytest.mark.asyncio
async def test_real_handle_webhook_falls_back_when_owner_missing():
    """With no owning adapter in the registry, the real route falls back to the
    webhook adapter's own handle_message (backward-compatible)."""
    secret = "wake-fallback-secret"
    routes = {
        "kanban-transition": {
            "secret": secret,
            "events": ["blocked"],
            "prompt": "WAKE {task_id}",
            "deliver": "log",
        }
    }
    webhook = _WebhookAdapter(
        _PlatformConfig(enabled=True, extra={"host": "0.0.0.0", "port": 0, "routes": routes})
    )
    webhook_calls: list = []

    async def _webhook_hm(ev):
        webhook_calls.append(ev)

    webhook.handle_message = _webhook_hm  # type: ignore[method-assign]
    # Registry has NO discord adapter.
    webhook.gateway_runner = types.SimpleNamespace(adapters={})

    app = _web.Application()
    app.router.add_post("/webhooks/{route_name}", webhook._handle_webhook)
    payload = {
        "task_id": "t_probe",
        "status": "blocked",
        "event_type": "blocked",
        "origin_platform": "discord",
        "origin_chat_id": "c1",
        "origin_thread_id": "c1",
    }
    body = _json.dumps(payload).encode()
    async with _TestClient(_TestServer(app)) as cli:
        resp = await cli.post(
            "/webhooks/kanban-transition",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sig(body, secret),
                "X-GitHub-Delivery": "wake-int-002",
            },
        )
        assert resp.status == 202
        await asyncio.sleep(0.1)
    assert len(webhook_calls) == 1, "fallback: wake runs on the webhook adapter when no owner"
