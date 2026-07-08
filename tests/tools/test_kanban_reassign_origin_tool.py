"""D3: the kanban_reassign_origin worker tool.

Exposes reassign_task_origin to an orchestrator that mints a new thread for a
fork and designates it the origin. Beyond re-pointing the card's notify-sub, the
tool refreshes the caller's HERMES_KANBAN_ORIGIN so *subsequently* created child
cards inherit the reassigned surface (D3).
"""

from __future__ import annotations

import json
import os

import pytest

import gateway.session_context as sc
from gateway.session_context import get_kanban_origin

_ORIGIN_ENV = "HERMES_KANBAN_ORIGIN"


@pytest.fixture(autouse=True)
def _isolate_origin():
    saved_ctx = sc._KANBAN_ORIGIN.get()
    saved_env = os.environ.get(_ORIGIN_ENV)
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    os.environ.pop(_ORIGIN_ENV, None)
    try:
        yield
    finally:
        sc._KANBAN_ORIGIN.set(saved_ctx)
        if saved_env is None:
            os.environ.pop(_ORIGIN_ENV, None)
        else:
            os.environ[_ORIGIN_ENV] = saved_env


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    yield home


def _subs(task_id):
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        return [dict(r) for r in kb.list_notify_subs(conn, task_id)]
    finally:
        conn.close()


def _make_task():
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        return kb.create_task(conn, title="fork", assignee="peer")
    finally:
        conn.close()


def test_reassign_origin_tool_repoints_sub(monkeypatch, worker_env):
    from tools import kanban_tools as kt
    tid = _make_task()
    # seed an old origin
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        kb.add_notify_sub(conn, task_id=tid, platform="discord",
                          chat_id="CHAN", thread_id="OLD", notifier_profile="p")
    finally:
        conn.close()

    out = kt._handle_reassign_origin({
        "task_id": tid, "platform": "discord",
        "chat_id": "CHAN", "thread_id": "NEW",
    })
    d = json.loads(out)
    assert d["ok"] is True, d

    subs = [s for s in _subs(tid) if s["platform"] == "discord"]
    assert len(subs) == 1
    assert subs[0]["thread_id"] == "NEW"


def test_reassign_origin_tool_refreshes_context_for_future_children(monkeypatch, worker_env):
    """D3: after reassign, the caller's origin points at the new surface."""
    from tools import kanban_tools as kt
    tid = _make_task()

    out = kt._handle_reassign_origin({
        "task_id": tid, "platform": "discord",
        "chat_id": "NEWCHAN", "thread_id": "NEWTHREAD", "user_id": "U",
    })
    assert json.loads(out)["ok"] is True

    origin = get_kanban_origin()
    assert origin is not None
    assert origin["platform"] == "discord"
    assert origin["chat_id"] == "NEWCHAN"
    assert origin["thread_id"] == "NEWTHREAD"


def test_reassign_origin_tool_requires_task_platform_chat(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_reassign_origin({"task_id": "t_x"}))
    assert "error" in d and d.get("ok") is not True
