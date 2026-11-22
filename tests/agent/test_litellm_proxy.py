"""A LiteLLM proxy on a local address is recognized by what it serves, not by its name.

The planner's LiteLLM branch keyed on a ``litellm`` token in the provider id or hostname, so the
common install shape (``provider: custom``, ``base_url: http://127.0.0.1:4000/v1``) never got
``cache_control`` markers for Claude and paid full input price on every turn. A local endpoint is
now probed once at LiteLLM's unauthenticated ``/health/liveliness`` route.

The servers here are real HTTP servers on 127.0.0.1 so the probe runs through its real transport.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agent.litellm_proxy as lp
import agent.model_metadata as mm
from agent.agent_runtime_helpers import anthropic_prompt_cache_policy
from agent.prompt_caching import build_prompt_cache_plan, envelope_tool_part_cache_markers_supported


@contextmanager
def _server(routes: dict[str, tuple[int, bytes]]):
    """A local HTTP server answering ``routes`` ({path: (status, body)}), 404 otherwise; yields
    (base_url, hits) where ``hits`` lists every path requested."""
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            status, body = routes.get(self.path, (404, b'{"detail":"Not Found"}'))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/v1", hits
    finally:
        httpd.shutdown()
        httpd.server_close()


LITELLM = {"/health/liveliness": (200, b'"I\'m alive!"')}


@pytest.fixture(autouse=True)
def _fresh_probe_caches():
    lp._probe_cache.clear()
    mm._endpoint_blackhole_cache.clear()
    yield
    lp._probe_cache.clear()


def _agent(base_url: str, model: str = "claude-opus-5-5", provider: str = "custom",
           api_mode: str = "chat_completions"):
    return SimpleNamespace(provider=provider, base_url=base_url, api_mode=api_mode, model=model,
                           _custom_providers=[], api_key="", _cache_disabled=False)


class TestIsLitellmProxy:
    def test_local_litellm_server_is_recognized(self):
        with _server(LITELLM) as (base_url, hits):
            assert lp.is_litellm_proxy(base_url) is True
        assert hits == ["/health/liveliness"]

    def test_local_server_without_the_route_is_not_litellm(self):
        with _server({}) as (base_url, _):
            assert lp.is_litellm_proxy(base_url) is False

    @pytest.mark.parametrize("body", [b'{"status": "ok"}', b'"healthy"', b"I'm alive and well, says vLLM"])
    def test_other_liveness_bodies_are_not_litellm(self, body):
        with _server({"/health/liveliness": (200, body)}) as (base_url, _):
            assert lp.is_litellm_proxy(base_url) is False

    def test_unreachable_local_port_is_not_litellm(self):
        with _server({}) as (base_url, _):
            pass  # closed: nothing listens on the port any more
        assert lp.is_litellm_proxy(base_url) is False

    def test_verdict_is_cached_per_server(self):
        with _server(LITELLM) as (base_url, hits):
            assert lp.is_litellm_proxy(base_url) is True
            assert lp.is_litellm_proxy(base_url.removesuffix("/v1")) is True
            assert lp.is_litellm_proxy(base_url.replace("127.0.0.1", "localhost")) is True
        assert hits == ["/health/liveliness"]

    def test_positive_verdict_survives_a_new_process(self):
        with _server(LITELLM) as (base_url, hits):
            assert lp.is_litellm_proxy(base_url) is True
            lp._probe_cache.clear()  # what a fresh process starts with
            assert lp.is_litellm_proxy(base_url) is True
        assert hits == ["/health/liveliness"]

    def test_negative_verdict_expires(self):
        with _server({}) as (base_url, hits):
            assert lp.is_litellm_proxy(base_url) is False
            server = mm._server_root(base_url)
            verdict, at = lp._probe_cache[server]
            lp._probe_cache[server] = (verdict, at - mm._ENDPOINT_PROBE_FAILURE_TTL_SECONDS - 1)
            assert lp.is_litellm_proxy(base_url) is False
        assert hits == ["/health/liveliness", "/health/liveliness"]

    @pytest.mark.parametrize("base_url", [
        "https://api.openai.com/v1", "https://api.anthropic.com", "https://gateway.example.com/v1", "",
    ])
    def test_hosted_and_blank_endpoints_are_never_probed(self, base_url):
        with patch("httpx.Client", side_effect=AssertionError("hosted endpoint was probed")):
            assert lp.is_litellm_proxy(base_url) is False


class TestCachePolicyOnLoopbackLitellm:
    def test_claude_on_loopback_litellm_gets_envelope_markers(self):
        with _server(LITELLM) as (base_url, _):
            for provider in ("custom", "custom:vertex-llm-proxy"):
                assert anthropic_prompt_cache_policy(_agent(base_url, provider=provider)) == (True, False)

    def test_claude_on_loopback_non_litellm_stays_unmarked(self):
        with _server({}) as (base_url, _):
            assert anthropic_prompt_cache_policy(_agent(base_url)) == (False, False)

    def test_non_claude_on_loopback_litellm_stays_unmarked(self):
        with _server(LITELLM) as (base_url, hits):
            assert anthropic_prompt_cache_policy(_agent(base_url, model="gemini-4-neon")) == (False, False)
        assert hits == []  # the probe only runs where its answer could change the verdict

    def test_operator_disable_still_wins(self):
        with _server(LITELLM) as (base_url, _):
            agent = _agent(base_url)
            agent._cache_disabled = True
            assert anthropic_prompt_cache_policy(agent) == (False, False)

    def test_explicit_prompt_caching_false_still_wins(self):
        with _server(LITELLM) as (base_url, _):
            agent = _agent(base_url, provider="custom:vertex-llm-proxy")
            agent._custom_providers = [{"name": "vertex-llm-proxy", "base_url": base_url,
                                        "models": {"claude-opus-5-5": {"prompt_caching": False}}}]
            assert anthropic_prompt_cache_policy(agent) == (False, False)

    @pytest.mark.parametrize("api_mode", ["codex_responses", "bedrock_converse"])
    def test_other_transports_stay_unmarked(self, api_mode):
        with _server(LITELLM) as (base_url, _):
            assert anthropic_prompt_cache_policy(_agent(base_url, api_mode=api_mode)) == (False, False)


class TestToolPartGuardOnLoopbackLitellm:
    """A detected LiteLLM proxy also gets the #89886 guard: no part-level markers on role:tool."""

    def test_loopback_litellm_suppresses_tool_part_markers(self):
        with _server(LITELLM) as (base_url, _):
            assert envelope_tool_part_cache_markers_supported("custom", base_url) is False

    def test_loopback_non_litellm_keeps_tool_part_markers(self):
        with _server({}) as (base_url, _):
            assert envelope_tool_part_cache_markers_supported("custom", base_url) is True

    def test_planned_request_carries_markers_but_none_on_tool_parts(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "run the tool"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "tool output"},
        ]
        with _server(LITELLM) as (base_url, _):
            should_cache, native = anthropic_prompt_cache_policy(_agent(base_url))
            assert should_cache
            plan = build_prompt_cache_plan(
                messages, None, cache_ttl="5m", native_anthropic=native,
                tool_part_markers=envelope_tool_part_cache_markers_supported("custom", base_url),
            )
        marked = [m for m in plan.messages if "cache_control" in str(m)]
        assert marked, "no cache_control marker was planned"
        tool = next(m for m in plan.messages if m["role"] == "tool")
        parts = tool["content"] if isinstance(tool["content"], list) else []
        assert not any(isinstance(p, dict) and "cache_control" in p for p in parts)
