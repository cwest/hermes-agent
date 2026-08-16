"""Behavior-contract tests for the mid-session voice-contract re-injection.

The voice contract is a SHORT reminder re-asserted into a long-running
session on a turn-count cadence. It rides the existing ``api_content``
sidecar channel (the API copy of the current user message) so that:

  - the composed SYSTEM PROMPT stays byte-stable for the life of the
    conversation (prompt-cache invariant),
  - no synthetic user message is ever inserted (role-alternation invariant),
  - the persisted ``api_content`` sidecar equals the bytes sent (drift
    invariant),
  - the reminder appears at the configured cadence, not every turn,
  - the payload is hard-capped even with an oversized configured contract.

These are contracts about how the pieces relate, not snapshots of the
current wording — see AGENTS.md "Don't write change-detector tests".
"""

from __future__ import annotations

from agent.turn_context import (
    MAX_VOICE_CONTRACT_CHARS,
    compose_user_api_content,
    format_voice_contract_reminder,
    should_inject_voice_contract,
)


# ── format_voice_contract_reminder: bounding + shape ──────────────────────

def test_reminder_is_none_for_empty_contract():
    assert format_voice_contract_reminder("") is None
    assert format_voice_contract_reminder("   ") is None
    assert format_voice_contract_reminder(None) is None


def test_reminder_wraps_contract_and_flags_it_as_a_system_note():
    out = format_voice_contract_reminder("Terse. Evidence-first.")
    assert out is not None
    # The block must self-identify as a system note (not new user input) so
    # the model doesn't treat it as a fresh instruction from the user.
    assert "Terse. Evidence-first." in out
    lowered = out.lower()
    assert "system" in lowered and "note" in lowered


def test_reminder_hard_caps_an_oversized_contract():
    huge = "V" * (MAX_VOICE_CONTRACT_CHARS * 5)
    out = format_voice_contract_reminder(huge)
    assert out is not None
    # The whole rendered block (wrapper included) must never exceed the cap,
    # following the TodoStore bounding precedent.
    assert len(out) <= MAX_VOICE_CONTRACT_CHARS


def test_reminder_cap_default_is_a_few_hundred_chars():
    # The card targets "a few hundred characters" — a small always-on core.
    assert 100 <= MAX_VOICE_CONTRACT_CHARS <= 1000


# ── should_inject_voice_contract: cadence, not every turn ─────────────────

def test_cadence_disabled_when_interval_is_zero():
    for turn in range(0, 12):
        assert should_inject_voice_contract(turn, 0) is False


def test_cadence_fires_only_on_multiples_of_the_interval():
    interval = 5
    fired = [t for t in range(1, 21) if should_inject_voice_contract(t, interval)]
    assert fired == [5, 10, 15, 20]


def test_cadence_does_not_fire_on_the_first_turn():
    # _user_turn_count is 1 on the first turn; a fresh session must not eat
    # the reminder cost before any decay could have happened.
    assert should_inject_voice_contract(1, 5) is False


def test_cadence_negative_interval_is_off():
    assert should_inject_voice_contract(10, -1) is False


# ── compose_user_api_content: injection wiring + no-op safety ─────────────

def test_voice_contract_appended_to_api_copy():
    out = compose_user_api_content(
        "hello",
        ext_prefetch_cache="",
        plugin_user_context="",
        voice_contract="Terse. Evidence-first.",
    )
    assert out is not None
    assert out.startswith("hello")
    assert "Terse. Evidence-first." in out


def test_no_injection_returns_none_so_message_sends_as_is():
    # No ephemeral context of any kind => None => the clean message ships
    # unchanged (no sidecar, cache-neutral).
    out = compose_user_api_content(
        "hello",
        ext_prefetch_cache="",
        plugin_user_context="",
        voice_contract="",
    )
    assert out is None


def test_voice_contract_default_arg_is_backward_compatible():
    # Existing callers that don't pass voice_contract keep the old behavior.
    out = compose_user_api_content("hello", "", "")
    assert out is None


def test_multimodal_content_never_takes_the_sidecar():
    # Non-string (list) content returns None regardless of a voice contract,
    # so image/attachment turns don't silently drop or mis-attach it.
    out = compose_user_api_content(
        [{"type": "text", "text": "hi"}],
        ext_prefetch_cache="",
        plugin_user_context="",
        voice_contract="Terse.",
    )
    assert out is None


def test_voice_contract_composes_with_other_injections():
    out = compose_user_api_content(
        "hello",
        ext_prefetch_cache="",
        plugin_user_context="plugin-ctx",
        voice_contract="Terse.",
    )
    assert out is not None
    assert "plugin-ctx" in out
    assert "Terse." in out


# ── config plumbing: default-off invariant ────────────────────────────────

def test_config_default_ships_voice_contract_off():
    # Behavior contract (not a snapshot): the shipped default must resolve the
    # feature OFF — interval 0 and empty text — since it changes prompt bytes
    # for every fired turn.
    from hermes_cli.config import DEFAULT_CONFIG

    vc = DEFAULT_CONFIG["agent"]["voice_contract"]
    assert vc["interval"] == 0
    assert vc["text"] == ""
    # And the off default means the cadence predicate never fires on it.
    assert should_inject_voice_contract(10, vc["interval"]) is False

