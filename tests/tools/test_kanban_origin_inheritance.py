"""C1–C3: _maybe_auto_subscribe prefers the INHERITED kanban origin.

The card's origin is its ``kanban_notify_subs`` row, stamped at create time by
``_maybe_auto_subscribe``. Historically that row was sourced from the running
process's own ``HERMES_SESSION_*``. That is correct for a card created inside a
live gateway session (root capture) but WRONG once the workstream crosses a spawn
boundary into a detached context (dispatched worker / delegate_task / background
process), where ``HERMES_SESSION_*`` names the detached run, not the human origin.

These tests pin the fixed source-of-truth:

    origin = get_kanban_origin() or capture_kanban_origin_from_session()

- C1 (root capture, no regression): live session, no inherited origin → stamp
  the live surface (byte-identical to today).
- C2 (inheritance across boundary — the core fix): inherited origin set +
  a DETACHED/foreign session → stamp the INHERITED origin, not the detached one.
- C3 (no-origin fallback): neither inherited origin nor live session → behave
  exactly as today (no sub in a CLI/test context).
"""

from __future__ import annotations

import json
import os

import pytest

import gateway.session_context as sc
from gateway.session_context import _VAR_MAP, set_kanban_origin

SESSION_VARS = list(_VAR_MAP.keys())
_ORIGIN_ENV = "HERMES_KANBAN_ORIGIN"


@pytest.fixture(autouse=True)
def _isolate_origin():
    """Reset the origin ContextVar + mirror around each test (worker_env owns HOME)."""
    saved_origin_ctx = sc._KANBAN_ORIGIN.get()
    saved_origin_env = os.environ.get(_ORIGIN_ENV)
    sc._KANBAN_ORIGIN.set(sc._UNSET)
    os.environ.pop(_ORIGIN_ENV, None)
    try:
        yield
    finally:
        sc._KANBAN_ORIGIN.set(saved_origin_ctx)
        if saved_origin_env is None:
            os.environ.pop(_ORIGIN_ENV, None)
        else:
            os.environ[_ORIGIN_ENV] = saved_origin_env


def _list_subs_for_task(task_id):
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        return [dict(r) for r in kb.list_notify_subs(conn, task_id)]
    finally:
        conn.close()


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


def _create(assignee="peer", title="origin-test"):
    from tools import kanban_tools as kt
    out = kt._handle_create({"title": title, "assignee": assignee})
    d = json.loads(out)
    assert d["ok"] is True, d
    return d


def test_c1_root_capture_no_inherited_origin(monkeypatch, worker_env):
    """C1: live session + no inherited origin → stamp the live surface (no regression)."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "ROOTCHAT")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "ROOTTHREAD")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "ROOTUSER")

    d = _create()
    assert d["subscribed"] is True, d
    subs = _list_subs_for_task(d["task_id"])
    assert len(subs) == 1
    s = subs[0]
    assert s["platform"] == "discord"
    assert s["chat_id"] == "ROOTCHAT"
    assert s["thread_id"] == "ROOTTHREAD"
    assert s["user_id"] == "ROOTUSER"


def test_c2_inheritance_beats_detached_session(monkeypatch, worker_env):
    """C2 (core fix): inherited origin wins over the detached run's own identity."""
    # The detached worker's OWN session names a foreign/webhook surface...
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webhook")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "DETACHED_RUN")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "")
    # ...but it inherited the HUMAN origin across the spawn boundary.
    set_kanban_origin(
        platform="discord", chat_id="HUMAN_ORIGIN", thread_id="HUMAN_THREAD",
        user_id="HUMAN_USER",
    )

    d = _create()
    assert d["subscribed"] is True, d
    subs = _list_subs_for_task(d["task_id"])
    assert len(subs) == 1
    s = subs[0]
    assert s["platform"] == "discord", s
    assert s["chat_id"] == "HUMAN_ORIGIN", s
    assert s["thread_id"] == "HUMAN_THREAD", s
    assert s["user_id"] == "HUMAN_USER", s
    # The detached identity must NOT have been stamped.
    assert s["chat_id"] != "DETACHED_RUN"


def test_c3_no_origin_no_session_no_sub(monkeypatch, worker_env):
    """C3: no inherited origin AND no live session → no sub (unchanged CLI behaviour)."""
    for v in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID",
              "HERMES_SESSION_KEY", "HERMES_SESSION_ID"):
        monkeypatch.delenv(v, raising=False)

    d = _create()
    assert d["subscribed"] is False, d
    assert _list_subs_for_task(d["task_id"]) == []
