"""E2E: an inherited-origin (and a reassigned) card's wake targets the right surface.

Exercises the real chain end to end against a temp HERMES_HOME — no mocks of the
units under test:

  dispatcher seed (worker_origin_env → HERMES_KANBAN_ORIGIN)
    → real kanban_create in a DETACHED worker session (foreign HERMES_SESSION_*)
    → real _maybe_auto_subscribe stamps the INHERITED origin as the child's sub
    → real build_transition_payload reads that sub
    → assert the transition-wake body routes to the inherited origin thread.

Then the reassign path: kanban_reassign_origin re-points the card, and a wake
built from the re-pointed sub targets the NEW thread.

This is the actual defect the card targets: a wake for work spawned across the
spawn boundary must land on the human origin surface, not a detached/void one.
"""

from __future__ import annotations

import json
import os

import pytest

import gateway.session_context as sc
from gateway.kanban_transition_emit import build_transition_payload
from gateway.session_context import _VAR_MAP

SESSION_VARS = list(_VAR_MAP.keys())
_ORIGIN_ENV = "HERMES_KANBAN_ORIGIN"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    # Clean origin + session slate.
    saved_ctx = sc._KANBAN_ORIGIN.get()
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    for v in SESSION_VARS + [_ORIGIN_ENV]:
        monkeypatch.delenv(v, raising=False)
    try:
        yield h
    finally:
        sc._KANBAN_ORIGIN.set(saved_ctx)


def _subs(task_id):
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        return [dict(r) for r in kb.list_notify_subs(conn, task_id)]
    finally:
        conn.close()


def _origin_sub(task_id, platform="discord"):
    subs = [s for s in _subs(task_id) if s["platform"] == platform]
    assert len(subs) == 1, subs
    return subs[0]


def test_e2e_inherited_origin_wake_targets_human_thread(home, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # 1) A human-origin card exists with its origin notify-sub (the workstream root).
    conn = kb.connect()
    try:
        root_tid = kb.create_task(conn, title="root", assignee="peer")
        kb.add_notify_sub(
            conn, task_id=root_tid, platform="discord", chat_id="HUMAN_CHAN",
            thread_id="HUMAN_THREAD", user_id="HUMAN_USER", notifier_profile="p",
        )
        # 2) The dispatcher computes the origin seed for the worker it spawns.
        seed = kb.worker_origin_env(conn, root_tid)
    finally:
        conn.close()
    assert seed is not None

    # 3) The worker runs DETACHED: its own session is foreign/contextless, but it
    #    carries the inherited origin via HERMES_KANBAN_ORIGIN (as _default_spawn
    #    seeds it). Simulate that env exactly.
    monkeypatch.setenv(_ORIGIN_ENV, seed)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webhook")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "DETACHED_WORKER")

    # 4) The worker creates a follow-up child card through the REAL tool path.
    out = kt._handle_create({"title": "child-of-fork", "assignee": "peer"})
    d = json.loads(out)
    assert d["ok"] is True and d["subscribed"] is True, d
    child_tid = d["task_id"]

    # 5) The child's origin sub is the INHERITED human surface, not the worker's.
    child_sub = _origin_sub(child_tid)
    assert child_sub["chat_id"] == "HUMAN_CHAN"
    assert child_sub["thread_id"] == "HUMAN_THREAD"

    # 6) The notifier builds a transition wake from that sub → body routes to the
    #    human origin thread (the actual delivery target), NOT a void/webhook one.
    body = build_transition_payload(
        task_id=child_tid, board="default", kind="blocked", reason=None,
        event_id=42, title="child-of-fork",
        origin_platform=child_sub["platform"],
        origin_chat_id=child_sub["chat_id"],
        origin_thread_id=child_sub["thread_id"],
    )
    assert body["origin_platform"] == "discord"
    assert body["origin_chat_id"] == "HUMAN_CHAN"
    assert body["origin_thread_id"] == "HUMAN_THREAD"


def test_e2e_reassigned_origin_wake_targets_new_thread(home, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="fork-me", assignee="peer")
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord", chat_id="CHAN",
            thread_id="OLD_THREAD", notifier_profile="p",
        )
    finally:
        conn.close()

    # Reassign the origin to a freshly-minted thread via the REAL tool.
    out = kt._handle_reassign_origin({
        "task_id": tid, "platform": "discord",
        "chat_id": "CHAN", "thread_id": "NEW_THREAD",
    })
    assert json.loads(out)["ok"] is True

    sub = _origin_sub(tid)
    assert sub["thread_id"] == "NEW_THREAD"

    body = build_transition_payload(
        task_id=tid, board="default", kind="status_changed", reason=None,
        event_id=7,
        origin_platform=sub["platform"],
        origin_chat_id=sub["chat_id"],
        origin_thread_id=sub["thread_id"],
    )
    assert body["origin_thread_id"] == "NEW_THREAD"
    assert body.get("origin_thread_id") != "OLD_THREAD"
