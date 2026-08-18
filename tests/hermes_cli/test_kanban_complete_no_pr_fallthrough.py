"""Regression tests: a NO-PR / edit-in-place card that is review-eligible must
not self-complete straight to ``done`` past its reviewer.

``done`` on the board means exactly one thing — Casey merged/accepted the work.
The author-lane redirect (``complete_task``) already MOVEs a ``running|ready``
author completion to the ``review`` lane instead of ``done`` when a review owner
resolves, and already REFUSES a PR-owing card whose owner map is un-stamped
(``completion_redirect_unresolved``). But the refusal was scoped to cards with a
PR signal (``_card_requires_pr`` or an open PR artifact). A ``scratch`` /
``~/.hermes`` edit-in-place card that declares a review lane in its body's
``Routing (owner map): {…}`` prose — the ``cwest/hermes-config`` work class —
carried NO PR signal, so it fell through the PR-only guard and self-completed to
``done`` with no review round (the live ``t_baaa247f`` false-``done``).

The fix reconciles core's owner resolution with the staging path
(``onecard_common.resolve_reviewer``): the owner-map reader now also honors the
human-prose ``Routing (owner map): {ready: …, review: …, …}`` form (not only the
strict ``state_owners={…}`` submit-audit form), so a card whose review lane is
declared in its body resolves the SAME reviewer the staging path would — and its
author-lane completion MOVEs it to review rather than to ``done``.

Review-eligibility is an explicit, named predicate (``_card_is_review_eligible``):
a card is review-eligible when it declares an owner/routing map (strict or prose,
in the body or any comment) OR carries a PR signal. A card with NO map declaration
anywhere and NO PR (a genuine dispatcher/system bookkeeping card, a plain
``scratch`` task) is review-EXEMPT and completes to ``done`` exactly as before.

These tests pin the contract on the real ``complete_task`` path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# The human-prose owner-map form homestead/fork-core work stamps into a card
# BODY (submit_card writes the strict state_owners={…} audit-comment form; a
# card filed via bare create_task with an inline routing line uses this prose
# form). Both must resolve the same reviewer.
_PROSE_MAP_FULL = (
    "Routing (owner map): {ready: eckert, review: lamport, "
    "blocked-acceptance: casey}"
)
_PROSE_MAP_NO_REVIEW = "Routing (owner map): {ready: eckert}"


# ---------------------------------------------------------------------------
# Reconcile the resolver split: the owner-map reader must ALSO read the prose
# ``Routing (owner map):`` form, so it agrees with the staging path's resolver.
# ---------------------------------------------------------------------------


def test_review_owner_reads_prose_routing_map_in_body(kanban_home: Path) -> None:
    """``_review_owner_from_owner_map`` resolves the review owner from a card
    whose review lane is declared only in its BODY's ``Routing (owner map):``
    prose — the exact shape ``t_baaa247f`` carried while the strict-form reader
    returned ``None`` and ``resolve_reviewer`` returned ``lamport`` (the split
    this closes)."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="edit in place",
            assignee="eckert",
            body="# Why\nsomething\n\n# Scope\n" + _PROSE_MAP_FULL,
            workspace_kind="scratch", detached=True,
        )
        assert kb._review_owner_from_owner_map(conn, tid) == "lamport"


def test_review_owner_reads_prose_routing_map_in_comment(kanban_home: Path) -> None:
    """The prose ``Routing (owner map):`` form is honored in a COMMENT too (the
    fork-core inline-fix filing records it in a ``hollis`` audit comment)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="edit in place", assignee="eckert", detached=True)
        kb.add_comment(
            conn, tid, author="hollis",
            body=f"[audit] actor=hollis stage=intake\n{_PROSE_MAP_FULL} · triager: hollis",
        )
        assert kb._review_owner_from_owner_map(conn, tid) == "lamport"


def test_strict_state_owners_form_still_wins(kanban_home: Path) -> None:
    """The strict ``state_owners={…}`` submit-audit form is still resolved (no
    regression from adding the prose arm)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="gated card", assignee="eckert", detached=True)
        kb.add_comment(
            conn, tid, author="hollis",
            body=(
                "[audit] actor=hollis stage=submit ts=2026-07-26T00:00:00Z\n"
                "notes: state_owners={ready: eckert, review: lamport, "
                "blocked-acceptance: casey} triager=hollis team=engineering"
            ),
        )
        assert kb._review_owner_from_owner_map(conn, tid) == "lamport"


# ---------------------------------------------------------------------------
# The core fix: a no-PR card that DECLARES a review lane MOVEs to review, not done
# ---------------------------------------------------------------------------


def test_no_pr_card_with_prose_review_owner_moves_to_review(kanban_home: Path) -> None:
    """The ``t_baaa247f`` shape: a ``scratch`` / edit-in-place card, no PR, whose
    body declares ``review: lamport``. An author-lane completion must MOVE it to
    the ``review`` lane + lamport — NOT self-complete to ``done``."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="stamp SOUL block in place",
            assignee="eckert",
            body="# Why\nfoo\n\n# Scope\n" + _PROSE_MAP_FULL,
            workspace_kind="scratch", detached=True,
        )
        kb.claim_task(conn, tid)
        assert kb.get_task(conn, tid).status == "running"

        ok = kb.complete_task(conn, tid, summary="edited in place, no PR")

        assert ok is True, "a review-eligible no-PR card redirects (a move)"
        task = kb.get_task(conn, tid)
        assert task.status == "review", "no-PR completion MOVES to review"
        assert task.assignee == "lamport", "assignee is the card's review owner"
        assert task.completed_at is None, "a move to review is not a completion"


def test_no_pr_card_with_prose_review_owner_does_not_reach_done(kanban_home: Path) -> None:
    """No ``status_changed -> done`` / ``completed`` event lands for the move."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="edit in place", assignee="eckert",
            body="# Scope\n" + _PROSE_MAP_FULL, workspace_kind="scratch", detached=True,
        )
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="edited in place")

        events = kb.list_events(conn, tid)
        assert not [
            e for e in events
            if e.kind == "status_changed" and (e.payload or {}).get("to") == "done"
        ], "a review-eligible no-PR card must not land in done"
        moved = [
            e for e in events
            if e.kind == "status_changed" and (e.payload or {}).get("to") == "review"
        ]
        assert moved, "the card must MOVE to the review lane"


# ---------------------------------------------------------------------------
# The refusal arm: review-eligible but the review owner is UNRESOLVABLE -> REFUSE
# ---------------------------------------------------------------------------


def test_no_pr_eligible_card_without_review_owner_is_refused(kanban_home: Path) -> None:
    """A no-PR card that declares an owner/routing map (so it is review-eligible)
    but whose map names NO review lane cannot resolve a reviewer. It must REFUSE
    (clean no-op) rather than silently fall through to ``done``."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="edit in place, malformed routing", assignee="eckert",
            body="# Scope\n" + _PROSE_MAP_NO_REVIEW, workspace_kind="scratch", detached=True,
        )
        kb.claim_task(conn, tid)

        ok = kb.complete_task(conn, tid, summary="no review lane declared")

        assert ok is False, "a review-eligible card with no resolvable reviewer refuses"
        task = kb.get_task(conn, tid)
        assert task.status == "running", "card must not move to done"
        assert task.completed_at is None, "no completion timestamp on a refusal"


def test_no_pr_eligible_refusal_emits_audit_event(kanban_home: Path) -> None:
    """The refused no-PR completion is auditable via ``completion_redirect_unresolved``
    and lands no ``completed`` / ``-> done`` event."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="edit in place, malformed routing", assignee="eckert",
            body="# Scope\n" + _PROSE_MAP_NO_REVIEW, workspace_kind="scratch", detached=True,
        )
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="no review lane declared")

        events = kb.list_events(conn, tid)
        refusals = [e for e in events if e.kind == "completion_redirect_unresolved"]
        assert refusals, "a completion_redirect_unresolved event must be recorded"
        assert not [e for e in events if e.kind == "completed"]
        assert not [
            e for e in events
            if e.kind == "status_changed" and (e.payload or {}).get("to") == "done"
        ]


def test_merge_override_bypasses_no_pr_refusal(kanban_home: Path) -> None:
    """Casey's merge override completes a review-eligible no-PR card to ``done``
    even with no resolvable review owner — the broadened refusal is bypassed too."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="edit in place", assignee="eckert",
            body="# Scope\n" + _PROSE_MAP_NO_REVIEW, workspace_kind="scratch", detached=True,
        )
        kb.claim_task(conn, tid)

        ok = kb.complete_task(
            conn, tid, summary="accepted by Casey", allow_acceptance_complete=True,
        )
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# The exempt set stays exempt: a genuine bookkeeping / scratch card with NO map
# declaration anywhere and NO PR completes to ``done`` exactly as before.
# ---------------------------------------------------------------------------


def test_plain_scratch_card_no_map_still_completes_to_done(kanban_home: Path) -> None:
    """A plain ``scratch`` task with no owner/routing map anywhere and no PR is
    genuinely review-EXEMPT — the refusal/redirect must NOT fire; it completes to
    ``done`` (the dispatcher/system-bookkeeping and legacy-test population)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="bookkeeping tick", assignee="salton", detached=True)
        kb.claim_task(conn, tid)

        assert kb.complete_task(conn, tid, summary="done, no review needed") is True
        assert kb.get_task(conn, tid).status == "done"


def test_body_without_routing_map_is_not_eligible(kanban_home: Path) -> None:
    """A card body with prose that is NOT an owner/routing map does not make the
    card review-eligible (no false positive on incidental ``{…}`` text)."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="scratch", assignee="salton",
            body="# Why\nfix the thing. config = {a: 1, b: 2}\n",
            workspace_kind="scratch", detached=True,
        )
        kb.claim_task(conn, tid)

        assert kb.complete_task(conn, tid, summary="fixed") is True
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# The named predicate itself
# ---------------------------------------------------------------------------


def test_card_is_review_eligible_predicate(kanban_home: Path) -> None:
    """``_card_is_review_eligible`` is True for a card declaring a routing map or
    carrying a PR signal, and False for a plain bookkeeping card."""
    with kb.connect() as conn:
        prose = kb.create_task(
            conn, title="prose map", assignee="eckert",
            body="# Scope\n" + _PROSE_MAP_FULL, workspace_kind="scratch", detached=True,
        )
        assert kb._card_is_review_eligible(conn, prose) is True

        worktree = kb.create_task(
            conn, title="worktree card", assignee="eckert",
            workspace_kind="worktree", workspace_path="/Users/x/src/repo", detached=True,
        )
        assert kb._card_is_review_eligible(conn, worktree) is True

        plain = kb.create_task(conn, title="bookkeeping", assignee="salton", detached=True)
        assert kb._card_is_review_eligible(conn, plain) is False
