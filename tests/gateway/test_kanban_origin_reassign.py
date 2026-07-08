"""D1/D2: reassign_task_origin — atomically re-point a card's origin.

When a workstream forks, an orchestrator mints a new thread and designates it the
origin for future wakes. The DB primitive re-points the card's notify-sub(s) for
a platform to a new ``(chat_id, thread_id)``:

- D1 (reassign): existing same-platform sub(s) are replaced by exactly the new
  surface; the new sub's cursor is seeded to the latest existing event id so a
  transition wake after the re-point resolves to the NEW surface and NO
  historical event is replayed.
- D2 (idempotent): re-pointing to the current surface leaves the row (and its
  live cursor) unchanged — no rewind, no history flood.

Deleting only the SAME-platform rows preserves multi-platform fan-out subs and
sidesteps the thread-less MISROUTE guard (which only blocks *adds*).
"""

from __future__ import annotations

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


def _subs(conn, task_id):
    return [dict(r) for r in kb.list_notify_subs(conn, task_id)]


def _make_task_with_events(conn, n_events=3):
    tid = kb.create_task(conn, title="fork me", assignee="peer")
    # Generate some history so we can assert the cursor seed suppresses replay.
    for i in range(n_events):
        kb.add_comment(conn, tid, author="tester", body=f"event {i}")
    return tid


def test_d1_reassign_replaces_same_platform_sub_and_seeds_cursor(conn):
    tid = _make_task_with_events(conn)
    # Original origin: an old thread.
    kb.add_notify_sub(
        conn, task_id=tid, platform="discord", chat_id="CHAN",
        thread_id="OLD_THREAD", user_id="U", notifier_profile="p",
    )
    # A different-platform sub that must be PRESERVED.
    kb.add_notify_sub(
        conn, task_id=tid, platform="telegram", chat_id="TG", thread_id="TG_T",
    )

    latest_before = max(e.id for e in kb.list_events(conn, tid))

    row = kb.reassign_task_origin(
        conn, task_id=tid, platform="discord", chat_id="CHAN",
        thread_id="NEW_THREAD", user_id="U", notifier_profile="p",
    )

    subs = _subs(conn, tid)
    discord = [s for s in subs if s["platform"] == "discord"]
    telegram = [s for s in subs if s["platform"] == "telegram"]

    # Exactly one discord sub, pointing at the NEW thread; old thread gone.
    assert len(discord) == 1, subs
    assert discord[0]["thread_id"] == "NEW_THREAD"
    assert all(s["thread_id"] != "OLD_THREAD" for s in discord)
    # Multi-platform fan-out preserved.
    assert len(telegram) == 1 and telegram[0]["thread_id"] == "TG_T"
    # Cursor seeded to the latest existing event → no history replay.
    assert discord[0]["last_event_id"] >= latest_before
    # Return value describes the new row.
    assert row["platform"] == "discord" and row["thread_id"] == "NEW_THREAD"


def test_d2_reassign_to_current_surface_is_noop(conn):
    tid = _make_task_with_events(conn)
    kb.add_notify_sub(
        conn, task_id=tid, platform="discord", chat_id="CHAN",
        thread_id="THREAD", notifier_profile="p",
    )
    # Advance the cursor to a live value to prove it is not rewound.
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = 999 "
            "WHERE task_id = ? AND platform = 'discord'",
            (tid,),
        )

    kb.reassign_task_origin(
        conn, task_id=tid, platform="discord", chat_id="CHAN",
        thread_id="THREAD", notifier_profile="p",
    )

    subs = [s for s in _subs(conn, tid) if s["platform"] == "discord"]
    assert len(subs) == 1
    assert subs[0]["thread_id"] == "THREAD"
    # Idempotent: the live cursor must not be rewound.
    assert subs[0]["last_event_id"] == 999


def test_d1_reassign_when_no_prior_sub_creates_one(conn):
    """Re-pointing a card that had no origin sub yet just creates the new one."""
    tid = _make_task_with_events(conn)
    row = kb.reassign_task_origin(
        conn, task_id=tid, platform="discord", chat_id="CHAN",
        thread_id="NEW", notifier_profile="p",
    )
    subs = [s for s in _subs(conn, tid) if s["platform"] == "discord"]
    assert len(subs) == 1
    assert subs[0]["thread_id"] == "NEW"
    assert row["thread_id"] == "NEW"
