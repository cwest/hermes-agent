"""Egress: a kanban-transition wake must reach the ORIGIN thread, not Home.

Regression coverage for the "pipeline goes dark" bug (live evidence
2026-07-06, research thread 1523789197662748683): work runs and cards move,
but every transition wake dispatched to Casey's Home channel with thread=None
instead of the origin thread the card was born in.

Root cause proven against the live board: the wake destination is taken
verbatim from the notify-subscription ROW, and a thread-born card ends up with
TWO subscription rows —

  1. the correct origin sub   ``discord:<chat>:<thread>``   (stamped at filing)
  2. a thread-less Home sub    ``discord:<home>:``           (added later, e.g.
     by the review-stage defensive re-subscribe to the Home channel)

The emit-wake loop fires ONE wake per sub row, so the card double-fires: one
wake to the origin thread (correct) and one to Home/thread=None (dark to Casey).

Two coupled fixes, both unit-tested here:

A. ``add_notify_sub`` must NOT create a thread-less channel sub for a
   (task, platform) that already has a thread-bearing sub — the write that
   introduces the second, dark-routing row.

B. ``dedupe_wake_subs`` collapses a card's subs to a single wake target,
   preferring the concrete-origin (thread-bearing) sub over a thread-less
   fallback — so even a legacy DB that already has both rows fires exactly one
   wake, to the origin thread.

Run with the hermes venv python:
  uv run pytest tests/gateway/test_wake_origin_thread_routing.py -q
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb


# ── A: add_notify_sub must not add a thread-less sub over a thread sub ────────

def test_threadless_channel_sub_is_skipped_when_thread_sub_exists(tmp_path, monkeypatch):
    """A thread-born card already carrying ``discord:<chat>:<thread>`` must NOT
    gain a second thread-less ``discord:<chat>:`` row when something (the
    review-stage Home re-subscribe) subscribes the bare channel again.

    Without the guard the card ends with two rows and the wake double-fires,
    one of them to Home/thread=None — the dark-to-Casey wake.
    """
    db_path = tmp_path / "dedupe_write.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="thread-born", assignee="hollis")
        # 1) origin sub stamped at filing: chat + thread both set.
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord",
            chat_id="1523789197662748683", thread_id="1523789197662748683",
            notifier_profile="default",
        )
        # 2) later, the review stage re-subscribes the BARE channel (no thread).
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord",
            chat_id="1523789197662748683", thread_id=None,
            notifier_profile="default",
        )

        rows = [s for s in kb.list_notify_subs(conn) if s["task_id"] == tid]
        # Exactly one row must survive: the thread-bearing one.
        assert len(rows) == 1, (
            f"expected the thread-less channel sub to be skipped, got rows: {rows}"
        )
        assert rows[0]["thread_id"] == "1523789197662748683", (
            "the surviving row must be the concrete-origin (thread-bearing) sub"
        )
    finally:
        conn.close()


def test_threadless_channel_sub_still_allowed_for_a_channel_born_card(tmp_path, monkeypatch):
    """The guard must NOT block a legitimately channel-born card (no thread
    origin) from subscribing its channel — only the thread-less-over-thread case
    is suppressed."""
    db_path = tmp_path / "dedupe_write_channel.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="channel-born", assignee="casey")
        kb.add_notify_sub(
            conn, task_id=tid, platform="discord",
            chat_id="1515879019269197885", thread_id=None,
            notifier_profile="default",
        )
        rows = [s for s in kb.list_notify_subs(conn) if s["task_id"] == tid]
        assert len(rows) == 1
        assert (rows[0]["thread_id"] or "") == ""
    finally:
        conn.close()


# ── B: dedupe_wake_subs collapses to one wake target, origin-thread preferred ─

def test_dedupe_wake_subs_prefers_thread_bearing_sub():
    """Given both a thread sub and a thread-less Home sub for the SAME card,
    the wake collector must yield exactly ONE sub — the thread-bearing one — so
    the card fires a single wake to its origin thread, never a second to Home."""
    from gateway.kanban_transition_emit import dedupe_wake_subs

    thread_sub = {
        "task_id": "t_x", "platform": "discord",
        "chat_id": "1523789197662748683", "thread_id": "1523789197662748683",
    }
    home_sub = {
        "task_id": "t_x", "platform": "discord",
        "chat_id": "1515879019269197885", "thread_id": "",
    }

    # Order must not matter: home sub first…
    out = dedupe_wake_subs([home_sub, thread_sub])
    assert len(out) == 1
    assert out[0]["thread_id"] == "1523789197662748683"
    assert out[0]["chat_id"] == "1523789197662748683"

    # …and thread sub first.
    out2 = dedupe_wake_subs([thread_sub, home_sub])
    assert len(out2) == 1
    assert out2[0]["thread_id"] == "1523789197662748683"


def test_dedupe_wake_subs_keeps_distinct_cards_and_distinct_threads():
    """Dedup is per (task_id, platform); distinct cards and distinct real
    threads are all preserved — the collapse only removes the thread-less
    duplicate of a card that also has a thread-bearing sub."""
    from gateway.kanban_transition_emit import dedupe_wake_subs

    subs = [
        {"task_id": "t_a", "platform": "discord", "chat_id": "111", "thread_id": "111"},
        {"task_id": "t_b", "platform": "discord", "chat_id": "222", "thread_id": ""},
        {"task_id": "t_c", "platform": "discord", "chat_id": "333", "thread_id": "333"},
        {"task_id": "t_c", "platform": "discord", "chat_id": "999", "thread_id": ""},
    ]
    out = dedupe_wake_subs(subs)
    by_task = {s["task_id"]: s for s in out}
    assert set(by_task) == {"t_a", "t_b", "t_c"}
    assert by_task["t_a"]["thread_id"] == "111"
    assert by_task["t_b"]["thread_id"] == ""      # only a channel sub — kept
    assert by_task["t_c"]["thread_id"] == "333"   # thread-bearing preferred


def test_dedupe_wake_subs_two_real_threads_same_card_are_both_kept():
    """If a card genuinely has two DISTINCT thread subs (two watchers), both are
    kept — dedup only discards the thread-LESS duplicate, never a real thread."""
    from gateway.kanban_transition_emit import dedupe_wake_subs

    subs = [
        {"task_id": "t_m", "platform": "discord", "chat_id": "10", "thread_id": "10"},
        {"task_id": "t_m", "platform": "discord", "chat_id": "20", "thread_id": "20"},
    ]
    out = dedupe_wake_subs(subs)
    assert len(out) == 2
    assert {s["thread_id"] for s in out} == {"10", "20"}
