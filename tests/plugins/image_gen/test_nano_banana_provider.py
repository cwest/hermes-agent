#!/usr/bin/env python3
"""Tests for the nano-banana image gen backend (Gemini image via local proxy)."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# The plugin directory uses a hyphen (matches the backend name + the shipped
# openai-codex convention), which is not a valid dotted-import identifier. Load
# it via importlib; patch attributes on the module object directly.
nb = importlib.import_module("plugins.image_gen.nano-banana")

_PNG_DATA_URI = "data:image/png;base64,dGVzdC1pbWFnZS1kYXRh"  # "test-image-data"


def _runtime_ok(**over):
    base = {
        "provider": "custom:vertex-llm-proxy",
        "api_mode": "chat_completions",
        "base_url": "http://127.0.0.1:4000/v1",
        "api_key": "sk-local",
        "source": "config",
    }
    base.update(over)
    return base


def _mock_chat_response(images, *, content=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "images": [
                        {"type": "image_url", "image_url": {"url": u}} for u in images
                    ],
                }
            }
        ]
    }
    return resp


def _provider():
    return nb.NanoBananaImageGenProvider()


def _patch_runtime(**over):
    return patch.object(nb, "resolve_runtime_provider", return_value=_runtime_ok(**over))


def _patch_runtime_error():
    return patch.object(nb, "resolve_runtime_provider", side_effect=RuntimeError("boom"))


def _patch_cfg(cfg):
    return patch.object(nb, "_load_image_gen_config", return_value=cfg)


def _patch_save(path="/tmp/nano_banana_gen.png"):
    return patch.object(nb, "save_b64_image", return_value=Path(path))


# ---------------------------------------------------------------------------
# Provider identity + capabilities
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_name(self):
        assert _provider().name == "nano-banana"

    def test_display_name(self):
        assert _provider().display_name

    def test_capabilities_support_image_input(self):
        caps = _provider().capabilities()
        assert "text" in caps["modalities"]
        assert "image" in caps["modalities"]
        assert caps["max_reference_images"] >= 1

    def test_default_model_is_pro(self):
        assert nb.DEFAULT_MODEL == "gemini-3-pro-image"
        with _patch_cfg({}):
            assert _provider().default_model() == "gemini-3-pro-image"

    def test_flash_is_in_catalog(self):
        ids = {m["id"] for m in _provider().list_models()}
        assert "gemini-3-pro-image" in ids
        assert "gemini-3.1-flash-image" in ids

    def test_lite_slot_documented_zero_code(self):
        """Lite is a config-driven slot: selecting it via config resolves to
        exactly that id with no code change (proves no two-model hard-coding)."""
        with _patch_cfg({"nano-banana": {"model": "gemini-3.1-flash-lite-image"}}):
            assert _provider()._resolve_model() == "gemini-3.1-flash-lite-image"


# ---------------------------------------------------------------------------
# Model precedence
# ---------------------------------------------------------------------------


class TestModelPrecedence:
    def test_default_when_unset(self):
        with _patch_cfg({}):
            assert _provider()._resolve_model() == "gemini-3-pro-image"

    def test_kwarg_wins(self):
        cfg = {"nano-banana": {"model": "gemini-3.1-flash-image"}, "model": "gemini-3-pro-image"}
        with _patch_cfg(cfg):
            assert _provider()._resolve_model("gemini-3.1-flash-lite-image") == "gemini-3.1-flash-lite-image"

    def test_env_wins_over_config(self, monkeypatch):
        monkeypatch.setenv("NANO_BANANA_IMAGE_MODEL", "gemini-3.1-flash-image")
        with _patch_cfg({"nano-banana": {"model": "gemini-3-pro-image"}}):
            assert _provider()._resolve_model() == "gemini-3.1-flash-image"

    def test_scoped_config_wins_over_top_level(self):
        cfg = {"nano-banana": {"model": "gemini-3.1-flash-image"}, "model": "gemini-3-pro-image"}
        with _patch_cfg(cfg):
            assert _provider()._resolve_model() == "gemini-3.1-flash-image"

    def test_top_level_config_used_when_known_id(self):
        with _patch_cfg({"model": "gemini-3.1-flash-image"}):
            assert _provider()._resolve_model() == "gemini-3.1-flash-image"

    def test_top_level_config_ignored_when_unknown_id(self):
        """A top-level image_gen.model belonging to another backend (e.g. an
        openai/ id) must not hijack nano-banana — fall through to default."""
        with _patch_cfg({"model": "openai/gpt-image-2"}):
            assert _provider()._resolve_model() == "gemini-3-pro-image"


# ---------------------------------------------------------------------------
# Availability / graceful degradation
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_available_with_key(self):
        with _patch_runtime():
            assert _provider().is_available() is True

    def test_unavailable_without_key(self):
        with _patch_runtime(api_key=""):
            assert _provider().is_available() is False

    def test_unavailable_on_resolution_error(self):
        with _patch_runtime_error():
            assert _provider().is_available() is False


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_missing_credentials(self):
        with _patch_runtime(api_key=""):
            result = _provider().generate(prompt="a banana")
        assert result["success"] is False
        assert result["error_type"] == "missing_api_key"

    def test_success_data_uri(self):
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])), \
             _patch_save() as mock_save:
            result = _provider().generate(prompt="a banana")
        assert result["success"] is True
        assert result["image"] == "/tmp/nano_banana_gen.png"
        assert result["provider"] == "nano-banana"
        assert result["model"] == "gemini-3-pro-image"
        mock_save.assert_called_once()

    def test_reads_image_from_choices_message_images(self):
        """The image MUST be read from choices[0].message.images[0].image_url.url
        (content is null on this protocol)."""
        resp = _mock_chat_response([_PNG_DATA_URI], content=None)
        with _patch_runtime(), \
             patch("requests.post", return_value=resp), \
             _patch_save("/tmp/x.png"):
            result = _provider().generate(prompt="a banana")
        assert result["success"] is True

    def test_empty_images_is_empty_response(self):
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([])):
            result = _provider().generate(prompt="a banana")
        assert result["success"] is False
        assert result["error_type"] == "empty_response"

    def test_payload_shape_text_to_image(self):
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="a banana", aspect_ratio="portrait")
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "gemini-3-pro-image"
        assert payload["modalities"] == ["image", "text"]
        assert payload["image_config"]["aspect_ratio"] == "9:16"
        content = payload["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "a banana"}
        assert all(c["type"] != "image_url" for c in content)

    def test_posts_to_resolved_base_url(self):
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="a banana")
        url = mock_post.call_args[0][0]
        assert url == "http://127.0.0.1:4000/v1/chat/completions"

    def test_auth_header_bearer_token(self):
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="a banana")
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-local"

    def test_edit_routing_attaches_image_url(self, tmp_path):
        src = tmp_path / "src.png"
        src.write_bytes(b"\x89PNG\r\n")
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             _patch_save("/tmp/x.png"):
            result = _provider().generate(prompt="make it red", image_url=str(src))
        assert result["success"] is True
        assert result["modality"] == "image"
        content = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
        image_parts = [c for c in content if c["type"] == "image_url"]
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_multiple_references_clamped(self, tmp_path):
        refs = []
        for i in range(nb._MAX_REFERENCE_IMAGES + 2):
            f = tmp_path / f"r{i}.png"
            f.write_bytes(b"\x89PNG\r\n")
            refs.append(str(f))
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="keep the character", reference_image_urls=refs)
        content = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
        image_parts = [c for c in content if c["type"] == "image_url"]
        assert len(image_parts) == nb._MAX_REFERENCE_IMAGES

    def test_http_error_is_api_error(self):
        import requests as req_lib

        resp = MagicMock()
        resp.status_code = 500
        resp.text = "server error"
        resp.json.return_value = {"error": {"message": "boom"}}
        resp.raise_for_status.side_effect = req_lib.HTTPError(response=resp)
        with _patch_runtime(), patch("requests.post", return_value=resp):
            result = _provider().generate(prompt="a banana")
        assert result["success"] is False
        assert result["error_type"] == "api_error"

    def test_timeout(self):
        import requests as req_lib

        with _patch_runtime(), patch("requests.post", side_effect=req_lib.Timeout()):
            result = _provider().generate(prompt="a banana")
        assert result["success"] is False
        assert result["error_type"] == "timeout"

    def test_connection_error(self):
        import requests as req_lib

        with _patch_runtime(), patch("requests.post", side_effect=req_lib.ConnectionError("no proxy")):
            result = _provider().generate(prompt="a banana")
        assert result["success"] is False
        assert result["error_type"] == "connection_error"

    def test_empty_prompt_with_no_image_rejected(self):
        with _patch_runtime():
            result = _provider().generate(prompt="   ")
        assert result["success"] is False
        assert result["error_type"] == "invalid_argument"

    def test_model_kwarg_flows_into_payload(self):
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             _patch_save("/tmp/x.png"):
            result = _provider().generate(prompt="a banana", model="gemini-3.1-flash-image")
        assert result["model"] == "gemini-3.1-flash-image"
        assert mock_post.call_args.kwargs["json"]["model"] == "gemini-3.1-flash-image"


# ---------------------------------------------------------------------------
# Resolution control (config-only; default 4K)
# ---------------------------------------------------------------------------


class TestResolutionPrecedence:
    def test_default_is_4k(self):
        with _patch_cfg({}):
            assert _provider()._resolve_resolution() == "4K"

    def test_scoped_config_override(self):
        with _patch_cfg({"nano-banana": {"resolution": "2K"}}):
            assert _provider()._resolve_resolution() == "2K"

    def test_env_wins_over_config(self, monkeypatch):
        monkeypatch.setenv("NANO_BANANA_IMAGE_RESOLUTION", "1K")
        with _patch_cfg({"nano-banana": {"resolution": "4K"}}):
            assert _provider()._resolve_resolution() == "1K"

    def test_lowercase_is_normalized_to_uppercase(self):
        # The proxy rejects lowercase 'k'; normalize before sending.
        with _patch_cfg({"nano-banana": {"resolution": "2k"}}):
            assert _provider()._resolve_resolution() == "2K"

    def test_surrounding_whitespace_trimmed(self):
        with _patch_cfg({"nano-banana": {"resolution": "  4K  "}}):
            assert _provider()._resolve_resolution() == "4K"

    def test_unknown_value_falls_back_to_default(self):
        # An out-of-ladder value must not be sent verbatim (would 400 the proxy);
        # fall back to the default rather than fail.
        with _patch_cfg({"nano-banana": {"resolution": "8K"}}):
            assert _provider()._resolve_resolution() == "4K"


class TestResolutionPerModelCap:
    def test_capped_model_clamps_down(self):
        # Lite is documented 1K-only; a 4K request degrades gracefully to 1K.
        with _patch_cfg({"nano-banana": {"resolution": "4K"}}):
            assert (
                _provider()._resolve_resolution(model="gemini-3.1-flash-lite-image")
                == "1K"
            )

    def test_uncapped_model_keeps_requested(self):
        with _patch_cfg({"nano-banana": {"resolution": "4K"}}):
            assert (
                _provider()._resolve_resolution(model="gemini-3-pro-image") == "4K"
            )

    def test_capped_model_below_cap_unchanged(self):
        with _patch_cfg({"nano-banana": {"resolution": "1K"}}):
            assert (
                _provider()._resolve_resolution(model="gemini-3.1-flash-lite-image")
                == "1K"
            )


class TestResolutionPayload:
    def test_image_size_sent_on_text_to_image(self):
        with _patch_cfg({"nano-banana": {"resolution": "4K"}}), _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="a banana", aspect_ratio="landscape")
        ic = mock_post.call_args.kwargs["json"]["image_config"]
        assert ic["image_size"] == "4K"
        assert ic["aspect_ratio"] == "16:9"

    def test_default_4k_sent_when_config_absent(self):
        with _patch_cfg({}), _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="a banana")
        assert mock_post.call_args.kwargs["json"]["image_config"]["image_size"] == "4K"

    def test_config_override_2k_sent(self):
        with _patch_cfg({"nano-banana": {"resolution": "2K"}}), _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="a banana")
        assert mock_post.call_args.kwargs["json"]["image_config"]["image_size"] == "2K"

    def test_success_reports_resolution(self):
        with _patch_cfg({"nano-banana": {"resolution": "2K"}}), _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])), \
             _patch_save("/tmp/x.png"):
            result = _provider().generate(prompt="a banana")
        assert result["resolution"] == "2K"


class TestResolutionGracefulDegradation:
    def test_proxy_rejects_image_size_falls_back_without_it(self):
        """If the proxy 400s on the image_size field, retry once WITHOUT it
        (current no-resolution behavior) rather than failing the generation."""
        import requests as req_lib

        bad = MagicMock()
        bad.status_code = 400
        bad.text = "Unknown name \"image_size\""
        bad.json.return_value = {"error": {"message": "Unknown name \"image_size\""}}
        bad.raise_for_status.side_effect = req_lib.HTTPError(response=bad)
        good = _mock_chat_response([_PNG_DATA_URI])

        with _patch_cfg({"nano-banana": {"resolution": "4K"}}), _patch_runtime(), \
             patch("requests.post", side_effect=[bad, good]) as mock_post, \
             _patch_save("/tmp/x.png"):
            result = _provider().generate(prompt="a banana")

        assert result["success"] is True
        # Two calls: first with image_size, retry without it.
        assert mock_post.call_count == 2
        first_ic = mock_post.call_args_list[0].kwargs["json"]["image_config"]
        second_ic = mock_post.call_args_list[1].kwargs["json"]["image_config"]
        assert "image_size" in first_ic
        assert "image_size" not in second_ic

    def test_unrelated_400_is_not_masked_by_fallback(self):
        """A 400 that is NOT about the resolution field must surface as an error,
        not trigger an infinite/masking retry."""
        import requests as req_lib

        bad = MagicMock()
        bad.status_code = 400
        bad.text = "prompt was blocked by safety policy"
        bad.json.return_value = {"error": {"message": "prompt was blocked by safety policy"}}
        bad.raise_for_status.side_effect = req_lib.HTTPError(response=bad)

        with _patch_cfg({"nano-banana": {"resolution": "4K"}}), _patch_runtime(), \
             patch("requests.post", return_value=bad) as mock_post:
            result = _provider().generate(prompt="a banana")

        assert result["success"] is False
        assert result["error_type"] == "api_error"
        assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# Registration + setup schema
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register(self):
        ctx = MagicMock()
        nb.register(ctx)
        registered = [c.args[0].name for c in ctx.register_image_gen_provider.call_args_list]
        assert registered == ["nano-banana"]

    def test_setup_schema_points_at_prompting_skill(self):
        schema = _provider().get_setup_schema()
        assert isinstance(schema.get("name"), str) and schema["name"]
        blob = (schema.get("tag", "") + schema.get("post_setup_hint", "")).lower()
        assert "nano-banana-prompting" in blob
