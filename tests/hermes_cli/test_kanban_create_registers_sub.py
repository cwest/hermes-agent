"""`hermes kanban create --session-id` must register the notify subscription.

Regression coverage for the "CLI-filed cards never wake back to their origin
thread" bug (live evidence 2026-07-07): three cards filed via
``hermes kanban create ... --session-id discord:<chan>:<thread>`` stored the
origin ``session_id`` on the task row but had NO ``kanban_notify_subs`` row, so
the per-card thread-targeted wake had nothing to fire from — the channel-level
default sub fired instead, landing the wake in the parent channel (thread=None).

The gate path (``onecard_common.submit_card``) and the gateway ``/kanban
create`` path both materialize the notify sub; the bare CLI ``create`` path did
not. This locks the contract that ``--session-id`` on the CLI registers the
same sub, so CLI-filed and gate-filed cards behave identically.

Two coupled requirements, both covered here:

1. PRIMARY — a CLI ``create --session-id`` creates exactly one
   ``kanban_notify_subs`` row with platform/chat_id/thread_id populated.
2. SECONDARY — a sub-thread origin (parent channel != thread) must resolve to
   the THREAD session key, matching the live inbound Discord thread key
   ``agent:main:discord:thread:<thread>:<thread>``, not the parent channel.

Run with the hermes venv python:
  scripts/run_tests.sh tests/hermes_cli/test_kanban_create_registers_sub.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _created_id(out: str) -> str:
    m = re.search(r"Created\s+(t_[0-9a-f]+)\b", out)
    assert m, f"no task id in create output: {out!r}"
    return m.group(1)


# ── PRIMARY: CLI create --session-id registers a notify sub ──────────────────

def test_cli_create_with_thread_session_id_registers_sub(kanban_home):
    """The exact live-bug shape: a card filed via the CLI with a Discord
    sub-thread session-id (parent channel != thread) must gain a notify sub."""
    chan = "1515879019269197885"
    thread = "1523994741836873811"
    sid = f"discord:{chan}:{thread}"

    out = kc.run_slash(f"create 'wake-me' --assignee eckert --session-id {sid}")
    tid = _created_id(out)

    conn = kb.connect()
    try:
        subs = [s for s in kb.list_notify_subs(conn) if s["task_id"] == tid]
        assert len(subs) == 1, (
            f"CLI create --session-id must register exactly one notify sub, "
            f"got {subs!r}"
        )
        sub = subs[0]
        assert sub["platform"] == "discord"
        # SECONDARY defect: a sub-thread must resolve to the THREAD session key,
        # so both chat_id and thread_id are the thread id — mirroring the live
        # inbound Discord thread source (chat_id == thread_id == thread id).
        assert sub["chat_id"] == thread, (
            "sub-thread origin must store the thread id as chat_id so the wake "
            "key mirrors the live inbound thread key; got "
            f"chat_id={sub['chat_id']!r}"
        )
        assert sub["thread_id"] == thread, (
            f"sub must carry the thread id; got thread_id={sub['thread_id']!r}"
        )
    finally:
        conn.close()


def test_cli_create_with_canonical_session_key_registers_sub(kanban_home):
    """The canonical F2 form (a full ``agent:main:...`` session key) must also
    register a sub with the right thread columns."""
    thread = "123456789"
    sid = f"agent:main:discord:thread:{thread}:{thread}"

    out = kc.run_slash(f"create 'canonical' --assignee eckert --session-id {sid}")
    tid = _created_id(out)

    conn = kb.connect()
    try:
        subs = [s for s in kb.list_notify_subs(conn) if s["task_id"] == tid]
        assert len(subs) == 1, f"expected one sub, got {subs!r}"
        assert subs[0]["platform"] == "discord"
        assert subs[0]["chat_id"] == thread
        assert subs[0]["thread_id"] == thread
    finally:
        conn.close()


def test_cli_create_channel_session_id_registers_channel_sub(kanban_home):
    """A genuinely channel-born card (no thread) subscribes its channel with an
    empty thread_id — the fallback stays functional, no regression."""
    chan = "1515879019269197885"
    sid = f"discord:{chan}"

    out = kc.run_slash(f"create 'channel-born' --assignee casey --session-id {sid}")
    tid = _created_id(out)

    conn = kb.connect()
    try:
        subs = [s for s in kb.list_notify_subs(conn) if s["task_id"] == tid]
        assert len(subs) == 1, f"expected one channel sub, got {subs!r}"
        assert subs[0]["chat_id"] == chan
        assert (subs[0]["thread_id"] or "") == ""
    finally:
        conn.close()


def test_cli_create_without_session_id_registers_no_sub(kanban_home):
    """No --session-id => no sub. Must not regress the no-origin case."""
    out = kc.run_slash("create 'no-origin' --assignee eckert")
    tid = _created_id(out)

    conn = kb.connect()
    try:
        subs = [s for s in kb.list_notify_subs(conn) if s["task_id"] == tid]
        assert subs == [], f"a card with no origin must have no sub, got {subs!r}"
    finally:
        conn.close()


# ── SECONDARY (E2E): sub columns resolve to the ORIGIN thread session key ─────

def test_sub_thread_resolves_to_thread_session_key_not_parent_channel(kanban_home):
    """The whole chain: CLI create --session-id (parent chan != thread) -> sub
    -> transition target -> _build_origin_source -> build_session_key must land
    on the THREAD session key the live inbound path produces, NOT the parent
    channel. This is the chat_id != thread_id case the card calls out.
    """
    from gateway.kanban_transition_emit import resolve_transition_target
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.session import build_session_key

    chan = "1515879019269197885"
    thread = "1523994741836873811"
    sid = f"discord:{chan}:{thread}"

    out = kc.run_slash(f"create 'e2e' --assignee eckert --session-id {sid}")
    tid = _created_id(out)

    conn = kb.connect()
    try:
        sub = [s for s in kb.list_notify_subs(conn) if s["task_id"] == tid][0]
        task = kb.get_task(conn, tid)
    finally:
        conn.close()

    # The notifier reads chat_id/thread_id verbatim off the sub row.
    target = resolve_transition_target(
        session_id=task.session_id,
        sub={
            "platform": sub["platform"],
            "chat_id": sub["chat_id"],
            "thread_id": sub["thread_id"],
        },
        default_channel=chan,
    )
    assert target["is_fallback"] is False
    assert target["thread_id"] == thread

    # _build_origin_source + build_session_key must yield the live thread key.
    adapter = object.__new__(WebhookAdapter)
    source = adapter._build_origin_source(
        target["platform"], target["chat_id"], target["thread_id"],
    )
    assert source is not None, "origin source must resolve for a thread sub"
    key = build_session_key(source)
    assert key == f"agent:main:discord:thread:{thread}:{thread}", (
        "the wake must target the live inbound thread session key "
        f"(chat_id == thread_id == {thread}), got {key!r} — a parent-channel "
        "chat_id would send the wake to the wrong session"
    )


# ── parse_origin_session: the shared resolver's behavior contract ─────────────

@pytest.mark.parametrize(
    "session_id,expected",
    [
        # Bare triple with a sub-thread: thread-mirror sets chat_id := thread.
        ("discord:1515879019269197885:1523994741836873811",
         ("discord", "1523994741836873811", "1523994741836873811")),
        # Bare pair (channel-born): no thread; chat_id stays the channel.
        ("discord:1515879019269197885",
         ("discord", "1515879019269197885", None)),
        # Canonical thread session key: thread-mirror already holds.
        ("agent:main:discord:thread:123:123",
         ("discord", "123", "123")),
        # Canonical channel key: the 6th element (a user id) is NOT a thread.
        ("agent:main:discord:channel:999:user42",
         ("discord", "999", None)),
        # Canonical dm key: 6th element IS an unambiguous thread id.
        ("agent:main:telegram:dm:55:77",
         ("telegram", "77", "77")),
        # Empty / unusable inputs => None (no sub).
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_parse_origin_session_contract(session_id, expected):
    assert kb.parse_origin_session(session_id) == expected


def test_cli_create_bad_session_id_does_not_crash(kanban_home):
    """A malformed --session-id must not fail the create nor leave a sub."""
    out = kc.run_slash("create 'weird' --assignee eckert --session-id notaformat")
    tid = _created_id(out)
    conn = kb.connect()
    try:
        subs = [s for s in kb.list_notify_subs(conn) if s["task_id"] == tid]
        assert subs == [], f"an unusable session-id must yield no sub, got {subs!r}"
    finally:
        conn.close()

