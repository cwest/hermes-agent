"""Regression tests: a PR-requiring card may complete on a VERIFIED NO-OP.

The missing-PR guard (:func:`_card_requires_pr` + the enforcement block in
:func:`complete_task`) correctly refuses ``-> done`` for a ``worktree`` card
that carries no ``pull/<n>`` artifact — an implementer that built in a worktree
and exited without pushing must NOT flip the card to ``done``.

But an AUDIT / curation card legitimately produces NO PR: its contract is "open
a PR ONLY if a change is warranted; a verified no-op close is a correct and
complete outcome." Such a card is still filed with a ``worktree`` workspace
(the auditor needs a real checkout to run conformance tooling in), so a CORRECT
outcome hit the guard with no lane to reach — ``-> done`` refused,
``-> review`` re-claimed — a live respawn loop (``t_fa343520``, four runs).

The fix is a first-class, auditable terminal for a declared verified no-op, NOT
a bypass flag and NOT the ``allow_acceptance_complete`` acceptance override
(which would fake a merge that never happened):

* The completing call DECLARES the no-op explicitly — ``verified_no_op=True``
  with a non-empty ``no_pr_reason`` threaded from ``kanban_complete`` through
  ``complete_task``.
* The guard accepts the declaration in place of the PR artifact and the card
  reaches its terminal lane (``done``) — a verified no-op is the accepted
  null-diff outcome; there is nothing for Casey to merge.
* The transition records a DISTINGUISHABLE ``completion_no_pr_verified`` event
  carrying the declared reason — as legible in the event stream as a merged PR,
  never a silent bypass and never the acceptance-bypass event.

These tests pin all four "Done when" criteria plus the load-bearing invariant
that the declaration does NOT weaken the guard for its designed case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _stage_running_worktree_card(
    conn, *, with_owner_map: bool = False, workspace_path: str = "/Users/x/src/repo"
) -> str:
    """A claimed (``running``) ``worktree`` card — a card that owes a PR.

    When ``with_owner_map`` is set, stamp a review-lane owner map so the card
    is review-ELIGIBLE: this proves the verified-no-op terminal bypasses the
    author-lane->review redirect (guard 4) and does NOT get shunted into the
    review lane where it would respawn-loop.
    """
    tid = kb.create_task(
        conn,
        title="audit the corpus for drift",
        assignee="easley",
        workspace_kind="worktree",
        workspace_path=workspace_path,
    )
    if with_owner_map:
        kb.add_comment(
            conn, tid, author="hollis",
            body=(
                "[audit] actor=hollis stage=submit ts=2026-08-03T00:00:00Z\n"
                "notes: state_owners={ready: easley, review: lamport, "
                "blocked-acceptance: casey} triager=hollis team=engineering"
            ),
        )
    kb.claim_task(conn, tid)
    assert kb.get_task(conn, tid).status == "running"
    return tid


# ---------------------------------------------------------------------------
# RED 1 — a declared verified no-op reaches its terminal lane
# ---------------------------------------------------------------------------


def test_verified_no_op_completes_worktree_card_without_pr(kanban_home: Path) -> None:
    """A ``worktree`` card with no PR that DECLARES a verified no-op reaches
    its terminal lane (``done``) instead of being refused."""
    with kb.connect() as conn:
        tid = _stage_running_worktree_card(conn)

        ok = kb.complete_task(
            conn, tid,
            summary="audited the corpus; already conformant, no change warranted",
            verified_no_op=True,
            no_pr_reason="corpus already conformant; no drift found across 239 nodes",
        )

        assert ok is True, "a declared verified no-op must reach its terminal lane"
        task = kb.get_task(conn, tid)
        assert task.status == "done", "a verified no-op terminates at done"
        assert task.completed_at is not None, "a completed card carries a timestamp"


def test_verified_no_op_not_shunted_to_review(kanban_home: Path) -> None:
    """A verified no-op on a review-ELIGIBLE worktree card (owner map naming a
    review lane) must NOT be moved into ``review`` — there is nothing to review
    (no PR, no diff), and the review lane is exactly where the observed respawn
    loop happened. It terminates at ``done``."""
    with kb.connect() as conn:
        tid = _stage_running_worktree_card(conn, with_owner_map=True)

        ok = kb.complete_task(
            conn, tid,
            summary="verified no-op",
            verified_no_op=True,
            no_pr_reason="nothing actionable found",
        )

        assert ok is True
        assert kb.get_task(conn, tid).status == "done", \
            "a verified no-op must terminate at done, never move to review"


# ---------------------------------------------------------------------------
# RED 2 — the guard is NOT weakened: no declaration => still refused
# ---------------------------------------------------------------------------


def test_worktree_card_without_pr_and_without_declaration_still_refused(
    kanban_home: Path,
) -> None:
    """The whole point of the guard: a ``worktree`` card with NO PR and NO
    declared no-op is STILL refused with ``completion_refused_missing_pr``.
    The declaration is the ONLY thing that unlocks the terminal — its absence
    leaves the designed case exactly as strict as before."""
    with kb.connect() as conn:
        tid = _stage_running_worktree_card(conn)

        ok = kb.complete_task(conn, tid, summary="wrote files, never opened a PR")

        assert ok is False, "no PR and no declared no-op must still be refused"
        task = kb.get_task(conn, tid)
        assert task.status == "running", "a refusal must not move the card"
        assert task.completed_at is None
        events = kb.list_events(conn, tid)
        assert [e for e in events if e.kind == "completion_refused_missing_pr"], \
            "the designed-case refusal event must still fire"
        assert not [e for e in events if e.kind == "completion_no_pr_verified"], \
            "no verified-no-op event on an undeclared refusal"


def test_verified_no_op_flag_without_reason_is_refused(kanban_home: Path) -> None:
    """A verified-no-op declaration MUST carry a reason — the no-op close has
    to be legible. ``verified_no_op=True`` with an empty/blank reason does not
    unlock the terminal; the card is refused as if undeclared."""
    with kb.connect() as conn:
        tid = _stage_running_worktree_card(conn)

        ok = kb.complete_task(
            conn, tid, summary="no-op but no reason given",
            verified_no_op=True, no_pr_reason="   ",
        )

        assert ok is False, "a no-op declaration with no reason must not unlock"
        assert kb.get_task(conn, tid).status == "running"
        events = kb.list_events(conn, tid)
        assert [e for e in events if e.kind == "completion_refused_missing_pr"]
        assert not [e for e in events if e.kind == "completion_no_pr_verified"]


# ---------------------------------------------------------------------------
# RED 3 — the no-op terminal emits its OWN distinguishable event
# ---------------------------------------------------------------------------


def test_verified_no_op_emits_distinguishable_event_with_reason(
    kanban_home: Path,
) -> None:
    """The verified-no-op close emits a ``completion_no_pr_verified`` event
    carrying the declared reason — NOT the acceptance bypass event and NOT the
    missing-PR refusal. A no-op is as legible in the stream as a merged PR."""
    reason = "corpus already conformant; no drift found across 239 nodes"
    with kb.connect() as conn:
        tid = _stage_running_worktree_card(conn)

        kb.complete_task(
            conn, tid, summary="verified no-op",
            verified_no_op=True, no_pr_reason=reason,
        )

        events = kb.list_events(conn, tid)
        verified = [e for e in events if e.kind == "completion_no_pr_verified"]
        assert verified, "a completion_no_pr_verified event must be recorded"
        assert verified[0].payload.get("no_pr_reason") == reason, \
            "the declared reason must be recorded on the event"
        assert verified[0].payload.get("workspace_kind") == "worktree"
        # The no-op is NOT an acceptance and NOT a refusal.
        assert not [e for e in events if e.kind == "completion_refused_missing_pr"], \
            "a verified no-op is not a missing-PR refusal"
        assert not [e for e in events if e.kind == "completion_refused_acceptance"], \
            "a verified no-op is not an acceptance refusal"
        # It DID complete: the normal completion event lands too.
        assert [e for e in events if e.kind == "completed"], \
            "a verified no-op is a real completion"


# ---------------------------------------------------------------------------
# RED 4 — the allow_acceptance_complete path is unchanged
# ---------------------------------------------------------------------------


def test_merge_override_unchanged_by_no_op_path(kanban_home: Path) -> None:
    """Casey's merge path (``allow_acceptance_complete=True``) still completes a
    worktree card with no PR and WITHOUT any no-op declaration — the override is
    untouched by the new terminal, and it emits the normal completion, never a
    ``completion_no_pr_verified`` event (that event belongs to the honest
    worker-declared no-op, not to an acceptance)."""
    with kb.connect() as conn:
        tid = _stage_running_worktree_card(conn)

        ok = kb.complete_task(
            conn, tid, summary="merged by Casey",
            allow_acceptance_complete=True,
        )

        assert ok is True, "the merge override must still complete the card"
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        events = kb.list_events(conn, tid)
        assert not [e for e in events if e.kind == "completion_no_pr_verified"], \
            "the acceptance override must not masquerade as a verified no-op"
        assert [e for e in events if e.kind == "completed"]
