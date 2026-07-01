"""E2E proof (card t_dfafdefd): a probe lane-move yields a banner-led,
greppable woken-turn message, unambiguous vs any other message.

This wires the REAL emit leg to the REAL webhook route end-to-end:

    build_transition_payload (emitter, stamps the unique wake_banner)
        -> emit_transition (signed loopback HTTP POST)
        -> WebhookAdapter._handle_webhook (the live route)
        -> the woken run's prompt (captured)

and asserts the captured prompt's FIRST line is the exact unique banner and that
the banner is greppable by its marker — the whole point of the card. It exercises
the actual network/signing path and the actual route handler, not a mock of
either.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from gateway.config import PlatformConfig
from gateway.kanban_transition_emit import (
    WAKE_BANNER_PREFIX,
    build_transition_payload,
    emit_transition,
)
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


@pytest.mark.asyncio
async def test_probe_lane_move_yields_greppable_banner_led_woken_message():
    # A probe card + a lane move (ready -> review), exactly as a status_changed
    # transition event carries {"from": "ready", "to": "review"}.
    secret = "e2e-secret"
    task_id = "t_probeE2E"
    event_id = 314159
    payload = build_transition_payload(
        task_id=task_id,
        board="default",
        kind="status_changed",
        reason="",
        event_id=event_id,
        title="probe: lane move E2E",
        from_lane="ready",
        to_lane="review",
    )
    expected_banner = (
        f"AUTONOMOUS-WAKE {task_id} status_changed ready->review evt={event_id}"
    )
    assert payload["wake_banner"] == expected_banner

    # A real webhook adapter with the kanban-transition route (HMAC via the
    # emitter's real signature — the route validates the same secret).
    extra = {
        "host": "127.0.0.1",
        "port": 0,
        "routes": {
            "kanban-transition": {
                "secret": secret,
                "prompt": (
                    "{wake_banner}\n\nBoard: {board}\nTask: {task_id}\n"
                    "Lane: {from_lane} -> {to_lane}"
                ),
            }
        },
        "rate_limit": 1000,
        "max_body_bytes": 1_048_576,
    }
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra=extra))

    captured = {}

    async def _capture(event):
        captured["text"] = event.text

    adapter.handle_message = AsyncMock(side_effect=_capture)

    # Stand up the real route on a loopback port and drive the REAL emitter POST.
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        cfg = {
            "enabled": True,
            "route": "kanban-transition",
            "webhook_host": "127.0.0.1",
            "webhook_port": port,
        }
        ok = await emit_transition(cfg, payload, secret)
        assert ok is True, "emit_transition should report success on a 2xx"
        # The route dispatches handle_message in a background task; give it a beat.
        for _ in range(50):
            if "text" in captured:
                break
            await asyncio.sleep(0.02)
    finally:
        await runner.cleanup()

    woken = captured.get("text")
    assert woken, "the woken run must have received a prompt"

    # THE CARD CONTRACT: the first line of the woken message is the unique banner.
    first_line = woken.splitlines()[0]
    assert first_line == expected_banner, (
        "woken message first line %r != unique wake banner %r"
        % (first_line, expected_banner)
    )
    # Greppable by its unique marker, unambiguous vs any other message.
    assert WAKE_BANNER_PREFIX in woken
    assert f"evt={event_id}" in woken
    assert "None" not in first_line

    # Emit greppable E2E evidence to stdout (captured on the card).
    print("E2E-EVIDENCE woken-first-line: " + first_line)


if __name__ == "__main__":
    asyncio.run(
        test_probe_lane_move_yields_greppable_banner_led_woken_message()
    )
    print("RESULT: PASS — probe lane-move yields a greppable banner-led message")
