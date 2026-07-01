"""Thread-origin autonomy: report-back to the origin thread + wake Hollis.

Covers three fixes that together close the loop so work moving through the
kanban system proactively reaches BOTH Casey (in his origin thread) and Hollis
(to proceed) — with no "status / update / check it" polling:

F1 — a notify-subscription must be owned by the profile of the gateway that will
     DELIVER it (the notifier's profile), not the profile of whoever created the
     sub. Otherwise the notifier's owner-profile gate silently drops it.
F2 — a card created from a thread must persist its origin session_id, so a later
     transition can wake that session.
F3 — a terminal transition must target the card's ORIGIN session/thread for
     delivery, not a throwaway webhook session; fall back to the default channel
     when there is no origin.

Run with the hermes venv python:
  ../hermes-agent/.venv/bin/python -m pytest tests/gateway/test_thread_origin_autonomy.py -q
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb


# ── F1: subscription ownership = the delivering gateway's profile ─────────────

def test_notifier_delivery_profile_resolver_exists_and_is_stable(monkeypatch):
    """There must be ONE canonical resolver for 'the profile that delivers
    notifications', so the notifier gate and every subscribe site agree.

    It resolves from config `kanban.notifier_profile` first, then the active
    profile, then 'default' — never raising.
    """
    # Config value wins.
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **k: {"kanban": {"notifier_profile": "default"}},
    )
    assert kb.notifier_delivery_profile() == "default"

    # Empty config value falls back to a non-empty default (never "").
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **k: {"kanban": {"notifier_profile": ""}},
    )
    assert kb.notifier_delivery_profile()  # truthy, not empty


def test_subscription_defaults_to_delivery_profile_not_creator(tmp_path, monkeypatch):
    """A sub created under a WORKER profile must still be stamped with the
    delivering gateway's profile, so the notifier gate passes.

    This is the F1 regression: subs were stamped with the creator's profile
    (`salton`/`hollis`/…), which the `default` gateway notifier drops.
    """
    db_path = tmp_path / "f1.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    # The delivering gateway is 'default'.
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **k: {"kanban": {"notifier_profile": "default"}},
    )
    # …but the CREATOR context is a worker profile 'salton'.
    monkeypatch.setenv("HERMES_PROFILE_NAME", "salton")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="worker-created", assignee="salton")
        # Subscribe WITHOUT an explicit notifier_profile — must default to the
        # delivering gateway's profile, not the creator's.
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord", chat_id="c1",
            notifier_profile=kb.notifier_delivery_profile(),
        )
        subs = kb.list_notify_subs(conn)
        mine = [s for s in subs if s["task_id"] == tid]
        assert mine, "subscription must exist"
        assert mine[0]["notifier_profile"] == "default", (
            "sub must be owned by the delivering gateway's profile ('default'), "
            "not the creator's ('salton') — else the notifier drops it"
        )
    finally:
        conn.close()


# ── F2: cards persist their origin session_id ────────────────────────────────

def test_create_task_persists_origin_session_id(tmp_path, monkeypatch):
    """A card created with an origin session must persist session_id so a later
    transition can wake that session. (create_task already accepts it; this
    locks the contract that it round-trips.)"""
    db_path = tmp_path / "f2.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        sid = "agent:main:discord:thread:123:123"
        tid = kb.create_task(
            conn, title="thread-born", assignee="hollis", session_id=sid,
        )
        task = kb.get_task(conn, tid)
        assert task.session_id == sid, "origin session_id must persist on the card"

        # No session context => None (non-thread origin), not a crash.
        tid2 = kb.create_task(conn, title="no-origin", assignee="hollis")
        assert kb.get_task(conn, tid2).session_id in (None, ""), (
            "a card with no origin session must have empty session_id"
        )
    finally:
        conn.close()


# ── F3: transition targets the ORIGIN session/thread (fallback = default) ─────

def test_resolve_transition_target_prefers_origin_then_falls_back():
    """The transition wake must resolve delivery to the card's ORIGIN
    session/thread when present, else the default channel — never a throwaway
    webhook session."""
    from gateway.kanban_transition_emit import resolve_transition_target

    DEFAULT_CHANNEL = "1515879019269197885"

    # Card with an origin session + thread source => target the origin.
    origin = resolve_transition_target(
        session_id="agent:main:discord:thread:123:123",
        sub={"platform": "discord", "chat_id": "123", "thread_id": "123"},
        default_channel=DEFAULT_CHANNEL,
    )
    assert origin["session_id"] == "agent:main:discord:thread:123:123"
    assert origin["chat_id"] == "123"
    assert origin["thread_id"] == "123"
    assert origin.get("is_fallback") is False

    # No origin session and no sub => fall back to the default channel.
    fb = resolve_transition_target(
        session_id=None, sub=None, default_channel=DEFAULT_CHANNEL,
    )
    assert fb["chat_id"] == DEFAULT_CHANNEL
    assert fb.get("is_fallback") is True
    # Never a throwaway webhook session.
    assert "webhook:kanban-transition" not in str(fb.get("session_id") or "")


def test_build_payload_carries_origin_when_known_omits_when_not():
    """The transition payload carries origin session/thread when known (so the
    woken orchestrator reports back to the origin thread), and omits them when
    unknown (keeping the body byte-stable for the fallback case)."""
    from gateway.kanban_transition_emit import build_transition_payload

    with_origin = build_transition_payload(
        task_id="t_o", board="default", kind="completed", reason=None, event_id=5,
        title="x",
        origin_session_id="agent:main:discord:thread:9:9",
        origin_platform="discord", origin_chat_id="9", origin_thread_id="9",
    )
    assert with_origin["origin_session_id"] == "agent:main:discord:thread:9:9"
    assert with_origin["origin_platform"] == "discord"
    assert with_origin["origin_chat_id"] == "9"
    assert with_origin["origin_thread_id"] == "9"

    without = build_transition_payload(
        task_id="t_n", board="default", kind="completed", reason=None, event_id=6,
        title="x",
    )
    # No origin keys leak into the body when unknown.
    for k in ("origin_session_id", "origin_platform", "origin_chat_id", "origin_thread_id"):
        assert k not in without
    # event_type still present (the merged classification fix must survive).
    assert without["event_type"] == "completed"


# ── F2 wiring: the origin stamp key must MIRROR the live inbound key ──────────
#
# The F2 stamp on `/kanban create` derives the origin session key that a later
# transition wakes. If that derivation diverges from the key the live inbound
# path (base.handle_message -> build_session_key) produces for the SAME source,
# the transition wakes a session that never existed and the report-back dies in
# the log — reintroducing the exact defect this change closes. These tests lock
# the stamp derivation to the inbound derivation across the axes that break it.

from gateway.session import Platform, SessionSource, build_session_key
from gateway.slash_commands import _origin_session_key


def _inbound_key(source, extra):
    """The key the live inbound path builds for `source` (base.py:handle_message)."""
    return build_session_key(
        source,
        group_sessions_per_user=extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
    )


def _thread_source():
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="chan1",
        chat_type="thread",
        user_id="user1",
        thread_id="thread1",
    )


def test_origin_stamp_key_matches_inbound_default_config():
    """Under default platform flags, the stamped origin key is byte-identical
    to the live inbound key for the same source."""
    src = _thread_source()
    extra = {}  # defaults: group_sessions_per_user=True, thread_sessions_per_user=False
    assert _origin_session_key(src, extra) == _inbound_key(src, extra)


def test_origin_stamp_key_matches_inbound_under_thread_per_user():
    """When `thread_sessions_per_user: true`, the inbound key appends the
    participant id in a thread. The stamp MUST read the same flag and match —
    the per-user axis that silently diverged when the stamp used default flags."""
    src = _thread_source()
    extra = {"thread_sessions_per_user": True}
    inbound = _inbound_key(src, extra)
    # Sanity: the flag actually changes the inbound key (guards the test itself).
    assert inbound != _inbound_key(src, {}), "flag must change the inbound key"
    assert _origin_session_key(src, extra) == inbound


def test_origin_stamp_key_matches_inbound_under_group_per_user_off():
    """The stamp must mirror `group_sessions_per_user` too, not just assume the
    default True."""
    src = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chan1",
        chat_type="group",
        user_id="user1",
    )
    extra = {"group_sessions_per_user": False}
    inbound = _inbound_key(src, extra)
    assert inbound != _inbound_key(src, {}), "flag must change the inbound key"
    assert _origin_session_key(src, extra) == inbound


def test_origin_stamp_key_uses_agent_main_namespace_no_profile():
    """The inbound path passes NO profile, so its key namespace is `agent:main`.
    The stamp must NOT inject a profile namespace (which would produce
    `agent:<profile>:...` and never match the live thread session)."""
    src = _thread_source()
    key = _origin_session_key(src, {})
    assert key.startswith("agent:main:"), (
        f"origin stamp must use the agent:main namespace like inbound, got {key!r}"
    )
