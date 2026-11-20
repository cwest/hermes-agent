"""A webhook route's ``skills:`` lookup must not run on the gateway event loop.

The skill-command scan parses every SKILL.md frontmatter and rescans whenever a skill
directory changes. Run inline in ``_handle_webhook`` it held the loop for more than 10s
during a burst of GitHub deliveries: the Discord heartbeat stalled, every managed
worker's ``worker.persist`` receipt timed out, and their cards gave up together.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


@pytest.mark.asyncio
async def test_route_skill_lookup_runs_off_the_event_loop(monkeypatch):
    import agent.skill_commands as sc_mod

    loop_thread = threading.current_thread()
    scanned_on = []

    def _scan():
        scanned_on.append(threading.current_thread())
        return {"/review-skill": {"name": "review-skill"}}

    monkeypatch.setattr(sc_mod, "get_skill_commands", _scan)
    monkeypatch.setattr(sc_mod, "build_skill_invocation_message",
                        lambda key, user_instruction="": f"SKILL BODY\n{user_instruction}")
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1", "port": 0,
        "routes": {"gh": {"secret": _INSECURE_NO_AUTH, "prompt": "PR: {action}", "skills": ["review-skill"]}},
    }))
    seen = []

    async def _capture(request, route_config, route_name, profile, payload, prompt, event_type, delivery_id, now):
        seen.append(prompt)
        return web.Response(status=202)

    adapter._dispatch_agent_run = _capture
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/webhooks/gh", json={"action": "opened"})
        assert resp.status == 202
        await asyncio.sleep(0)

    assert seen == ["SKILL BODY\nPR: opened"]
    assert scanned_on and loop_thread not in scanned_on
