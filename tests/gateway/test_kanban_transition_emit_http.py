"""E2E proof of the 4c emit leg: emit_transition makes a real signed HTTP POST.

Spins a throwaway aiohttp server on a loopback port, points the emitter at it via
cfg, and asserts the POST arrives with the JSON payload + a valid HMAC signature
header. This proves the actual network/signing path (not just the pure decision
function the unit tests cover) without needing a running gateway.

Run with the hermes venv python:
  ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/gateway/test_kanban_transition_emit_http.py -q
(or directly)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

from gateway.kanban_transition_emit import build_transition_payload, emit_transition


def test_emit_transition_posts_signed_payload():
    from aiohttp import web

    received = {}

    async def handler(request):
        body = await request.read()
        received["body"] = body
        received["sig"] = request.headers.get("X-Hub-Signature-256")
        received["kind"] = request.headers.get("X-Kanban-Event")
        received["idem"] = request.headers.get("X-Idempotency-Key")
        # Classify the event the way the real webhook adapter does — header
        # first, then body ``event_type`` / ``type``. The loopback bridge
        # sends no GitHub/GitLab header, so this exercises the body-field
        # contract that lets a route actually dispatch (rather than 200-ignore).
        parsed = json.loads(body)
        received["adapter_event_type"] = (
            request.headers.get("X-GitHub-Event", "")
            or request.headers.get("X-GitLab-Event", "")
            or parsed.get("event_type", "")
            or parsed.get("type", "")
            or "unknown"
        )
        return web.json_response({"ok": True})

    async def scenario():
        app = web.Application()
        app.router.add_post("/webhooks/kanban-transition", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        # Port 0 => OS picks a free loopback port.
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            secret = "proof-secret"
            cfg = {
                "enabled": True,
                "route": "kanban-transition",
                "webhook_host": "127.0.0.1",
                "webhook_port": port,
            }
            payload = build_transition_payload(
                task_id="t_proof", board="default", kind="blocked",
                reason="awaiting-casey-signoff: merge PR #7", event_id=99,
                title="proof card",
            )
            ok = await emit_transition(cfg, payload, secret)
            return ok, secret
        finally:
            await runner.cleanup()

    ok, secret = asyncio.run(scenario())

    assert ok is True, "emit_transition should report success on a 2xx"
    assert received, "the route handler must have received the POST"

    # Payload round-trips.
    got = json.loads(received["body"])
    assert got["task_id"] == "t_proof"
    assert got["kind"] == "blocked"
    assert got["idempotency_key"] == "kanban-transition:default:t_proof:blocked:99"

    # HMAC signature is valid for the received body.
    expected = "sha256=" + hmac.new(
        secret.encode(), received["body"], hashlib.sha256
    ).hexdigest()
    assert received["sig"] == expected, "HMAC signature must validate"
    assert received["kind"] == "blocked"
    assert received["idem"] == "kanban-transition:default:t_proof:blocked:99"
    # The real adapter must be able to classify this as a 'blocked' event from
    # the body alone (no GitHub/GitLab header on a loopback POST). If this is
    # 'unknown', a route filtering on [blocked, completed] would 200-ignore the
    # POST and never spawn the orchestrator run — the production regression.
    assert received["adapter_event_type"] == "blocked", (
        "adapter classified the loopback POST as %r; the route would ignore it"
        % received["adapter_event_type"]
    )
    assert got["event_type"] == "blocked"


if __name__ == "__main__":
    test_emit_transition_posts_signed_payload()
    print("RESULT: PASS — emit_transition POSTs a valid HMAC-signed payload")
