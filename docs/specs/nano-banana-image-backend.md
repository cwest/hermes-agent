# Spec — nano-banana image_gen backend

## Goal

Add a first-class `image_gen` backend, `nano-banana`, that serves Google's
"nano banana" Gemini image family through the local OpenAI-compatible proxy at
`http://127.0.0.1:4000/v1`. Full v1 capability: text-to-image, image editing
(`image_url` source), and reference-image grounding (`reference_image_urls`,
multiple, character-consistency). Pro is the quality default; Flash is a
one-parameter fast path. Adds a framework-level GC janitor over the shared
image cache.

This is ONE new backend on the existing `ImageGenProvider` framework — not a
from-scratch plugin. It mirrors the shipped `plugins/image_gen/openrouter/`
backend (which already speaks this exact `/chat/completions` image protocol)
and the `plugins/image_gen/openai-codex/` tier-precedence pattern.

## Verified grounding (live proxy, HTTP 200)

Probed against the running proxy before design:

- Models served: `gemini-3-pro-image` (Pro, default) + `gemini-3.1-flash-image`
  (Flash). `gemini-3.1-flash-lite-image` (Lite) is NOT on the proxy yet.
- Response shape: `choices[0].message.content` is `null`; the image is at
  `choices[0].message.images[0].image_url.url` as `data:image/png;base64,…`.
- `image_config.aspect_ratio` **works** on the generate path:
  `1:1`→1024×1024, `9:16`→768×1376, `16:9`→1376×768, `4:3`→1200×896.
  (Supersedes the brief's "1408×768 regardless" note for generate.)
- Edit/reference works: inline a source image as an `image_url` content part →
  edited image returned. On the edit path, output dims track the source image
  (~1408×768) rather than the aspect flag — documented as a limitation.

## Auth (rubric-compliant, no new env var)

The proxy is a configured `custom_providers` entry named `vertex-llm-proxy`
(base_url `http://127.0.0.1:4000/v1`, token via the existing secret mechanism).
The backend resolves `(base_url, api_key)` at runtime via
`resolve_runtime_provider(requested=<runtime_name>)`, exactly like the
openrouter backend. The runtime name is config-overridable
(`image_gen.nano-banana.runtime`), defaulting to `custom:vertex-llm-proxy`.
The client sends only `Authorization: Bearer <proxy token>`; the proxy
authenticates to the upstream server-side. No Google credential client-side.

## Naming

Backend name = `nano-banana` (selected via `image_gen.provider: nano-banana`).
NOT `vertex` — that is a dead product name. The card body's residual `vertex`
references in "Done when"/config lines are stale; the authoritative NAMING NOTE
governs.

## Model routing (Pro-default, config-driven)

Precedence (first hit wins), mirroring openai-codex:

1. `model` kwarg from dispatch (explicit caller override)
2. `NANO_BANANA_IMAGE_MODEL` env (escape hatch for scripts/tests)
3. `image_gen.nano-banana.model` in config.yaml
4. `image_gen.model` in config.yaml (when it's one of our known ids)
5. `DEFAULT_MODEL` = `gemini-3-pro-image` (Pro)

Flash is `gemini-3.1-flash-image`. Lite (`gemini-3.1-flash-lite-image`) is a
documented catalog slot with zero-code drop-in when the proxy serves it. No
two-model hard-coding: the resolver treats the model list as data.

## Capabilities

`modalities: ["text", "image"]`, `max_reference_images: 3` (clamp;
Gemini image models accept up to 3 input images per request — matches the
openrouter backend's clamp). References inlined as data URIs for local files.

## Aspect ratio

Semantic aspect → proxy strings: `square`→`1:1`, `landscape`→`16:9`,
`portrait`→`9:16`. Uses the framework's `resolve_aspect_ratio` clamp. Passed
as `image_config.aspect_ratio`. Verified working on generate; documented that
edit-path dims follow the source.

## Graceful degradation

`is_available()` returns False (no crash) when the runtime can't resolve or the
token is missing. `generate()` surfaces `error_response` for missing creds,
API errors, timeouts, connection errors, empty responses, and IO errors.

## Cache GC — framework-level janitor (decided policy)

The cache dir `$HERMES_HOME/cache/images/` is SHARED by all backends, so GC is
framework-level, NOT bolted onto nano-banana. No existing prune mechanism was
found. Implement a small shared helper in `agent/image_gen_provider.py`,
invoked opportunistically inside `save_b64_image` / `save_url_image` (the two
shared save paths every backend already uses — so all six existing backends get
GC for free, zero per-backend code):

- Prune when EITHER total cache size > `max_total_mb` (default 2048) OR a file's
  age > `max_age_days` (default 30) — whichever trips first — deleting
  OLDEST-first (by mtime) until back under caps.
- Never delete the file just written.
- Emit a single INFO log line on prune:
  `image cache GC: pruned N files, freed X MB`.
- Caps config-overridable: `image_gen.cache.max_age_days`,
  `image_gen.cache.max_total_mb` (config.yaml, not env).
- Best-effort: any GC error is swallowed (logged at debug) so a GC hiccup never
  fails an image save. Safe by construction — durable copies live in the
  destination repos; the cache is disposable machine-local state.

## Prompting hook

The setup schema / tag points agents at the forthcoming `nano-banana-prompting`
skill. No prompt-craft inlined into backend code.

## Files

- `plugins/image_gen/nano-banana/plugin.yaml` (kind: backend)
- `plugins/image_gen/nano-banana/__init__.py` (NanoBananaImageGenProvider)
- `agent/image_gen_provider.py` (add `_prune_image_cache` + hook into save fns)
- `hermes_cli/config.py` (DEFAULT_CONFIG `image_gen` section for discoverability)
- `tests/plugins/image_gen/test_nano_banana_provider.py`
- `tests/agent/test_image_cache_gc.py`

## Out of scope (separate follow-ups)

`nano-banana-prompting` skill; illustrator persona; multi-turn stateful editing;
upstreaming to core (stays a plugin).
