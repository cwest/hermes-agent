"""Tests for the self-announcing wake banner on a transition-woken turn.

Card t_dfafdefd (piece 2 of the event-driven-autonomy gap): once a kanban
transition wakes the orchestrator as its own turn, that woken output must be
UNMISTAKABLE. Today the route prompt only says "surface context," so the woken
reply looks like any other message and got conflated with a late-delivered prior
reply (verified 2026-07-01).

The fix: every transition-emit payload carries a unique, greppable ``wake_banner``
— a fixed-prefix line stamping ``task_id`` + ``kind`` + ``from``->``to`` lane +
``event_id`` — so the woken turn can lead with it verbatim and both the
orchestrator and Casey can identify the wake unambiguously (and grep for it).

Behavior contract (invariants, not snapshots):
  - The banner is deterministic for a given (task_id, kind, from, to, event_id):
    same inputs -> byte-identical banner; different event_id -> different banner.
  - It leads with a fixed, unique prefix (``AUTONOMOUS-WAKE``) that cannot collide
    with an ordinary chat message.
  - It carries the from->to lane hop when known, and degrades to a stable
    placeholder (never crashes, never emits ``None``) when lane info is absent —
    most transition kinds do not carry a lane pair.
  - ``build_transition_payload`` stamps ``from_lane``/``to_lane``/``wake_banner``
    into the payload so the webhook route can render it via ``{wake_banner}``.
"""

from __future__ import annotations

from gateway.kanban_transition_emit import (
    WAKE_BANNER_PREFIX,
    build_transition_payload,
    build_wake_banner,
)


def test_banner_has_fixed_unique_prefix():
    banner = build_wake_banner(
        task_id="t_abc", kind="blocked", from_lane="ready",
        to_lane="blocked", event_id=99,
    )
    assert banner.startswith(WAKE_BANNER_PREFIX)
    # The prefix is a fixed, greppable sentinel unlikely to appear in prose.
    assert WAKE_BANNER_PREFIX == "AUTONOMOUS-WAKE"


def test_banner_carries_all_transition_coordinates():
    banner = build_wake_banner(
        task_id="t_abc123", kind="blocked", from_lane="ready",
        to_lane="blocked", event_id=4242,
    )
    assert banner == "AUTONOMOUS-WAKE t_abc123 blocked ready->blocked evt=4242"


def test_banner_is_deterministic_and_event_scoped():
    a = build_wake_banner(
        task_id="t_x", kind="assigned", from_lane="running",
        to_lane="review", event_id=7,
    )
    b = build_wake_banner(
        task_id="t_x", kind="assigned", from_lane="running",
        to_lane="review", event_id=7,
    )
    c = build_wake_banner(
        task_id="t_x", kind="assigned", from_lane="running",
        to_lane="review", event_id=8,
    )
    assert a == b  # byte-identical for identical inputs (greppable, stable)
    assert a != c  # a different event yields a different, unique banner


def test_banner_degrades_when_lane_unknown_never_none():
    # Most kinds (crashed / timed_out / gave_up) carry no from->to lane pair.
    banner = build_wake_banner(
        task_id="t_x", kind="crashed", from_lane=None,
        to_lane=None, event_id=3,
    )
    assert banner.startswith("AUTONOMOUS-WAKE t_x crashed ")
    assert "None" not in banner  # never leaks a Python None into the wire text
    assert "evt=3" in banner


def test_payload_stamps_banner_and_lanes():
    payload = build_transition_payload(
        task_id="t_abc123",
        board="default",
        kind="status_changed",
        reason="",
        event_id=17,
        title="Adopt event-driven orchestration",
        from_lane="ready",
        to_lane="review",
    )
    assert payload["from_lane"] == "ready"
    assert payload["to_lane"] == "review"
    assert payload["wake_banner"] == (
        "AUTONOMOUS-WAKE t_abc123 status_changed ready->review evt=17"
    )


def test_payload_banner_present_even_without_lanes():
    # A blocked/acceptance transition carries no from/to; banner still stamped.
    payload = build_transition_payload(
        task_id="t_z",
        board="default",
        kind="blocked",
        reason="awaiting-casey-signoff: merge PR #99",
        event_id=5,
    )
    assert payload["wake_banner"].startswith("AUTONOMOUS-WAKE t_z blocked ")
    assert "evt=5" in payload["wake_banner"]
    assert "None" not in payload["wake_banner"]
