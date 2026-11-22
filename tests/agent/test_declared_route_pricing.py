"""A custom endpoint (a local LLM proxy) that publishes no prices can declare them per model in
config: ``providers.<id>.models.<model>.pricing``. The declaration is scoped to the provider's
route, like every other per-model key, and prices each token bucket at its own rate."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

import hermes_yaml as yaml
from agent.usage_pricing import CanonicalUsage, estimate_usage_cost, get_pricing_entry

PROXY = "http://127.0.0.1:4000/v1"
OPUS_RATES = {"input": "4.00", "output": "20.00", "cache_read": "0.20", "cache_write": "5.00"}


@pytest.fixture
def proxy_home(tmp_path, monkeypatch):
    def write(models):
        config = {
            "model": {"default": "claude-opus-5-5", "provider": "custom", "base_url": PROXY},
            "providers": {"local-proxy": {"api": PROXY + "/", "api_key": "sk-local", "models": models}},
        }
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # The proxy's own /models carries no prices, which is why the declaration exists.
    monkeypatch.setattr("agent.usage_pricing.fetch_endpoint_model_metadata", lambda *_a, **_k: {})
    write({"claude-opus-5-5": {"prompt_caching": True, "pricing": OPUS_RATES}})
    return write


def test_declared_prices_estimate_every_bucket_on_the_custom_route(proxy_home):
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000,
                           cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)

    result = estimate_usage_cost("claude-opus-5-5", usage, provider="custom", base_url=PROXY)

    assert result.status == "estimated"
    assert result.source == "user_override"
    assert result.amount_usd == Decimal("4.00") + Decimal("20.00") + Decimal("0.20") + Decimal("5.00")


def test_cache_reads_bill_at_the_cache_rate_not_the_input_rate(proxy_home):
    cached = estimate_usage_cost("claude-opus-5-5", CanonicalUsage(cache_read_tokens=1_000_000),
                                 provider="custom", base_url=PROXY)
    uncached = estimate_usage_cost("claude-opus-5-5", CanonicalUsage(input_tokens=1_000_000),
                                   provider="custom", base_url=PROXY)

    assert cached.amount_usd == Decimal("0.20")
    assert uncached.amount_usd == Decimal("4.00")


def test_named_custom_provider_route_reads_the_same_declaration(proxy_home):
    # Delegated children run as ``custom:<name>`` against the same URL.
    result = estimate_usage_cost("claude-opus-5-5", CanonicalUsage(output_tokens=1_000_000),
                                 provider="custom:local-proxy", base_url=PROXY)

    assert result.amount_usd == Decimal("20.00")


def test_declaration_does_not_leak_to_another_route_or_model(proxy_home):
    assert get_pricing_entry("claude-opus-5-5", provider="custom", base_url="http://127.0.0.1:9999/v1") is None
    assert get_pricing_entry("claude-sonnet-5-5", provider="custom", base_url=PROXY) is None


@pytest.mark.parametrize("bad", ["-1", "NaN", "Infinity", "lots"])
def test_an_unusable_declared_rate_leaves_the_route_unpriced(proxy_home, bad):
    proxy_home({"claude-opus-5-5": {"pricing": {**OPUS_RATES, "cache_read": bad}}})

    result = estimate_usage_cost("claude-opus-5-5", CanonicalUsage(input_tokens=10), provider="custom", base_url=PROXY)

    assert result.status == "unknown"
    assert result.amount_usd is None


def test_a_bucket_without_a_declared_rate_is_unknown_when_used(proxy_home):
    proxy_home({"claude-opus-5-5": {"pricing": {"input": "4.00", "output": "20.00"}}})

    priced = estimate_usage_cost("claude-opus-5-5", CanonicalUsage(input_tokens=10, output_tokens=10),
                                 provider="custom", base_url=PROXY)
    unpriced = estimate_usage_cost("claude-opus-5-5", CanonicalUsage(input_tokens=10, cache_read_tokens=10),
                                   provider="custom", base_url=PROXY)

    assert priced.status == "estimated"
    assert unpriced.status == "unknown"


def test_a_session_on_the_custom_provider_records_a_nonzero_estimate(proxy_home):
    from agent import turn_usage
    from run_agent import AIAgent

    agent = AIAgent(api_key="sk-local", base_url=PROXY, provider="custom", api_mode="chat_completions",
                    model="claude-opus-5-5", session_id="t", platform="cli", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, save_trajectories=False, enabled_toolsets=["file"])
    usage = SimpleNamespace(prompt_tokens=50_000, completion_tokens=1_000, total_tokens=51_000,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=40_000, cache_write_tokens=0),
                            completion_tokens_details=None)
    try:
        turn_usage.record_response_usage(
            agent, SimpleNamespace(usage=usage, id=None, model="claude-opus-5-5"),
            messages=[{"role": "user", "content": "hi"}], api_call_count=1, api_duration=0.1,
            compression_attempts=0, max_compression_attempts=3)
        cost, status = agent.session_estimated_cost_usd, agent.session_cost_status
    finally:
        agent.close()

    assert status == "estimated"
    # 10K uncached input at $4/M + 40K cache reads at $0.20/M + 1K output at $20/M.
    assert cost == pytest.approx(0.04 + 0.008 + 0.02)
