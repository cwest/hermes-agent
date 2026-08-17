"""The ``created`` task_event payload records the resolved card ORIGIN.

The defect class this closes: a card's origin is stamped from ambient process
state onto a MUTABLE ``tasks`` / ``kanban_notify_subs`` row, but the ``created``
event payload recorded NO origin at all. When a card was stamped with the WRONG
origin (a foreign thread leaking across a spawn boundary), there was no audit
trail — reading the card's event history could not reconstruct which caller
filed it or what origin it was given at creation. This blocked a real
post-mortem.

The fix records the resolved origin in the ``created`` event payload at creation
time: ``platform / chat_id / thread_id / session_id``, plus how it was resolved
(``origin_source``: inherited from the env mirror vs. captured from a live
session vs. none) and the ``origin_pid`` of the creating process.

These are BEHAVIOR CONTRACTS keyed on the resolution ORDER (inherit beats live
capture), not on snapshotted teammate names. Each test carries a witnessed
NEGATIVE CONTROL: with the origin never bound and no live session, the payload's
origin fields must be absent/None — so removing the change (which would leave the
inherited/captured paths unrecorded) turns the positive tests red.

Run:
  scripts/run_tests.sh tests/hermes_cli/test_kanban_created_event_origin.py -q
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import gateway.session_context as sc
from gateway.session_context import set_kanban_origin
from hermes_cli import kanban_db as kb

_ORIGIN_ENV = "HERMES_KANBAN_ORIGIN"
_SESSION_VARS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_THREAD_ID",
    "HERMES_SESSION_USER_ID",
    "HERMES_SESSION_ID",
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def _isolate_origin(monkeypatch):
    """Reset the origin ContextVar + mirror and clear session vars per test."""
    saved_ctx = sc._KANBAN_ORIGIN.get()
    saved_env = os.environ.get(_ORIGIN_ENV)
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    os.environ.pop(_ORIGIN_ENV, None)
    for v in _SESSION_VARS:
        monkeypatch.delenv(v, raising=False)
    try:
        yield
    finally:
        sc._KANBAN_ORIGIN.set(saved_ctx)
        if saved_env is None:
            os.environ.pop(_ORIGIN_ENV, None)
        else:
            os.environ[_ORIGIN_ENV] = saved_env


def _created_payload(conn, task_id) -> dict:
    for ev in kb.list_events(conn, task_id):
        if ev.kind == "created":
            assert ev.payload is not None, "created event carried no payload"
            return ev.payload
    raise AssertionError("no created event found")


def test_created_payload_records_live_session_capture(kanban_home, monkeypatch):
    """ROOT capture path: no inherited origin but a live session → the created
    payload snapshots the live surface and marks origin_source=live_session."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "ROOTCHAT")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "ROOTTHREAD")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "ROOTUSER")
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-root")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="live capture", kind="code")
        pl = _created_payload(conn, tid)
        assert pl["origin_platform"] == "discord", pl
        assert pl["origin_chat_id"] == "ROOTCHAT", pl
        assert pl["origin_thread_id"] == "ROOTTHREAD", pl
        assert pl["origin_session_id"] == "sess-root", pl
        assert pl["origin_source"] == "live_session", pl
        assert pl["origin_pid"] == os.getpid(), pl
    finally:
        conn.close()


def test_created_payload_records_inherited_origin(kanban_home, monkeypatch):
    """INHERIT path (the core audit fix): an origin bound across the spawn
    boundary is recorded in the payload as origin_source=inherited, NOT the
    detached run's own session identity."""
    # The detached run's OWN session names a foreign/webhook surface...
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webhook")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "DETACHED_RUN")
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-detached")
    # ...but it inherited the HUMAN origin across the spawn boundary.
    set_kanban_origin(
        platform="discord", chat_id="HUMAN_ORIGIN", thread_id="HUMAN_THREAD",
        user_id="HUMAN_USER", session_id="sess-human",
    )

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="inherited", kind="code")
        pl = _created_payload(conn, tid)
        assert pl["origin_platform"] == "discord", pl
        assert pl["origin_chat_id"] == "HUMAN_ORIGIN", pl
        assert pl["origin_thread_id"] == "HUMAN_THREAD", pl
        assert pl["origin_session_id"] == "sess-human", pl
        assert pl["origin_source"] == "inherited", pl
        assert pl["origin_pid"] == os.getpid(), pl
        # The detached identity must NOT have been recorded.
        assert pl["origin_chat_id"] != "DETACHED_RUN", pl
    finally:
        conn.close()


def test_created_payload_no_origin_when_detached(kanban_home):
    """NEGATIVE CONTROL: neither inherited origin nor a live session → the
    payload records no routable origin (source=none, fields None). This is the
    witness: if the inherit/capture recording were removed, the two positive
    tests above would fall to this same all-None shape and go red."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="detached", kind="code")
        pl = _created_payload(conn, tid)
        assert pl["origin_source"] == "none", pl
        assert pl["origin_platform"] is None, pl
        assert pl["origin_chat_id"] is None, pl
        assert pl["origin_thread_id"] is None, pl
        assert pl["origin_session_id"] is None, pl
        # pid is still recorded — the creating process is always knowable.
        assert pl["origin_pid"] == os.getpid(), pl
    finally:
        conn.close()


def test_event_history_alone_reconstructs_origin(kanban_home, monkeypatch):
    """Done-when contract: reading the event history is sufficient to determine
    the filed origin WITHOUT consulting the mutable tasks column."""
    set_kanban_origin(
        platform="slack", chat_id="C123", thread_id="T456", session_id="s-1",
    )
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="from-events", kind="code")
        pl = _created_payload(conn, tid)
        # Reconstruct purely from the event payload.
        reconstructed = (
            pl["origin_platform"], pl["origin_chat_id"], pl["origin_thread_id"],
        )
        assert reconstructed == ("slack", "C123", "T456"), pl
    finally:
        conn.close()
