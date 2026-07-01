"""Tests for decoupling the transition-emit (agent wake) from the chat-ping
delivery filter (``NOTIFY_KINDS``).

Root cause proven from a live E2E test (cards t_1c53b632 / t_a1f7320e,
2026-07-01): a card moving ``ready -> running -> review`` fires a
``status_changed`` event. The notifier's chat-ping path only claims events whose
kind is in ``NOTIFY_KINDS`` — and ``status_changed`` is deliberately EXCLUDED
there as high-frequency bookkeeping churn. The transition-emit (agent wake) was
nested inside that same delivery loop, so the wake could only ever fire for a
kind that ALSO chat-pinged. ``status_changed`` therefore never reached the emit
gate, and no wake ever fired on a plain lane move — the exact symptom.

The fix decouples the two: the emit path claims its own events over
``emit_kinds`` on a SEPARATE cursor (``last_emit_event_id``), independent of the
``NOTIFY_KINDS`` chat-ping cursor (``last_event_id``). A ``status_changed`` can
now wake the orchestrator WITHOUT also sending a chat ping.

These tests assert the intended behavior and are RED against the pre-fix code
(there is no ``claim_unseen_emit_events_for_sub`` and no ``last_emit_event_id``
column yet).
"""

from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as k


@pytest.fixture()
def conn(tmp_path):
    dbp = tmp_path / "kanban.db"
    c = k.connect(dbp)
    yield c
    c.close()


def _mk_task_with_sub(conn):
    task_id = k.create_task(
        conn,
        title="test: decouple emit from notify",
        body="# Why\nx\n# What\nx\n# Done when\nx\n# Scope\nx\n",
        assignee="eckert",
        initial_status="running",
    )
    # Register a subscription for the origin thread.
    conn.execute(
        "INSERT OR REPLACE INTO kanban_notify_subs "
        "(task_id, platform, chat_id, thread_id, created_at, last_event_id) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        (task_id, "discord", "123", "123", int(time.time())),
    )
    conn.commit()
    return task_id


# ---------------------------------------------------------------------------
# Schema: the emit path needs its OWN cursor
# ---------------------------------------------------------------------------

def test_notify_subs_has_emit_cursor_column(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(kanban_notify_subs)")}
    assert "last_emit_event_id" in cols, (
        "kanban_notify_subs must carry a separate emit cursor so the wake path "
        "claims independently of the chat-ping cursor"
    )


# ---------------------------------------------------------------------------
# The emit claim must see status_changed even though the ping claim does not
# ---------------------------------------------------------------------------

def test_emit_claim_sees_status_changed(conn):
    task_id = _mk_task_with_sub(conn)
    # Append a status_changed event (a plain lane move: ready -> review).
    k._append_event(conn, task_id, "status_changed", payload={"to_status": "review"})
    conn.commit()

    # The EMIT claim, filtering on emit_kinds including status_changed, MUST
    # return it — on its own cursor.
    old, new, events = k.claim_unseen_emit_events_for_sub(
        conn,
        task_id=task_id,
        platform="discord",
        chat_id="123",
        thread_id="123",
        kinds=("status_changed", "assigned", "blocked"),
    )
    kinds = [e.kind for e in events]
    assert "status_changed" in kinds, (
        "the emit claim must surface status_changed for the wake path"
    )
    assert new > old


def test_emit_and_ping_cursors_are_independent(conn):
    """A ping claim advancing its cursor must NOT consume the emit path's view,
    and vice-versa — the two run on separate cursors."""
    task_id = _mk_task_with_sub(conn)
    # A status_changed (emit-only) followed by a blocked (both ping + emit).
    k._append_event(conn, task_id, "status_changed", payload={"to_status": "review"})
    k._append_event(conn, task_id, "blocked", payload={"reason": "changes"})
    conn.commit()

    # Ping claim over NOTIFY_KINDS-like set (no status_changed) advances the
    # ping cursor past both events (cursor jumps to max matching id = blocked).
    p_old, p_new, p_events = k.claim_unseen_events_for_sub(
        conn,
        task_id=task_id,
        platform="discord",
        chat_id="123",
        thread_id="123",
        kinds=("blocked",),
    )
    assert [e.kind for e in p_events] == ["blocked"]

    # The EMIT claim, on its OWN cursor, must STILL see status_changed even
    # though the ping cursor already advanced past it.
    e_old, e_new, e_events = k.claim_unseen_emit_events_for_sub(
        conn,
        task_id=task_id,
        platform="discord",
        chat_id="123",
        thread_id="123",
        kinds=("status_changed", "blocked"),
    )
    e_kinds = [e.kind for e in e_events]
    assert "status_changed" in e_kinds, (
        "emit cursor must be independent of the ping cursor — status_changed "
        "must not be swallowed by the ping claim advancing last_event_id"
    )


def test_emit_claim_advances_only_emit_cursor(conn):
    task_id = _mk_task_with_sub(conn)
    k._append_event(conn, task_id, "status_changed", payload={"to_status": "review"})
    conn.commit()

    before = conn.execute(
        "SELECT last_event_id, last_emit_event_id FROM kanban_notify_subs "
        "WHERE task_id=? AND platform='discord' AND chat_id='123' AND thread_id='123'",
        (task_id,),
    ).fetchone()

    k.claim_unseen_emit_events_for_sub(
        conn,
        task_id=task_id,
        platform="discord",
        chat_id="123",
        thread_id="123",
        kinds=("status_changed",),
    )

    after = conn.execute(
        "SELECT last_event_id, last_emit_event_id FROM kanban_notify_subs "
        "WHERE task_id=? AND platform='discord' AND chat_id='123' AND thread_id='123'",
        (task_id,),
    ).fetchone()

    # The ping cursor is untouched; only the emit cursor advances.
    assert after["last_event_id"] == before["last_event_id"]
    assert after["last_emit_event_id"] > before["last_emit_event_id"]
