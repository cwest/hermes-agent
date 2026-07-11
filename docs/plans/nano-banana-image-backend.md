# Plan — nano-banana image_gen backend

Spec: `docs/specs/nano-banana-image-backend.md`. TDD (RED→GREEN→REFACTOR) per
task. Full suite green at the end + E2E against the live proxy.

## Task 1 — Cache GC janitor (framework-level)

RED: `tests/agent/test_image_cache_gc.py`
- prune by total size: many files over `max_total_mb` → oldest deleted until
  under cap; newest kept.
- prune by age: file older than `max_age_days` → deleted; fresh file kept.
- never deletes the just-written file (passed as `keep`).
- emits INFO log line with count + freed MB when it prunes; silent when nothing
  to prune.
- caps read from `image_gen.cache.*` config; defaults 30 days / 2048 MB.
- best-effort: GC exception is swallowed (save still returns the path).
- `save_b64_image` / `save_url_image` invoke the janitor after writing.

GREEN: add `_prune_image_cache(keep: Path | None)` +
`_load_cache_config()` to `agent/image_gen_provider.py`; call from both save
functions inside a try/except.

## Task 2 — nano-banana backend provider

RED: `tests/plugins/image_gen/test_nano_banana_provider.py`
- `name == "nano-banana"`, display name set.
- `capabilities()` → modalities include `image`, `max_reference_images >= 1`.
- `default_model() == "gemini-3-pro-image"` (Pro default).
- model precedence: kwarg > env (`NANO_BANANA_IMAGE_MODEL`) >
  `image_gen.nano-banana.model` > `image_gen.model` (known id) > DEFAULT_MODEL.
- `is_available()` True with key, False without, False on resolution error.
- `generate()` success (data URI) → saves via `save_b64_image`, returns path,
  provider == "nano-banana".
- payload shape: `modalities:["image","text"]`, `image_config.aspect_ratio`
  mapped (`portrait`→`9:16`), reference/edit image inlined as data URI part,
  reads image from `choices[0].message.images[0].image_url.url`.
- auth header `Authorization: Bearer <token>`; posts to
  `<base_url>/chat/completions`.
- edit routing: `image_url` present → attached as content part.
- multiple references clamped to `max_reference_images`.
- graceful degradation: missing creds → `missing_api_key`; empty images →
  `empty_response`; HTTP error → `api_error`; timeout → `timeout`.
- `register(ctx)` registers exactly one provider named `nano-banana`.

GREEN: `plugins/image_gen/nano-banana/__init__.py` +
`plugins/image_gen/nano-banana/plugin.yaml`.

## Task 3 — config discoverability

Add `image_gen` section to DEFAULT_CONFIG (`hermes_cli/config.py`) documenting
`cache.max_age_days`, `cache.max_total_mb`, and the model slot. New key →
deep-merge handles it, no `_config_version` bump.

## Task 4 — Verify

- Full targeted suite: `tests/plugins/image_gen/` + `tests/agent/test_image_cache_gc.py`.
- Broader import/regression sanity.
- E2E against live proxy: text-to-image (Pro + Flash), edit (`image_url`),
  reference grounding (multiple), confirm MEDIA-path output, confirm GC hook
  fires.

## Task 5 — Commit + PR

Signed, Conventional Commits, team-invisible. Push topic branch. Open DRAFT PR
against `cwest/integration`. Handoff comment on the card (PR url + head SHA +
test evidence). Do NOT complete the card.
