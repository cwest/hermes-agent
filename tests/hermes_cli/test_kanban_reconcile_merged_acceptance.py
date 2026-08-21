"""Regression tests: a merged-PR acceptance card has a verified reconcile to done.

``done`` on the board means exactly one thing — Casey merged/accepted the work.
The acceptance guard (``complete_task`` refusing an ``awaiting-casey-signoff``
card) correctly enforces that a worker cannot self-complete a card parked for
Casey's sign-off. But that guard cannot distinguish two cases:

* "Casey has not merged yet" — refuse (correct); and
* "Casey ALREADY merged, the ``github-pr-closed`` webhook was simply lost" — the
  card should reconcile to ``done``.

Observed live on ``t_fecc790d``: PR #82 was merged by Casey (squash commit
``d13344ac``), but the webhook never fired, so the card stayed ``blocked`` +
owner ``casey`` + reason ``awaiting-casey-signoff`` with no working path to
``done`` (``complete`` refuses; ``unblock`` + ``complete`` shunts to ``review``
against a merged PR).

:func:`reconcile_merged_acceptance` is the missing path. It PROVES the merge
against GitHub ground truth — the linked PR must be ``state == MERGED`` with a
non-null ``mergeCommit.oid`` — before moving the card, gated on ground truth
rather than caller assertion (a "trust me, it merged" flag would re-open exactly
the hole the acceptance guard exists to close). It is a reconciliation of a
MISSED event, not a new way to bypass sign-off: ``done`` still means "Casey
merged."

These tests pin the contract on the real path:

* An acceptance-lane card whose linked PR is verifiably MERGED reconciles to
  ``done``, and the audit trail records the merge commit + the missed-webhook
  reconcile (the ``t_fecc790d`` shape).
* The same call on a card whose PR is still OPEN is REFUSED — the guard was not
  weakened.
* The same call with no linked/resolvable PR is REFUSED.
* The same call on a card that is not in the acceptance lane is REFUSED (only an
  ``awaiting-casey-signoff`` park is reconcilable — this is not a generic
  self-complete).
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


_PR_URL = "https://github.com/cwest/okfctl/pull/82"
_MERGE_OID = "d13344ac9f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c"
_SIGNOFF_REASON = (
    f"awaiting-casey-signoff: reviewed PASS — {_PR_URL}; threads resolved; "
    "240 tests green. Ready to merge."
)


def _stage_acceptance_card(
    conn, *, reason: str = _SIGNOFF_REASON, pr_url: str | None = _PR_URL
) -> str:
    """Leave a card in the acceptance lane exactly as a reviewer PASS parks it:
    ``blocked`` + owner ``casey`` + sticky ``awaiting-casey-signoff`` reason,
    with the PR URL linked in a comment (the implementer's ready-for-review
    handoff)."""
    tid = kb.create_task(conn, title="feature work", assignee="casey", detached=True)
    kb.claim_task(conn, tid)
    if pr_url:
        kb.add_comment(
            conn, tid, author="easley",
            body=f"Draft PR opened: {pr_url} @ head abc1234. 240 tests green.",
        )
    assert kb.block_task(
        conn, tid, reason=reason,
        expected_run_id=kb.get_task(conn, tid).current_run_id,
    )
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert kb._latest_sticky_block_reason(conn, tid) == reason
    return tid


# ---------------------------------------------------------------------------
# RED 1 — the t_fecc790d shape: a MERGED-PR acceptance card reconciles to done
# ---------------------------------------------------------------------------


def test_reconcile_merged_acceptance_completes_to_done(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``awaiting-casey-signoff`` card whose linked PR is verifiably MERGED
    is reconciled to ``done`` — the missed-webhook recovery path."""
    monkeypatch.setattr(
        kb, "_resolve_pr_merge_commit", lambda url: ("merged", _MERGE_OID)
    )
    with kb.connect() as conn:
        tid = _stage_acceptance_card(conn)

        ok = kb.reconcile_merged_acceptance(conn, tid)

        assert ok is True, "a verifiably-merged acceptance card must reconcile"
        task = kb.get_task(conn, tid)
        assert task.status == "done", "reconcile must land the card in done"
        assert task.completed_at is not None


def test_reconcile_records_merge_commit_and_missed_webhook(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit trail records the merge commit + that this was a missed-webhook
    reconcile — so a done-by-reconcile is as traceable as a done-by-webhook."""
    monkeypatch.setattr(
        kb, "_resolve_pr_merge_commit", lambda url: ("merged", _MERGE_OID)
    )
    with kb.connect() as conn:
        tid = _stage_acceptance_card(conn)
        kb.reconcile_merged_acceptance(conn, tid)

        events = kb.list_events(conn, tid)
        recon = [e for e in events if e.kind == "completion_reconciled_merge"]
        assert recon, "a completion_reconciled_merge event must be recorded"
        payload = recon[-1].payload or {}
        assert payload.get("merge_commit") == _MERGE_OID, \
            "the reconcile must record the proven merge commit"
        assert payload.get("pr_url") == _PR_URL, "the reconcile must record the PR"
        # The card DID reach done (a real completion, via the merge override).
        assert [e for e in events if e.kind == "completed"], \
            "a reconciled merge must land a real 'completed' event"


# ---------------------------------------------------------------------------
# RED 2 — an OPEN PR is REFUSED (proves the guard was not weakened)
# ---------------------------------------------------------------------------


def test_reconcile_refuses_open_pr(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconcile is gated on GitHub ground truth: a card whose linked PR is
    still OPEN is REFUSED — a clean no-op, card stays in the acceptance lane."""
    monkeypatch.setattr(
        kb, "_resolve_pr_merge_commit", lambda url: ("open", None)
    )
    with kb.connect() as conn:
        tid = _stage_acceptance_card(conn)

        ok = kb.reconcile_merged_acceptance(conn, tid)

        assert ok is False, "an OPEN PR must not be reconcilable to done"
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "card must stay in the acceptance lane"
        assert task.assignee == "casey", "acceptance owner is unchanged"
        assert task.completed_at is None
        events = kb.list_events(conn, tid)
        assert not [e for e in events if e.kind == "completed"], \
            "an OPEN-PR refusal must not emit a 'completed' event"


def test_reconcile_refuses_merged_state_without_oid(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ground truth requires BOTH ``state == merged`` AND a non-null
    ``mergeCommit.oid``. A merged state with a null oid (unverifiable) is
    REFUSED — fail closed on the acceptance side."""
    monkeypatch.setattr(
        kb, "_resolve_pr_merge_commit", lambda url: ("merged", None)
    )
    with kb.connect() as conn:
        tid = _stage_acceptance_card(conn)

        ok = kb.reconcile_merged_acceptance(conn, tid)

        assert ok is False, "merged state with no merge oid must be refused"
        assert kb.get_task(conn, tid).status == "blocked"


def test_reconcile_refuses_unresolvable_pr_state(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient/unresolvable gh result (``unknown``) fails CLOSED — the
    reconcile refuses rather than force-accepting on an unverifiable answer."""
    monkeypatch.setattr(
        kb, "_resolve_pr_merge_commit", lambda url: ("unknown", None)
    )
    with kb.connect() as conn:
        tid = _stage_acceptance_card(conn)

        assert kb.reconcile_merged_acceptance(conn, tid) is False
        assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# RED 3 — no linked/resolvable PR is REFUSED
# ---------------------------------------------------------------------------


def test_reconcile_refuses_card_with_no_pr(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card with no resolvable PR URL cannot be reconciled — there is no
    artifact to prove a merge against. The gh hook must never even be called."""
    def _boom(url):  # pragma: no cover - must not be reached
        raise AssertionError("gh must not be consulted when no PR is linked")

    monkeypatch.setattr(kb, "_resolve_pr_merge_commit", _boom)
    with kb.connect() as conn:
        tid = _stage_acceptance_card(conn, pr_url=None)

        ok = kb.reconcile_merged_acceptance(conn, tid)

        assert ok is False, "no linked PR must not be reconcilable"
        assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# RED 4 — only the acceptance park is reconcilable (not a generic self-complete)
# ---------------------------------------------------------------------------


def test_reconcile_refuses_non_acceptance_card(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconcile is scoped to the ``awaiting-casey-signoff`` acceptance park.
    A ``blocked`` card with a generic reason (or any non-acceptance lane) is
    REFUSED even if its PR is merged — this is not a generic bypass to done."""
    monkeypatch.setattr(
        kb, "_resolve_pr_merge_commit", lambda url: ("merged", _MERGE_OID)
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="generic block", assignee="eckert", detached=True)
        kb.claim_task(conn, tid)
        kb.add_comment(
            conn, tid, author="easley",
            body=f"Draft PR opened: {_PR_URL} @ head abc1234.",
        )
        assert kb.block_task(
            conn, tid,
            reason="needs_input: which ACL default do we want?",
            kind="needs_input",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )

        ok = kb.reconcile_merged_acceptance(conn, tid)

        assert ok is False, "a non-acceptance block is not a reconcile target"
        assert kb.get_task(conn, tid).status == "blocked"


def test_reconcile_refuses_running_card(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card that is not ``blocked`` at all (e.g. still ``running``) is not an
    acceptance park and cannot be reconciled to done by this path."""
    monkeypatch.setattr(
        kb, "_resolve_pr_merge_commit", lambda url: ("merged", _MERGE_OID)
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="in flight", assignee="easley", detached=True)
        kb.claim_task(conn, tid)
        kb.add_comment(
            conn, tid, author="easley", body=f"Draft PR opened: {_PR_URL}",
        )
        assert kb.get_task(conn, tid).status == "running"

        ok = kb.reconcile_merged_acceptance(conn, tid)

        assert ok is False, "a running card is not an acceptance park"
        assert kb.get_task(conn, tid).status == "running"
