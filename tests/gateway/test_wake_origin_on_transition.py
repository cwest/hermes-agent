"""Tests for the wake-origin-on-transition fix.

Two defects proven from a live E2E test (card t_278408f5, 2026-07-01):

1. ``should_emit_transition`` / ``DEFAULT_EMIT_KINDS`` did not cover the
   actionable lane-move kinds (``status_changed``, ``assigned``, ``unblocked``),
   so a card moving to ``review`` fired no wake at all.

2. The webhook route ignored the ``origin_*`` fields the emitter sends, so every
   wake ran in a contextless ``webhook:<route>:<delivery>`` session instead of
   the origin thread session it was born in.

These tests assert the intended behavior and are RED against the pre-fix code.
"""

from __future__ import annotations

import gateway.kanban_transition_emit as kte


# ---------------------------------------------------------------------------
# Defect 1 — the wake gate must cover actionable lane-move kinds
# ---------------------------------------------------------------------------

def _enabled_cfg(**over):
    cfg = {"enabled": True}
    cfg.update(over)
    return cfg


def test_should_emit_covers_status_changed_by_default():
    # A card moving to review fires ``status_changed`` — it MUST wake the
    # orchestrator so it can act on the handoff. Was silent pre-fix.
    assert kte.should_emit_transition(_enabled_cfg(), "status_changed") is True


def test_should_emit_covers_assigned_by_default():
    assert kte.should_emit_transition(_enabled_cfg(), "assigned") is True


def test_should_emit_covers_unblocked_by_default():
    assert kte.should_emit_transition(_enabled_cfg(), "unblocked") is True


def test_should_emit_still_covers_blocked_and_loop_detected():
    # No regression to the two kinds that already worked.
    assert kte.should_emit_transition(_enabled_cfg(), "blocked") is True
    assert kte.should_emit_transition(
        _enabled_cfg(), "block_loop_detected"
    ) is True


def test_should_emit_covers_terminal_failure_kinds_by_default():
    # A worker that gives up / crashes / times out is a terminal-failure
    # escalation: the notifier's NOTIFY_KINDS pings chat for these (via
    # TERMINAL_KINDS), but the wake must ALSO fire so the orchestrator can act
    # on the failure path. These were silent pre-fix — a crashed worker pinged
    # chat but woke no orchestrator, the same silent-escalation gap this fix
    # closes, on the failure side.
    for kind in ("gave_up", "crashed", "timed_out"):
        assert kte.should_emit_transition(_enabled_cfg(), kind) is True, kind


def test_should_emit_respects_explicit_config_override():
    # An explicit emit_kinds list still wins over the default set.
    cfg = _enabled_cfg(emit_kinds=["blocked"])
    assert kte.should_emit_transition(cfg, "blocked") is True
    assert kte.should_emit_transition(cfg, "status_changed") is False


def test_should_emit_disabled_when_not_enabled():
    assert kte.should_emit_transition({"enabled": False}, "status_changed") is False
    assert kte.should_emit_transition(None, "status_changed") is False
