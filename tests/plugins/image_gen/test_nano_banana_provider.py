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
# Raw base64 (no data: prefix) — the images-API b64_json field carries this.
_PNG_B64 = "dGVzdC1pbWFnZS1kYXRh"  # "test-image-data"


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
    """Mock a /chat/completions image response (the EDIT / reference path)."""
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


def _mock_images_response(items):
    """Mock a /v1/images/generations response (the TEXT-TO-IMAGE path).

    Shape: ``{"data": [{"b64_json": ...} | {"url": ...}]}`` — the OpenAI
    images-API shape LiteLLM returns. This is the path where the nested
    ``imageConfig`` -> Vertex ``generationConfig.imageConfig`` mapping happens,
    so resolution (4K) is actually honored here (unlike the chat path).
    """
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"data": list(items)}
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
# generate() — TEXT-TO-IMAGE (routes to /v1/images/generations)
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_missing_credentials(self):
        with _patch_runtime(api_key=""):
            result = _provider().generate(prompt="a banana")
        assert result["success"] is False
        assert result["error_type"] == "missing_api_key"

    def test_success_b64_json(self):
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_images_response([{"b64_json": _PNG_B64}])), \
             _patch_save() as mock_save:
            result = _provider().generate(prompt="a banana")
        assert result["success"] is True
        assert result["image"] == "/tmp/nano_banana_gen.png"
        assert result["provider"] == "nano-banana"
        assert result["model"] == "gemini-3-pro-image"
        mock_save.assert_called_once()

    def test_empty_data_is_empty_response(self):
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_images_response([])):
            result = _provider().generate(prompt="a banana")
        assert result["success"] is False
        assert result["error_type"] == "empty_response"

    def test_posts_to_images_generations_endpoint(self):
        """TEXT-TO-IMAGE MUST hit /v1/images/generations (NOT chat/completions):
        that is the only path where LiteLLM maps imageConfig -> Vertex, so Pro
        honors 4K. This contract would have caught the original ~1K bug."""
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_images_response([{"b64_json": _PNG_B64}])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="a banana")
        url = mock_post.call_args[0][0]
        assert url == "http://127.0.0.1:4000/v1/images/generations"

    def test_payload_shape_text_to_image_nested_image_config(self):
        """The payload MUST carry model + prompt + a NESTED imageConfig with
        imageSize (= configured resolution) and aspectRatio (proxy string).
        FLAT imageSize/image_size at the top level are DROPPED by the proxy, so
        they must NOT be sent."""
        with _patch_cfg({"nano-banana": {"resolution": "4K"}}), _patch_runtime(), \
             patch("requests.post", return_value=_mock_images_response([{"b64_json": _PNG_B64}])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="a banana", aspect_ratio="landscape")
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "gemini-3-pro-image"
        assert payload["prompt"] == "a banana"
        # NESTED imageConfig — the load-bearing contract.
        assert payload["imageConfig"] == {"imageSize": "4K", "aspectRatio": "16:9"}
        # No FLAT resolution keys leak to the top level (proxy drops them).
        assert "imageSize" not in payload
        assert "image_size" not in payload
        # This is the images API, not chat: no chat-only keys.
        assert "messages" not in payload
        assert "modalities" not in payload

    def test_auth_header_bearer_token(self):
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_images_response([{"b64_json": _PNG_B64}])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="a banana")
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-local"

    def test_model_kwarg_flows_into_payload(self):
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_images_response([{"b64_json": _PNG_B64}])) as mock_post, \
             _patch_save("/tmp/x.png"):
            result = _provider().generate(prompt="a banana", model="gemini-3.1-flash-image")
        assert result["model"] == "gemini-3.1-flash-image"
        assert mock_post.call_args.kwargs["json"]["model"] == "gemini-3.1-flash-image"

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


# ---------------------------------------------------------------------------
# generate() — TEXT-TO-IMAGE response parsing (images-API shape)
# ---------------------------------------------------------------------------


class TestTextToImageResponseParse:
    def test_b64_json_is_saved(self):
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_images_response([{"b64_json": _PNG_B64}])), \
             _patch_save("/tmp/b64.png") as mock_save:
            result = _provider().generate(prompt="a banana")
        assert result["success"] is True
        assert result["image"] == "/tmp/b64.png"
        # b64_json is decoded and saved directly (no data: prefix stripping).
        mock_save.assert_called_once()
        assert mock_save.call_args[0][0] == _PNG_B64

    def test_url_is_fetched_and_saved(self):
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_images_response([{"url": "https://cdn.example/img.png"}])), \
             patch.object(nb, "save_url_image", return_value=Path("/tmp/url.png")) as mock_url_save:
            result = _provider().generate(prompt="a banana")
        assert result["success"] is True
        assert result["image"] == "/tmp/url.png"
        mock_url_save.assert_called_once()
        assert mock_url_save.call_args[0][0] == "https://cdn.example/img.png"

    def test_b64_json_preferred_when_both_present(self):
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_images_response(
                 [{"b64_json": _PNG_B64, "url": "https://cdn.example/img.png"}])), \
             _patch_save("/tmp/b64.png") as mock_save:
            result = _provider().generate(prompt="a banana")
        assert result["success"] is True
        assert result["image"] == "/tmp/b64.png"
        mock_save.assert_called_once()


# ---------------------------------------------------------------------------
# generate() — EDIT / reference path (KEPT on /chat/completions)
# ---------------------------------------------------------------------------
#
# Design decision (spec change #2): /v1/images/generations has no clean input-
# image contract at LiteLLM 1.92.0 (image input lives on the multipart
# /v1/images/edits route, unverified against this proxy). The edit/reference
# case already works on chat/completions today, and resolution matters less
# there because the model preserves source dimensions. So text-to-image moves
# to the images path (where 4K is honored) and edit/reference STAYS on chat.


class TestEditPathStaysOnChat:
    def test_edit_routing_hits_chat_completions(self, tmp_path):
        src = tmp_path / "src.png"
        src.write_bytes(b"\x89PNG\r\n")
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             _patch_save("/tmp/x.png"):
            result = _provider().generate(prompt="make it red", image_url=str(src))
        assert result["success"] is True
        assert result["modality"] == "image"
        # Edit stays on the chat path (input-image contract works there).
        url = mock_post.call_args[0][0]
        assert url == "http://127.0.0.1:4000/v1/chat/completions"

    def test_edit_routing_attaches_image_url(self, tmp_path):
        src = tmp_path / "src.png"
        src.write_bytes(b"\x89PNG\r\n")
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             _patch_save("/tmp/x.png"):
            result = _provider().generate(prompt="make it red", image_url=str(src))
        assert result["success"] is True
        content = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
        image_parts = [c for c in content if c["type"] == "image_url"]
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_edit_payload_is_chat_shape(self, tmp_path):
        src = tmp_path / "src.png"
        src.write_bytes(b"\x89PNG\r\n")
        with _patch_runtime(), \
             patch("requests.post", return_value=_mock_chat_response([_PNG_DATA_URI])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="make it red", image_url=str(src))
        payload = mock_post.call_args.kwargs["json"]
        assert payload["modalities"] == ["image", "text"]
        assert "messages" in payload
        # The chat path keeps the flat image_config it already used.
        assert "image_config" in payload
        # It does NOT carry the images-API nested imageConfig.
        assert "imageConfig" not in payload

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

    def test_edit_reads_image_from_choices_message_images(self, tmp_path):
        """The edit path still reads the image from
        choices[0].message.images[0].image_url.url (content is null)."""
        src = tmp_path / "src.png"
        src.write_bytes(b"\x89PNG\r\n")
        resp = _mock_chat_response([_PNG_DATA_URI], content=None)
        with _patch_runtime(), \
             patch("requests.post", return_value=resp), \
             _patch_save("/tmp/x.png"):
            result = _provider().generate(prompt="make it red", image_url=str(src))
        assert result["success"] is True


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
    """image_size now travels as a NESTED imageConfig.imageSize on the images
    path (text-to-image), not the flat chat image_config.image_size."""

    def test_image_size_sent_on_text_to_image(self):
        with _patch_cfg({"nano-banana": {"resolution": "4K"}}), _patch_runtime(), \
             patch("requests.post", return_value=_mock_images_response([{"b64_json": _PNG_B64}])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="a banana", aspect_ratio="landscape")
        ic = mock_post.call_args.kwargs["json"]["imageConfig"]
        assert ic["imageSize"] == "4K"
        assert ic["aspectRatio"] == "16:9"

    def test_default_4k_sent_when_config_absent(self):
        with _patch_cfg({}), _patch_runtime(), \
             patch("requests.post", return_value=_mock_images_response([{"b64_json": _PNG_B64}])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="a banana")
        assert mock_post.call_args.kwargs["json"]["imageConfig"]["imageSize"] == "4K"

    def test_config_override_2k_sent(self):
        with _patch_cfg({"nano-banana": {"resolution": "2K"}}), _patch_runtime(), \
             patch("requests.post", return_value=_mock_images_response([{"b64_json": _PNG_B64}])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="a banana")
        assert mock_post.call_args.kwargs["json"]["imageConfig"]["imageSize"] == "2K"

    def test_success_reports_resolution(self):
        with _patch_cfg({"nano-banana": {"resolution": "2K"}}), _patch_runtime(), \
             patch("requests.post", return_value=_mock_images_response([{"b64_json": _PNG_B64}])), \
             _patch_save("/tmp/x.png"):
            result = _provider().generate(prompt="a banana")
        assert result["resolution"] == "2K"

    def test_lite_cap_still_applies_on_images_path(self):
        """The per-model 1K cap for Lite still clamps on the images path."""
        with _patch_cfg({"nano-banana": {"resolution": "4K", "model": "gemini-3.1-flash-lite-image"}}), \
             _patch_runtime(), \
             patch("requests.post", return_value=_mock_images_response([{"b64_json": _PNG_B64}])) as mock_post, \
             _patch_save("/tmp/x.png"):
            _provider().generate(prompt="a banana")
        assert mock_post.call_args.kwargs["json"]["imageConfig"]["imageSize"] == "1K"


class TestResolutionGracefulDegradation:
    """If the proxy is on an older LiteLLM that rejects the nested imageConfig,
    retry once WITHOUT it so generation still lands (don't hard-fail)."""

    def test_proxy_rejects_image_config_falls_back_without_it(self):
        import requests as req_lib

        bad = MagicMock()
        bad.status_code = 400
        bad.text = "Unknown name \"imageConfig\""
        bad.json.return_value = {"error": {"message": "Unknown name \"imageConfig\""}}
        bad.raise_for_status.side_effect = req_lib.HTTPError(response=bad)
        good = _mock_images_response([{"b64_json": _PNG_B64}])

        with _patch_cfg({"nano-banana": {"resolution": "4K"}}), _patch_runtime(), \
             patch("requests.post", side_effect=[bad, good]) as mock_post, \
             _patch_save("/tmp/x.png"):
            result = _provider().generate(prompt="a banana")

        assert result["success"] is True
        # Two calls: first with imageConfig, retry without it.
        assert mock_post.call_count == 2
        first_payload = mock_post.call_args_list[0].kwargs["json"]
        second_payload = mock_post.call_args_list[1].kwargs["json"]
        assert "imageConfig" in first_payload
        assert "imageConfig" not in second_payload

    def test_rejects_image_size_error_also_triggers_fallback(self):
        """An older proxy may name the field imageSize/image_size in its 400;
        the fallback still fires."""
        import requests as req_lib

        bad = MagicMock()
        bad.status_code = 400
        bad.text = "Unknown name \"imageSize\""
        bad.json.return_value = {"error": {"message": "Unknown name \"imageSize\""}}
        bad.raise_for_status.side_effect = req_lib.HTTPError(response=bad)
        good = _mock_images_response([{"b64_json": _PNG_B64}])

        with _patch_cfg({"nano-banana": {"resolution": "4K"}}), _patch_runtime(), \
             patch("requests.post", side_effect=[bad, good]) as mock_post, \
             _patch_save("/tmp/x.png"):
            result = _provider().generate(prompt="a banana")

        assert result["success"] is True
        assert mock_post.call_count == 2
        assert "imageConfig" not in mock_post.call_args_list[1].kwargs["json"]

    def test_unrelated_400_is_not_masked_by_fallback(self):
        """A 400 that is NOT about the imageConfig/resolution field must surface
        as an error, not trigger a masking retry."""
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
