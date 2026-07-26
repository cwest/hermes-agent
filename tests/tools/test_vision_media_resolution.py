"""Tests for the vision_analyze media_resolution cost lever.

Covers the four contracts from the feature spec:
  1. Default behavior is UNCHANGED when nothing is set (no extra_body field).
  2. The config value (auxiliary.vision.media_resolution) is applied.
  3. A per-call override beats the config value.
  4. A non-supporting provider path cleanly IGNORES the setting (no-op,
     never errors, never sends an unknown field).

The knob maps low/medium/high -> Gemini generationConfig.mediaResolution
(MEDIA_RESOLUTION_LOW/MEDIUM/HIGH) and is injected ONLY for the Gemini/Vertex
provider family; every other provider ignores it.
"""

import os
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.vision_tools import (
    _build_media_resolution_extra_body,
    _normalize_media_resolution,
    _resolve_media_resolution,
    vision_analyze_tool,
)


# ---------------------------------------------------------------------------
# _normalize_media_resolution — value normalization
# ---------------------------------------------------------------------------


class TestNormalizeMediaResolution:
    def test_none_is_default_noop(self):
        assert _normalize_media_resolution(None) is None

    def test_empty_string_is_default_noop(self):
        assert _normalize_media_resolution("") is None
        assert _normalize_media_resolution("   ") is None

    def test_literal_default_is_noop(self):
        assert _normalize_media_resolution("default") is None
        assert _normalize_media_resolution("DEFAULT") is None
        assert _normalize_media_resolution("unset") is None

    def test_low_maps_to_gemini_enum(self):
        assert _normalize_media_resolution("low") == "MEDIA_RESOLUTION_LOW"
        assert _normalize_media_resolution("LOW") == "MEDIA_RESOLUTION_LOW"
        assert _normalize_media_resolution(" low ") == "MEDIA_RESOLUTION_LOW"

    def test_medium_maps_to_gemini_enum(self):
        assert _normalize_media_resolution("medium") == "MEDIA_RESOLUTION_MEDIUM"

    def test_high_maps_to_gemini_enum(self):
        assert _normalize_media_resolution("high") == "MEDIA_RESOLUTION_HIGH"

    def test_already_gemini_enum_passthrough(self):
        assert (
            _normalize_media_resolution("MEDIA_RESOLUTION_LOW")
            == "MEDIA_RESOLUTION_LOW"
        )

    def test_unknown_value_is_noop(self):
        """An unrecognized value must not send garbage — treat as default."""
        assert _normalize_media_resolution("ultra") is None
        assert _normalize_media_resolution("banana") is None


# ---------------------------------------------------------------------------
# _build_media_resolution_extra_body — provider-aware, fail-soft injection
# ---------------------------------------------------------------------------


class TestBuildMediaResolutionExtraBody:
    def test_default_unchanged_when_none(self):
        """Contract 1: no setting -> empty dict, byte-for-byte unchanged calls."""
        assert _build_media_resolution_extra_body("gemini", "gemini-3-flash", None) == {}

    def test_default_unchanged_when_literal_default(self):
        assert (
            _build_media_resolution_extra_body("gemini", "gemini-3-flash", "default")
            == {}
        )

    def test_gemini_provider_low_injects_flat_field(self):
        """Contract 2: gemini provider + low -> the flat media_resolution param."""
        out = _build_media_resolution_extra_body("gemini", "gemini-3-flash", "low")
        assert out == {"media_resolution": "MEDIA_RESOLUTION_LOW"}

    def test_gemini_provider_high_injects_high_enum(self):
        out = _build_media_resolution_extra_body("gemini", "gemini-3-flash", "high")
        assert out["media_resolution"] == "MEDIA_RESOLUTION_HIGH"

    def test_vertex_gemini_model_supported(self):
        """A Gemini model reached via an aggregator/vertex still supports it."""
        out = _build_media_resolution_extra_body(
            "vertex", "google/gemini-3-flash-preview", "low"
        )
        assert out["media_resolution"] == "MEDIA_RESOLUTION_LOW"

    def test_gemini_model_via_openrouter_supported(self):
        out = _build_media_resolution_extra_body(
            "openrouter", "google/gemini-3-flash-preview", "low"
        )
        assert out == {"media_resolution": "MEDIA_RESOLUTION_LOW"}

    def test_non_gemini_provider_is_noop(self):
        """Contract 4: an OpenAI/anthropic path must NOT send the field."""
        assert _build_media_resolution_extra_body("openai", "gpt-4o", "low") == {}
        assert (
            _build_media_resolution_extra_body("anthropic", "claude-opus-4", "low")
            == {}
        )

    def test_non_gemini_model_on_aggregator_is_noop(self):
        """openrouter routing a non-Gemini model must ignore the knob."""
        assert (
            _build_media_resolution_extra_body("openrouter", "openai/gpt-4o", "low")
            == {}
        )

    def test_unknown_provider_is_noop(self):
        assert _build_media_resolution_extra_body("somebackend", "some-model", "low") == {}

    def test_never_raises_on_bad_input(self):
        """Fail-soft: junk input returns {} instead of raising."""
        # No provider at all -> can't be Gemini -> no-op.
        assert _build_media_resolution_extra_body(None, None, "low") == {}
        # A direct 'gemini' provider qualifies even with an unknown model name.
        out = _build_media_resolution_extra_body("gemini", None, "low")
        assert out["media_resolution"] == "MEDIA_RESOLUTION_LOW"


# ---------------------------------------------------------------------------
# _resolve_media_resolution — config + per-call override precedence
# ---------------------------------------------------------------------------


class TestResolveMediaResolution:
    def test_no_config_no_override_is_none(self):
        """Contract 1: nothing set anywhere -> None (unchanged behavior)."""
        with patch("tools.vision_tools.load_config", return_value={}):
            assert _resolve_media_resolution(None) is None

    def test_config_value_applied(self):
        """Contract 2: config value flows through when no override."""
        cfg = {"auxiliary": {"vision": {"media_resolution": "low"}}}
        with patch("tools.vision_tools.load_config", return_value=cfg):
            assert _resolve_media_resolution(None) == "low"

    def test_override_beats_config(self):
        """Contract 3: per-call override wins over the config value."""
        cfg = {"auxiliary": {"vision": {"media_resolution": "low"}}}
        with patch("tools.vision_tools.load_config", return_value=cfg):
            assert _resolve_media_resolution("high") == "high"

    def test_override_used_when_no_config(self):
        with patch("tools.vision_tools.load_config", return_value={}):
            assert _resolve_media_resolution("low") == "low"

    def test_config_read_failure_falls_back_to_override(self):
        """A broken config read must not break the tool — fail-soft to override."""
        with patch(
            "tools.vision_tools.load_config", side_effect=RuntimeError("boom")
        ):
            assert _resolve_media_resolution("low") == "low"
            assert _resolve_media_resolution(None) is None


# ---------------------------------------------------------------------------
# E2E: vision_analyze_tool wires media_resolution into the real call kwargs
# ---------------------------------------------------------------------------


def _gemini_route(provider="gemini", model="gemini-3-flash"):
    """Patch resolve_vision_provider_client to report a Gemini route."""
    return patch(
        "agent.auxiliary_client.resolve_vision_provider_client",
        return_value=(provider, object(), model),
    )


def _mock_llm_response(text="ok"):
    resp = MagicMock()
    choice = MagicMock()
    choice.message.content = text
    resp.choices = [choice]
    return resp


class TestVisionAnalyzeToolWiring:
    """Exercise the real vision_analyze_tool path end-to-end (mock only the
    network LLM call + provider resolution), asserting the extra_body contract.
    """

    @pytest.mark.asyncio
    async def test_default_sends_no_extra_body(self, tmp_path):
        """Contract 1: with no config/override, the call is byte-for-byte the
        same as before — NO extra_body key is added."""
        img = tmp_path / "t.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        with (
            patch("tools.vision_tools.load_config", return_value={"auxiliary": {"vision": {}}}),
            patch("tools.vision_tools._image_to_base64_data_url", return_value="data:image/png;base64,abc"),
            patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock, return_value=_mock_llm_response()) as mock_llm,
            _gemini_route(),
        ):
            await vision_analyze_tool(str(img), "describe", "gemini-3-flash")
        assert "extra_body" not in mock_llm.await_args.kwargs

    @pytest.mark.asyncio
    async def test_config_low_on_gemini_injects_extra_body(self, tmp_path):
        """Contract 2: config media_resolution=low on a Gemini route sends the
        flat media_resolution extra_body field."""
        img = tmp_path / "t.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        cfg = {"auxiliary": {"vision": {"media_resolution": "low"}}}
        with (
            patch("tools.vision_tools.load_config", return_value=cfg),
            patch("tools.vision_tools._image_to_base64_data_url", return_value="data:image/png;base64,abc"),
            patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock, return_value=_mock_llm_response()) as mock_llm,
            _gemini_route(),
        ):
            await vision_analyze_tool(str(img), "describe", "gemini-3-flash")
        eb = mock_llm.await_args.kwargs["extra_body"]
        assert eb == {"media_resolution": "MEDIA_RESOLUTION_LOW"}

    @pytest.mark.asyncio
    async def test_per_call_override_beats_config(self, tmp_path):
        """Contract 3: an explicit media_resolution arg overrides the config."""
        img = tmp_path / "t.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        cfg = {"auxiliary": {"vision": {"media_resolution": "low"}}}
        with (
            patch("tools.vision_tools.load_config", return_value=cfg),
            patch("tools.vision_tools._image_to_base64_data_url", return_value="data:image/png;base64,abc"),
            patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock, return_value=_mock_llm_response()) as mock_llm,
            _gemini_route(),
        ):
            await vision_analyze_tool(str(img), "describe", "gemini-3-flash", media_resolution="high")
        eb = mock_llm.await_args.kwargs["extra_body"]
        assert eb == {"media_resolution": "MEDIA_RESOLUTION_HIGH"}

    @pytest.mark.asyncio
    async def test_non_gemini_provider_is_noop(self, tmp_path):
        """Contract 4: config set to low but the route is OpenAI — no extra_body,
        the setting is cleanly ignored (no error, no unknown field)."""
        img = tmp_path / "t.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        cfg = {"auxiliary": {"vision": {"media_resolution": "low"}}}
        with (
            patch("tools.vision_tools.load_config", return_value=cfg),
            patch("tools.vision_tools._image_to_base64_data_url", return_value="data:image/png;base64,abc"),
            patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock, return_value=_mock_llm_response()) as mock_llm,
            _gemini_route(provider="openai", model="gpt-4o"),
        ):
            result = json.loads(await vision_analyze_tool(str(img), "describe", "gpt-4o"))
        assert result["success"] is True
        assert "extra_body" not in mock_llm.await_args.kwargs

    @pytest.mark.asyncio
    async def test_provider_resolution_failure_does_not_break_call(self, tmp_path):
        """Fail-soft: if provider resolution blows up, the vision call still
        succeeds (just without the knob)."""
        img = tmp_path / "t.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        cfg = {"auxiliary": {"vision": {"media_resolution": "low"}}}
        with (
            patch("tools.vision_tools.load_config", return_value=cfg),
            patch("tools.vision_tools._image_to_base64_data_url", return_value="data:image/png;base64,abc"),
            patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock, return_value=_mock_llm_response()) as mock_llm,
            patch("agent.auxiliary_client.resolve_vision_provider_client", side_effect=RuntimeError("resolve boom")),
        ):
            result = json.loads(await vision_analyze_tool(str(img), "describe", "gemini-3-flash"))
        assert result["success"] is True
        assert "extra_body" not in mock_llm.await_args.kwargs
