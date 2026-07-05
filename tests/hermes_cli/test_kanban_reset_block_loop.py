"""Sanctioned recovery for a card whose ``block_recurrences`` is already inflated.

Card t_33a7eb03 (follow-up to the block-loop breaker, PR #48). PR #48 stops a
*distinct-finding* review bounce from FALSE-tripping the loop counter going
forward, but provides no RECOVERY for a card whose ``block_recurrences`` is ALREADY
inflated (by prior buggy-code runs or an operator's repeated block->unblock while
diagnosing). Live symptom on card t_0d57d36d: counter stuck at 5, every sanctioned
``block``->``unblock`` re-tripped straight to ``triage``, and there was NO sanctioned
verb/API to reset ``block_recurrences`` — the only escape was the non-obvious
``move_card`` workaround.

These tests pin the two sanctioned recovery paths:

* An explicit ``reset_block_recurrences`` core API (surfaced as
  ``hermes kanban unblock --reset-loop <id>``) that zeroes the counter through the
  sanctioned API and emits an audit event, so operators never touch the DB.
* Automatic reset inside ``auto_route_review_bounce`` when a genuine author-rework
  transition (a review bounce whose finding MATERIALLY DIFFERS from the prior
  round) routes the card back to the author — a fresh cycle, not a continuation of
  the loop.

Guard (load-bearing): a genuine SAME-unfixed-finding review loop must STILL escalate
to triage — the breaker is preserved, the reset only fires on progress.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB (real DB, temp board, no mocks)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _blocked_with_recurrences(conn, *, recurrences: int, assignee: str = "eckert",
                              reason: str = "needs_input: something") -> str:
    """Create a card parked ``blocked`` with an inflated ``block_recurrences``.

    Models the corrupted state directly: a card whose counter is already high
    (from prior buggy runs / operator churn) sitting in the human ``blocked``
    lane. Emits a real ``blocked`` event carrying ``reason`` so the sticky-reason
    detectors see a genuine block.
    """
    tid = kb.create_task(conn, title="stuck card", assignee=assignee)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='blocked', block_recurrences=?, block_kind='needs_input' "
            "WHERE id=?",
            (recurrences, tid),
        )
        kb._append_event(conn, tid, "blocked",
                         {"reason": reason, "kind": "needs_input",
                          "recurrences": recurrences})
    return tid


def _recurrences(conn, tid: str) -> int:
    row = conn.execute(
        "SELECT block_recurrences FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    return int(row["block_recurrences"]) if row and row["block_recurrences"] is not None else 0


# ---------------------------------------------------------------------------
# RED 1 — the explicit reset_block_recurrences core API
# ---------------------------------------------------------------------------


def test_reset_block_recurrences_zeroes_counter_and_audits(kanban_home: Path) -> None:
    """The sanctioned API zeroes an inflated ``block_recurrences`` and emits an
    auditable ``block_recurrences_reset`` event carrying the actor + prior value."""
    with kb.connect() as conn:
        tid = _blocked_with_recurrences(conn, recurrences=5)
        assert _recurrences(conn, tid) == 5

        ok = kb.reset_block_recurrences(
            conn, tid, actor="casey", reason="operator recovery"
        )

        assert ok is True
        assert _recurrences(conn, tid) == 0, "counter must be zeroed"

        events = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='block_recurrences_reset'",
            (tid,),
        ).fetchall()
        assert len(events) == 1, "exactly one reset audit event required"
        import json
        payload = json.loads(events[0]["payload"])
        assert payload.get("previous") == 5
        assert payload.get("actor") == "casey"
        assert payload.get("reason") == "operator recovery"


def test_reset_block_recurrences_missing_task_is_false(kanban_home: Path) -> None:
    """Resetting a non-existent task is a clean no-op ``False`` (not an error)."""
    with kb.connect() as conn:
        assert kb.reset_block_recurrences(conn, "t_deadbeefcafe", actor="casey") is False


def test_reset_block_recurrences_already_zero_still_audits(kanban_home: Path) -> None:
    """Resetting an already-zero counter succeeds and still records the audit event
    (the operator's deliberate action is worth an audit trail even when it's a no-op
    on the value)."""
    with kb.connect() as conn:
        tid = _blocked_with_recurrences(conn, recurrences=0)
        ok = kb.reset_block_recurrences(conn, tid, actor="casey")
        assert ok is True
        assert _recurrences(conn, tid) == 0
        events = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='block_recurrences_reset'",
            (tid,),
        ).fetchall()
        assert len(events) == 1


# ---------------------------------------------------------------------------
# RED 2 — an inflated-counter card recovers to the normal review-bounce flow
# ---------------------------------------------------------------------------


def test_inflated_card_recovers_via_reset_then_reblock_stays_blocked(kanban_home: Path) -> None:
    """The core recovery: a card whose counter is at the limit, once reset, must
    re-block to ``blocked`` (the normal human lane) instead of re-tripping straight
    to ``triage`` on the very next block. Proves the sanctioned reset returns the
    card to the normal review-bounce->author->rework flow without the move_card
    workaround and without touching the DB."""
    with kb.connect() as conn:
        # A card at the limit: the next same-cause block would escalate to triage.
        tid = _blocked_with_recurrences(conn, recurrences=kb.BLOCK_RECURRENCE_LIMIT)

        # Sanctioned reset, then return to the work pool and re-block.
        assert kb.reset_block_recurrences(conn, tid, actor="casey")
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"

        kb.claim_task(conn, tid)
        assert kb.block_task(
            conn, tid, reason="needs_input: something", kind="needs_input",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        # Without the reset this would be 'triage'; with it, back to 'blocked'.
        assert kb.get_task(conn, tid).status == "blocked", \
            "reset card must re-block to the human lane, not escalate to triage"


# ---------------------------------------------------------------------------
# RED 3 — the breaker is preserved: a genuine same-finding loop still escalates
# ---------------------------------------------------------------------------


def test_same_finding_loop_still_escalates_to_triage(kanban_home: Path) -> None:
    """The loop breaker must still fire on a genuine same-unfixed-finding loop.
    A card re-blocked BLOCK_RECURRENCE_LIMIT times for the identical needs_input
    cause (no reset) escalates to triage exactly as before — the recovery path does
    not weaken the breaker."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="genuinely looping", assignee="eckert")
        landed = None
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT + 1):
            kb.claim_task(conn, tid)
            kb.block_task(
                conn, tid, reason="needs_input: same unfixed cause", kind="needs_input",
                expected_run_id=kb.get_task(conn, tid).current_run_id,
            )
            landed = kb.get_task(conn, tid).status
            if landed == "triage":
                break
            kb.unblock_task(conn, tid)
        assert landed == "triage", "same-finding loop must still escalate to triage"


# ---------------------------------------------------------------------------
# RED 4 — auto_route_review_bounce resets on a materially-different finding
# ---------------------------------------------------------------------------


def _stage_bounce_with_prior_different_finding(conn, *, author="eckert", reviewer="lamport",
                                               recurrences: int = 3) -> str:
    """A review-bounce card whose current sticky finding DIFFERS from the prior
    round's finding, with an inflated counter — the genuine author-rework case."""
    tid = kb.create_task(conn, title="feature work", assignee=author)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked', assignee=?, block_recurrences=? "
                     "WHERE id=?", (reviewer, recurrences, tid))
        # Prior round's finding (older blocked event).
        kb._append_event(conn, tid, "blocked",
                         {"reason": "review-changes-requested: finding ALPHA; "
                          "see https://github.com/cwest/hermes-agent/pull/71",
                          "kind": "needs_input", "recurrences": recurrences - 1})
        kb._append_event(conn, tid, "unblocked", None)
        # The move that recorded the author (build->review hop).
        kb._append_event(conn, tid, "assigned",
                         {"from": author, "to": reviewer, "by": "onecard:move_card"})
        # Current round's DIFFERENT finding (newest blocked event = sticky).
        kb._append_event(conn, tid, "blocked",
                         {"reason": "review-changes-requested: finding BETA distinct; "
                          "see https://github.com/cwest/hermes-agent/pull/71",
                          "kind": "needs_input", "recurrences": recurrences})
    return tid


def test_auto_route_resets_counter_on_different_finding(kanban_home: Path) -> None:
    """A genuine author-rework transition (review bounce whose finding materially
    DIFFERS from the prior round) routes to the author AND resets block_recurrences
    to 0 — a fresh cycle, not a continuation of the loop."""
    with kb.connect() as conn:
        tid = _stage_bounce_with_prior_different_finding(conn, recurrences=3)
        assert _recurrences(conn, tid) == 3

        routed = kb.auto_route_review_bounce(conn)

        assert routed == 1
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.assignee == "eckert"
        assert _recurrences(conn, tid) == 0, \
            "a materially-different-finding rework must reset the loop counter"
        # And the reset is audited.
        n = conn.execute(
            "SELECT count(*) c FROM task_events WHERE task_id=? AND kind='block_recurrences_reset'",
            (tid,),
        ).fetchone()["c"]
        assert n == 1


def test_auto_route_preserves_counter_on_same_finding(kanban_home: Path) -> None:
    """The guard: when the review bounce repeats the IDENTICAL finding, the
    auto-route must NOT reset the counter — a same-unfixed-finding loop must keep
    accumulating toward the breaker."""
    with kb.connect() as conn:
        author, reviewer = "eckert", "lamport"
        same = ("review-changes-requested: finding ALPHA unchanged; "
                "see https://github.com/cwest/hermes-agent/pull/71")
        tid = kb.create_task(conn, title="feature work", assignee=author)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked', assignee=?, block_recurrences=1 "
                         "WHERE id=?", (reviewer, tid))
            kb._append_event(conn, tid, "blocked",
                             {"reason": same, "kind": "needs_input", "recurrences": 1})
            kb._append_event(conn, tid, "unblocked", None)
            kb._append_event(conn, tid, "assigned",
                             {"from": author, "to": reviewer, "by": "onecard:move_card"})
            # SAME finding again (sticky).
            kb._append_event(conn, tid, "blocked",
                             {"reason": same, "kind": "needs_input", "recurrences": 1})

        routed = kb.auto_route_review_bounce(conn)

        assert routed == 1, "the card still routes back to the author"
        assert _recurrences(conn, tid) == 1, \
            "a same-finding bounce must NOT reset the loop counter (breaker preserved)"
        n = conn.execute(
            "SELECT count(*) c FROM task_events WHERE task_id=? AND kind='block_recurrences_reset'",
            (tid,),
        ).fetchone()["c"]
        assert n == 0, "no reset event on a same-finding bounce"


# ---------------------------------------------------------------------------
# RED 5 — the CLI `unblock --reset-loop <id>` verb
# ---------------------------------------------------------------------------


def _unblock_ns(task_ids, *, reason=None, reset_loop=False):
    import argparse
    return argparse.Namespace(
        task_ids=list(task_ids),
        reason=reason,
        reset_loop=reset_loop,
    )


def test_cli_unblock_reset_loop_recovers_blocked_card(kanban_home: Path, capsys) -> None:
    """``hermes kanban unblock --reset-loop <id>`` zeroes an inflated counter AND
    returns the card to ``ready`` — the sanctioned operator recovery, no DB edit."""
    from hermes_cli import kanban as kb_cli
    with kb.connect() as conn:
        tid = _blocked_with_recurrences(conn, recurrences=5)

    rc = kb_cli._cmd_unblock(_unblock_ns([tid], reset_loop=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "reset loop counter" in out.lower()

    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert _recurrences(conn, tid) == 0
        n = conn.execute(
            "SELECT count(*) c FROM task_events WHERE task_id=? AND kind='block_recurrences_reset'",
            (tid,),
        ).fetchone()["c"]
        assert n == 1, "the CLI reset must emit an audit event"


def test_cli_unblock_reset_loop_recovers_card_stuck_in_triage(
    kanban_home: Path, capsys
) -> None:
    """A card already escalated to ``triage`` (the exact live symptom) is recovered
    by ``--reset-loop``: the counter is zeroed even though ``unblock`` cannot flip a
    triage card, so a subsequent normal cycle no longer re-trips to triage."""
    from hermes_cli import kanban as kb_cli
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="stuck in triage", assignee="eckert")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='triage', block_recurrences=5 WHERE id=?", (tid,)
            )

    rc = kb_cli._cmd_unblock(_unblock_ns([tid], reset_loop=True))
    assert rc == 0

    with kb.connect() as conn:
        # unblock does not apply to triage, but the counter is cleared and audited.
        assert _recurrences(conn, tid) == 0
        n = conn.execute(
            "SELECT count(*) c FROM task_events WHERE task_id=? AND kind='block_recurrences_reset'",
            (tid,),
        ).fetchone()["c"]
        assert n == 1


def test_cli_unblock_without_reset_loop_leaves_counter(kanban_home: Path, capsys) -> None:
    """A plain ``unblock`` (no ``--reset-loop``) must NOT touch the counter — the
    existing deliberate design (the counter survives an unblock) is unchanged."""
    from hermes_cli import kanban as kb_cli
    with kb.connect() as conn:
        tid = _blocked_with_recurrences(conn, recurrences=2)

    rc = kb_cli._cmd_unblock(_unblock_ns([tid]))
    assert rc == 0

    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert _recurrences(conn, tid) == 2, "plain unblock must not reset the counter"
        n = conn.execute(
            "SELECT count(*) c FROM task_events WHERE task_id=? AND kind='block_recurrences_reset'",
            (tid,),
        ).fetchone()["c"]
        assert n == 0

