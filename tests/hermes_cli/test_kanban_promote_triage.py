"""``promote`` is the operator's exit from ``triage``.

A card lands in ``triage`` when it is filed for specification or when the
block-loop breaker escalates it. ``specify`` and ``decompose`` leave triage
only through an auxiliary LLM rewrite of the card, so a card whose spec is
already fine had no deterministic way out short of editing the database.
``promote`` now accepts ``triage`` beside ``todo`` and ``blocked``, with the
same parent gate and audit event.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as c:
        yield c


def test_promote_moves_a_triage_card_to_ready_with_an_audit_event(conn) -> None:
    tid = kb.create_task(conn, title="filed for triage", assignee="dev", triage=True)
    assert kb.get_task(conn, tid).status == "triage"

    ok, err = kb.promote_task(conn, tid, actor="ops", reason="spec is fine")

    assert (ok, err) == (True, None)
    assert kb.get_task(conn, tid).status == "ready"
    payload = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'promoted_manual'", (tid,),
    ).fetchone()["payload"]
    assert json.loads(payload) == {"actor": "ops", "reason": "spec is fine", "from": "triage"}


def test_promote_recovers_a_card_the_block_loop_breaker_escalated(conn) -> None:
    tid = kb.create_task(conn, title="loops", assignee="dev")
    for _ in range(kb.BLOCK_RECURRENCE_LIMIT + 1):
        if kb.get_task(conn, tid).status == "triage":
            break
        assert kb.block_task(conn, tid, reason="same question", kind="needs_input")
        if kb.get_task(conn, tid).status == "blocked":
            assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "triage"

    ok, err = kb.promote_task(conn, tid, actor="ops")

    assert (ok, err) == (True, None)
    assert kb.get_task(conn, tid).status == "ready"


def test_a_triage_card_with_an_unfinished_parent_stays_in_triage(conn) -> None:
    parent = kb.create_task(conn, title="parent", assignee="dev")
    child = kb.create_task(conn, title="child", parents=[parent], assignee="dev", triage=True)

    ok, err = kb.promote_task(conn, child, actor="ops")

    assert ok is False and parent in err
    assert kb.get_task(conn, child).status == "triage"


def test_dry_run_leaves_the_triage_card_alone(conn) -> None:
    tid = kb.create_task(conn, title="filed for triage", assignee="dev", triage=True)
    assert kb.promote_task(conn, tid, actor="ops", dry_run=True) == (True, None)
    assert kb.get_task(conn, tid).status == "triage"


def test_the_refusal_names_every_promotable_status(conn) -> None:
    tid = kb.create_task(conn, title="already ready", assignee="dev")
    assert kb.get_task(conn, tid).status == "ready"
    ok, err = kb.promote_task(conn, tid, actor="ops")
    assert ok is False
    assert all(s in err for s in ("'todo'", "'blocked'", "'triage'"))
