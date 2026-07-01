"""Tests: a kanban-transition wake leads its prompt with the self-announce banner.

Card t_dfafdefd: the woken turn must be UNMISTAKABLE. The emitter stamps a
unique, greppable ``wake_banner`` into the POST body; the webhook route must
guarantee the woken run's prompt LEADS with that exact banner so the
orchestrator's in-thread reply opens with it verbatim — regardless of what the
route's ``prompt`` template happens to contain. This closes the ambiguity that
let a 202-dispatched wake be mistaken for a late-delivered prior reply.

The prepend is scoped to payloads that carry ``wake_banner`` (the kanban
transition emitter), so ordinary GitHub/monitoring webhooks are untouched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from gateway.kanban_transition_emit import WAKE_BANNER_PREFIX
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


def _kt_route(prompt="A card transitioned. Title: {title}"):
    return {"kanban-transition": {"secret": _INSECURE_NO_AUTH, "prompt": prompt}}


async def _post_and_capture(adapter, body):
    captured = {}

    async def _capture(event):
        captured["event"] = event

    adapter.handle_message = AsyncMock(side_effect=_capture)
    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await cli.post("/webhooks/kanban-transition", json=body)
        assert resp.status == 202
    adapter.handle_message.assert_awaited_once()
    return captured["event"].text


@pytest.mark.asyncio
async def test_woken_prompt_first_line_is_the_wake_banner():
    adapter = _make_adapter(_kt_route())
    banner = "AUTONOMOUS-WAKE t_abc status_changed ready->review evt=17"
    text = await _post_and_capture(
        adapter,
        {
            "event_type": "status_changed",
            "task_id": "t_abc",
            "title": "probe card",
            "wake_banner": banner,
        },
    )
    # The very first line of what the woken run receives IS the banner.
    assert text.splitlines()[0] == banner
    assert text.startswith(WAKE_BANNER_PREFIX)
    # The template context still follows the banner (banner is additive).
    assert "probe card" in text


@pytest.mark.asyncio
async def test_banner_prepended_even_with_empty_prompt_template():
    adapter = _make_adapter(_kt_route(prompt=""))
    banner = "AUTONOMOUS-WAKE t_z blocked ?->? evt=5"
    text = await _post_and_capture(
        adapter,
        {
            "event_type": "blocked",
            "task_id": "t_z",
            "wake_banner": banner,
        },
    )
    assert text.splitlines()[0] == banner


@pytest.mark.asyncio
async def test_non_transition_webhook_is_not_prepended():
    # A payload WITHOUT wake_banner (ordinary GitHub/monitoring webhook) is
    # delivered unchanged — no banner leaks into unrelated routes.
    adapter = _make_adapter(_kt_route(prompt="plain: {title}"))
    text = await _post_and_capture(
        adapter,
        {"event_type": "push", "title": "some push"},
    )
    assert not text.startswith(WAKE_BANNER_PREFIX)
    assert text == "plain: some push"
