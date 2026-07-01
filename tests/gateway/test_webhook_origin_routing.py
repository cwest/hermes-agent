"""Tests: the kanban-transition wake routes to the ORIGIN thread session.

Defect 2 (proven live, card t_278408f5): the webhook route ignored the
``origin_*`` fields the emitter sends, so every transition wake ran in a
contextless ``webhook:<route>:<delivery>`` session and never reached the origin
Discord thread. These assert the route now honors origin routing, with a clean
fallback when origin fields are absent (no regression to non-origin webhooks).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.config import PlatformConfig


def _make_adapter(routes):
    extra = {"host": "0.0.0.0", "port": 0, "routes": routes,
             "rate_limit": 100, "max_body_bytes": 1_048_576}
    return WebhookAdapter(PlatformConfig(enabled=True, extra=extra))


def _app(adapter):
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _kt_route():
    return {"kanban-transition": {"secret": _INSECURE_NO_AUTH, "prompt": "{title}"}}


@pytest.mark.asyncio
async def test_wake_targets_origin_thread_when_origin_fields_present():
    """A POST carrying origin_* fields builds a source targeting the origin
    thread/session — NOT the contextless webhook: session."""
    adapter = _make_adapter(_kt_route())
    captured = {}

    async def _capture(event):
        captured["event"] = event

    adapter.handle_message = AsyncMock(side_effect=_capture)

    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await cli.post(
            "/webhooks/kanban-transition",
            json={
                "event_type": "status_changed",
                "task_id": "t_abc",
                "title": "probe card",
                "origin_session_id": "20260629_125345_f5fe11ba",
                "origin_platform": "discord",
                "origin_chat_id": "1520255822704152666",
                "origin_thread_id": "1520255822704152666",
            },
        )
        assert resp.status == 202

    adapter.handle_message.assert_awaited_once()
    src = captured["event"].source
    # The wake must be routed to the ORIGIN, not a webhook: synthetic session.
    assert src.platform.value == "discord"
    assert str(src.chat_id) == "1520255822704152666"
    assert str(getattr(src, "thread_id", "")) == "1520255822704152666"
    assert not str(src.chat_id).startswith("webhook:")


@pytest.mark.asyncio
async def test_wake_falls_back_to_webhook_session_without_origin_fields():
    """No origin fields → current behavior: a webhook:<route>:<delivery>
    session. Backward-compatible; non-origin webhooks are unaffected."""
    adapter = _make_adapter(_kt_route())
    captured = {}

    async def _capture(event):
        captured["event"] = event

    adapter.handle_message = AsyncMock(side_effect=_capture)

    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await cli.post(
            "/webhooks/kanban-transition",
            json={"event_type": "status_changed", "task_id": "t_abc",
                  "title": "no-origin card"},
        )
        assert resp.status == 202

    adapter.handle_message.assert_awaited_once()
    src = captured["event"].source
    assert str(src.chat_id).startswith("webhook:kanban-transition:")


def test_origin_source_yields_the_live_thread_session_key():
    """The origin source MUST produce the exact session key the live Discord
    thread inbound produces — otherwise the wake lands in a phantom session
    (the F2 key-mismatch trap). Locks the key-mirror invariant."""
    from gateway.session import build_session_key
    adapter = _make_adapter(_kt_route())
    src = adapter._build_origin_source(
        "discord", "1520255822704152666", "1520255822704152666"
    )
    assert src is not None
    key = build_session_key(
        src, group_sessions_per_user=True, thread_sessions_per_user=False
    )
    assert key == "agent:main:discord:thread:1520255822704152666:1520255822704152666"


def test_origin_source_none_when_fields_absent():
    adapter = _make_adapter(_kt_route())
    assert adapter._build_origin_source(None, None, None) is None
    assert adapter._build_origin_source("discord", None, None) is None


def test_origin_source_unknown_platform_falls_back():
    adapter = _make_adapter(_kt_route())
    assert adapter._build_origin_source("nope", "123", "123") is None
