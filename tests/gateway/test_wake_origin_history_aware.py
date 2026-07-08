"""Woken kanban-transition turns must carry the ORIGIN session's history (C2).

Root cause (proven live 2026-07-07, thread 1523994741836873811): the wake
payload carries the authoritative ``origin_session_id`` (= task.session_id), but
the webhook handler dropped it and the turn resolved its transcript purely from
the coordinate-derived session key. For a Discord thread the live key is
``agent:main:discord:thread:<thread>:<thread>`` (chat==thread), but the persisted
sub can carry the PARENT channel as chat_id — so the coordinate key diverges,
``get_or_create_session`` lands on a phantom/empty session, and
``load_transcript`` returns no live history. The woken turn is context-blind and
posts messages that contradict facts the live session already established.

Fix, in two seams, both reusing the EXISTING resume path (no parallel loader):

1. The webhook stamps ``event.metadata["kanban_origin_session_id"]`` from the
   payload's ``origin_session_id`` (covered in test_webhook_origin_routing.py).
2. ``GatewayRunner._resume_origin_session_for_wake`` — after
   ``get_or_create_session`` resolves the (possibly divergent) session and
   BEFORE ``load_transcript`` — switches the session_key to the authoritative
   ``origin_session_id`` via ``SessionStore.switch_session`` (the same /resume
   mechanism a human follow-up uses), so the live thread's transcript loads.

These assert seam #2: the helper resumes the authoritative session on a
divergent-key wake, is a no-op on the healthy chat==thread case, and never
touches a non-wake message turn.

Run with the hermes venv python:
  uv run pytest tests/gateway/test_wake_origin_history_aware.py -q
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource


class _FakeStore:
    """Minimal SessionStore double recording switch_session calls."""

    def __init__(self, switch_result="__default__"):
        self.switch_calls = []
        self._switch_result = switch_result

    def switch_session(self, session_key, target_session_id):
        self.switch_calls.append((session_key, target_session_id))
        if self._switch_result == "__default__":
            # Emulate the real return: a new entry pointed at the target id.
            return SimpleNamespace(
                session_key=session_key, session_id=target_session_id
            )
        return self._switch_result


def _make_runner(store):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.session_store = store
    return runner


def _wake_event(origin_session_id):
    src = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1515879019269197885",   # PARENT channel (divergent!)
        chat_type="thread",
        thread_id="1523994741836873811",  # the real origin thread
    )
    ev = SimpleNamespace(source=src, metadata={"kanban_transition_wake": True})
    if origin_session_id is not None:
        ev.metadata["kanban_origin_session_id"] = origin_session_id
    return ev


def _entry(session_id):
    return SimpleNamespace(
        session_key="agent:main:discord:thread:1515879019269197885:1523994741836873811",
        session_id=session_id,
    )


def test_divergent_wake_resumes_authoritative_origin_session():
    """C2: a wake whose coordinate-derived session (S_wrong) differs from the
    authoritative origin_session_id (S_live) switches the key to S_live so the
    live transcript loads."""
    store = _FakeStore()
    runner = _make_runner(store)
    event = _wake_event(origin_session_id="20260707_S_live")
    resolved = _entry("20260707_S_wrong")  # what the divergent key resolved to

    out = runner._resume_origin_session_for_wake(
        event, event.source, resolved,
    )

    assert store.switch_calls == [
        (resolved.session_key, "20260707_S_live")
    ], "must switch the resolved key to the authoritative origin session id"
    assert out.session_id == "20260707_S_live", (
        "helper must return the switched entry so load_transcript reads S_live"
    )


def test_matching_ids_is_a_noop_healthy_path():
    """Healthy chat==thread case: coordinate key already resolves to the origin
    session, so no switch fires and the original entry is returned unchanged."""
    store = _FakeStore()
    runner = _make_runner(store)
    event = _wake_event(origin_session_id="20260707_same")
    resolved = _entry("20260707_same")

    out = runner._resume_origin_session_for_wake(
        event, event.source, resolved,
    )

    assert store.switch_calls == [], "must not switch when ids already match"
    assert out is resolved


def test_non_wake_message_is_untouched():
    """A normal (non-wake) message turn must never trigger a resume switch,
    even if some unrelated metadata is present."""
    store = _FakeStore()
    runner = _make_runner(store)
    src = SessionSource(
        platform=Platform.DISCORD, chat_id="c", chat_type="thread", thread_id="t",
    )
    event = SimpleNamespace(source=src, metadata={})  # no wake tag
    resolved = _entry("whatever")

    out = runner._resume_origin_session_for_wake(event, src, resolved)

    assert store.switch_calls == []
    assert out is resolved


def test_wake_without_origin_session_id_falls_through():
    """A wake carrying no origin_session_id keeps today's behavior: no switch,
    original entry returned (backward-compatible)."""
    store = _FakeStore()
    runner = _make_runner(store)
    event = _wake_event(origin_session_id=None)  # tag present, id absent
    resolved = _entry("20260707_wrong")

    out = runner._resume_origin_session_for_wake(event, event.source, resolved)

    assert store.switch_calls == []
    assert out is resolved


def test_switch_returning_none_falls_through_to_resolved():
    """If switch_session can't switch (returns None), the turn must fall back to
    the already-resolved entry rather than crashing or dropping the turn."""
    store = _FakeStore(switch_result=None)
    runner = _make_runner(store)
    event = _wake_event(origin_session_id="20260707_S_live")
    resolved = _entry("20260707_S_wrong")

    out = runner._resume_origin_session_for_wake(event, event.source, resolved)

    assert store.switch_calls == [(resolved.session_key, "20260707_S_live")]
    assert out is resolved, "None switch result must not lose the turn"


def test_helper_swallows_switch_exceptions():
    """A switch_session raising must not break the turn — return the resolved
    entry so the wake still runs (degraded to today's behavior)."""
    store = _FakeStore()
    store.switch_session = MagicMock(side_effect=RuntimeError("boom"))
    runner = _make_runner(store)
    event = _wake_event(origin_session_id="20260707_S_live")
    resolved = _entry("20260707_S_wrong")

    out = runner._resume_origin_session_for_wake(event, event.source, resolved)

    assert out is resolved


# ── E2E against a REAL SessionStore: the loaded transcript carries the ────────
#    origin session's seeded history (C2/C3, the no-contradiction invariant) ───

def test_woken_turn_loads_origin_history_end_to_end(tmp_path, monkeypatch):
    """C2/C3 end to end with a real SessionStore.

    Seed a persisted origin session S whose transcript ESTABLISHES fact X
    ("gateway restarted at 20:12; PR #54 is live"). Simulate a divergent-key
    wake carrying origin_session_id=S. After the resume helper runs, the entry
    it returns must load S's transcript — so the woken turn's model input
    contains the prior-conversation messages and cannot contradict X from stale
    card state alone.
    """
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")

    from gateway.session import SessionStore
    from gateway.config import GatewayConfig

    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())

    # 1) The LIVE origin session S, with a transcript establishing fact X.
    origin_sid = "20260707_201200_livethread"
    store._db.create_session(session_id=origin_sid, source="discord")
    store.append_to_transcript(
        origin_sid,
        {"role": "user", "content": "did you restart the gateway?", "timestamp": 1.0},
    )
    store.append_to_transcript(
        origin_sid,
        {"role": "assistant",
         "content": "Yes — gateway restarted at 20:12; PR #54 is live.",
         "timestamp": 2.0},
    )

    # 2) The wake resolves (via its divergent coordinate source) to a DIFFERENT,
    #    empty session — the phantom the coordinate key lands on.
    wrong_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1515879019269197885",   # PARENT channel (divergent)
        chat_type="thread",
        thread_id="1523994741836873811",
    )
    resolved_entry = store.get_or_create_session(wrong_source)
    # Sanity: the coordinate-resolved session is NOT the live origin one.
    assert resolved_entry.session_id != origin_sid
    assert store.load_transcript(resolved_entry.session_id) == []

    # 3) The wake carries the authoritative origin_session_id. Run the helper.
    runner = _make_runner(store)
    event = SimpleNamespace(
        source=wrong_source,
        metadata={
            "kanban_transition_wake": True,
            "kanban_origin_session_id": origin_sid,
        },
    )
    switched = runner._resume_origin_session_for_wake(
        event, wrong_source, resolved_entry,
    )

    # 4) The turn now points at the live origin session, and load_transcript
    #    (exactly what run.py does before building the model input) returns the
    #    seeded prior messages — the woken turn is history-aware.
    assert switched.session_id == origin_sid
    history = store.load_transcript(switched.session_id)
    contents = [m["content"] for m in history]
    assert "did you restart the gateway?" in contents
    assert any("gateway restarted at 20:12; PR #54 is live" in c for c in contents), (
        "the woken turn's loaded history must contain the established fact X"
    )

