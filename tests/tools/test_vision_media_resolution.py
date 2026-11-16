"""vision_analyze's ``media_resolution`` cost lever (Gemini/Vertex only).

A coarse question ("what's in this picture?") cost as many image tokens as reading fine print.
Gemini's generateContent exposes ``generationConfig.mediaResolution``. Measured on a 2400x1600
PNG with gemini-3.x flash: default 1,080 IMAGE tokens, ``low`` 260, ``high`` 1,080. So the lever
pays off only in the low direction.

Contract: ``auxiliary.vision.media_resolution`` ("" = provider default) or the per-call
``media_resolution`` arg (wins over config) adds a FLAT ``extra_body={"media_resolution":
"MEDIA_RESOLUTION_LOW"}`` to the auxiliary call. LiteLLM's Vertex adapter maps that flat key onto
generationConfig. It is sent only when the resolved vision route is Gemini-family. Any other
provider, an unknown value, or a resolver failure leaves the request without that field.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import vision_tools
from tools.vision_tools import vision_analyze_tool

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _response(text="ok"):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = text
    return resp


async def _call(tmp_path, *, config_value=None, route=("gemini", "gemini-3-flash"), resolver_error=None,
                **tool_kwargs):
    img = tmp_path / "t.png"
    img.write_bytes(_PNG)
    vision_cfg = {} if config_value is None else {"media_resolution": config_value}
    resolver = (patch("agent.auxiliary_client.resolve_vision_provider_client", side_effect=resolver_error)
                if resolver_error else
                patch("agent.auxiliary_client.resolve_vision_provider_client",
                      return_value=(route[0], object(), route[1])))
    with (
        patch("hermes_cli.config.load_config", return_value={"auxiliary": {"vision": vision_cfg}}),
        patch("tools.vision_tools.async_call_llm", new_callable=AsyncMock, return_value=_response()) as llm,
        resolver,
    ):
        await vision_analyze_tool(str(img), "describe", "some-model", **tool_kwargs)
    return llm.await_args.kwargs


@pytest.mark.asyncio
class TestWire:
    async def test_default_sends_no_extra_body(self, tmp_path):
        assert "extra_body" not in await _call(tmp_path)

    async def test_config_low_on_gemini(self, tmp_path):
        kwargs = await _call(tmp_path, config_value="low")
        assert kwargs["extra_body"] == {"media_resolution": "MEDIA_RESOLUTION_LOW"}

    async def test_per_call_value_beats_config(self, tmp_path):
        kwargs = await _call(tmp_path, config_value="low", media_resolution="high")
        assert kwargs["extra_body"] == {"media_resolution": "MEDIA_RESOLUTION_HIGH"}

    async def test_gemini_model_through_an_aggregator(self, tmp_path):
        kwargs = await _call(tmp_path, config_value="medium", route=("openrouter", "google/gemini-3.6-flash"))
        assert kwargs["extra_body"] == {"media_resolution": "MEDIA_RESOLUTION_MEDIUM"}

    @pytest.mark.parametrize("route", [("anthropic", "claude-opus-4"), ("openrouter", "openai/gpt-5"),
                                       (None, None)])
    async def test_non_gemini_route_is_untouched(self, tmp_path, route):
        assert "extra_body" not in await _call(tmp_path, config_value="low", route=route)

    @pytest.mark.parametrize("value", ["", "default", "ultra", "  ", 7])
    async def test_unset_or_unknown_value_is_untouched(self, tmp_path, value):
        assert "extra_body" not in await _call(tmp_path, config_value=value)

    async def test_resolver_failure_never_breaks_the_call(self, tmp_path):
        assert "extra_body" not in await _call(tmp_path, config_value="low", resolver_error=RuntimeError("boom"))


class TestSchemaAndHandler:
    def test_schema_offers_the_enum(self):
        prop = vision_tools.VISION_ANALYZE_SCHEMA["parameters"]["properties"]["media_resolution"]
        assert prop["enum"] == ["low", "medium", "high"]
        assert "media_resolution" not in vision_tools.VISION_ANALYZE_SCHEMA["parameters"]["required"]

    def test_handler_forwards_the_arg_on_the_aux_path(self, monkeypatch):
        seen = {}

        async def _fake_tool(image_url, prompt, model, task_id=None, region=None, media_resolution=None):
            seen["media_resolution"] = media_resolution
            return "{}"

        monkeypatch.setattr(vision_tools, "_should_use_native_vision_fast_path", lambda: False)
        monkeypatch.setattr(vision_tools, "vision_analyze_tool", _fake_tool)
        asyncio.run(vision_tools._handle_vision_analyze(
            {"image_url": "x.png", "question": "q", "media_resolution": "low"}))
        assert seen["media_resolution"] == "low"

    def test_default_config_key_is_empty(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["auxiliary"]["vision"]["media_resolution"] == ""
