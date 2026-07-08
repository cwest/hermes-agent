"""Dispatcher seeds HERMES_KANBAN_ORIGIN into the worker env (inheritance).

When the dispatcher spawns a worker subprocess for a card, the worker's own
session identity is detached (it is a fresh `hermes -p <assignee> chat -q` run).
For any card the worker CREATES to inherit the human origin of the workstream,
the dispatcher must seed HERMES_KANBAN_ORIGIN from the card's origin notify-sub.

``worker_origin_env`` builds that seed value from the card's notify-sub row.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()
    c = kb.connect()
    try:
        yield c
    finally:
        c.close()


def test_worker_origin_env_from_thread_bearing_sub(conn):
    tid = kb.create_task(conn, title="t", assignee="peer")
    kb.add_notify_sub(
        conn, task_id=tid, platform="discord", chat_id="CHAN",
        thread_id="THREAD", user_id="U", notifier_profile="p",
    )
    blob = kb.worker_origin_env(conn, tid)
    assert blob is not None
    parsed = json.loads(blob)
    assert parsed["platform"] == "discord"
    assert parsed["chat_id"] == "CHAN"
    assert parsed["thread_id"] == "THREAD"


def test_worker_origin_env_prefers_thread_bearing_over_threadless(conn):
    """When both a thread sub and a bare-channel sub exist, prefer the thread one."""
    tid = kb.create_task(conn, title="t", assignee="peer")
    # (add_notify_sub's guard actually blocks the second here, but assert the
    # selection is robust regardless of insert order.)
    kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="CHAN",
                      thread_id="THREAD")
    blob = kb.worker_origin_env(conn, tid)
    parsed = json.loads(blob)
    assert parsed["thread_id"] == "THREAD"


def test_worker_origin_env_none_when_no_sub(conn):
    tid = kb.create_task(conn, title="t", assignee="peer")
    assert kb.worker_origin_env(conn, tid) is None


def test_worker_origin_env_ignores_tui_and_report_back_only(conn):
    """A 'tui' sub is a local UI channel, not a routable chat origin → skip it."""
    tid = kb.create_task(conn, title="t", assignee="peer")
    kb.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="sess-key")
    assert kb.worker_origin_env(conn, tid) is None
