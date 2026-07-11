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
