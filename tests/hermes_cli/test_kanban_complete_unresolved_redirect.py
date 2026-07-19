"""Regression tests: an author-lane completion on a REVIEW-ELIGIBLE card that
cannot resolve a review owner must NOT silently fall through to ``done``.

``done`` on the board means exactly one thing — Casey merged/accepted the work.
The author-lane redirect (``complete_task``) moves a ``running|ready`` author
completion to the ``review`` lane INSTEAD of ``done`` — but only when the card's
stamped owner map yields a ``review`` owner. When a review-eligible card is filed
WITHOUT a ``stage=submit`` owner map, the redirect's precondition fails and the
completion previously fell through to the ``-> done`` UPDATE with no signal — the
exact false-``done`` (a code card with an open PR skipping the reviewer entirely).

The fix closes that hole for the population core can identify WITHOUT a card
``kind`` column: a PR-requiring card (an isolated git worktree cut for a
branch/PR — the same ``_card_requires_pr`` signal the missing-PR guard uses).
Such a card is unambiguously a code-review pipeline card. When its author-lane
completion cannot resolve a review owner, ``complete_task`` REFUSES (returns
False, no state mutation) and emits an auditable ``completion_redirect_unresolved``
event — the same clean no-op shape as the acceptance and missing-PR guards —
rather than silently landing ``done``.

Non-pipeline cards are untouched: a plain ``scratch`` task, a ``dir`` build, or a
``~/.hermes`` edit-in-place card with no review owner still completes to ``done``
exactly as before (the redirect never had a review lane to shunt them into, and
they are not PR-requiring, so the refusal never fires).

These tests pin the contract on the real ``complete_task`` path.
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


_PR_URL = "https://github.com/cwest/hermes-agent/pull/71"


def _card_with_pr(
    conn, *, stamped: bool, workspace_kind: str = "worktree",
    workspace_path: str = "/Users/x/src/repo",
) -> str:
    """A claimed (``running``) card that HAS a PR artifact.

    A worktree outside ``~/.hermes`` is PR-requiring (``_card_requires_pr``), and
    the PR comment satisfies the missing-PR guard — so completion reaches the
    author-lane redirect. ``stamped=False`` withholds the owner map, reproducing
    the false-``done`` hole; ``stamped=True`` proves the normal redirect still
    fires when the map IS present.

    ``workspace_kind`` defaults to ``worktree`` but can be ``dir`` to exercise a
    card that owns a PR yet is NOT ``_card_requires_pr`` — the population the
    engine-side guard must catch via the PR artifact alone.
    """
    tid = kb.create_task(
        conn,
        title="feature work",
        assignee="eckert",
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
    )
    if stamped:
        kb.add_comment(
            conn, tid, author="hollis",
            body=(
                "[audit] actor=hollis stage=submit ts=2026-07-14T18:00:00Z\n"
                "notes: state_owners={ready: eckert, review: lamport, "
                "blocked-acceptance: casey} triager=hollis team=engineering"
            ),
        )
    kb.add_comment(
        conn, tid, author="eckert",
        body=f"Draft PR opened: {_PR_URL} @ head abc1234. 240 tests green.",
    )
    kb.claim_task(conn, tid)
    assert kb.get_task(conn, tid).status == "running"
    return tid


def _worktree_card_with_pr(conn, *, stamped: bool) -> str:
    """Back-compat alias: a worktree-backed card that HAS a PR artifact."""
    return _card_with_pr(conn, stamped=stamped, workspace_kind="worktree")


# ---------------------------------------------------------------------------
# RED 1 — a review-eligible card with a PR but NO owner map must NOT reach done
# ---------------------------------------------------------------------------


def test_unstamped_pr_requiring_card_is_refused_not_done(kanban_home: Path) -> None:
    """The core false-``done`` bug: a worktree card WITH an open PR but no
    ``stage=submit`` owner map has no resolvable review owner, so the redirect
    cannot fire. It must REFUSE (clean no-op), NOT silently land ``done`` and
    skip the reviewer."""
    with kb.connect() as conn:
        tid = _worktree_card_with_pr(conn, stamped=False)

        ok = kb.complete_task(conn, tid, summary="impl done, forgot to stamp map")

        assert ok is False, "an unresolvable review-eligible card must not complete"
        task = kb.get_task(conn, tid)
        assert task.status == "running", "card must not move to done"
        assert task.completed_at is None, "no completion timestamp on a refusal"


def test_unstamped_pr_requiring_refusal_emits_audit_event(kanban_home: Path) -> None:
    """The rejected attempt is auditable and no ``completed`` event lands."""
    with kb.connect() as conn:
        tid = _worktree_card_with_pr(conn, stamped=False)
        kb.complete_task(conn, tid, summary="impl done, forgot to stamp map")

        events = kb.list_events(conn, tid)
        refusals = [
            e for e in events if e.kind == "completion_redirect_unresolved"
        ]
        assert refusals, "a completion_redirect_unresolved event must be recorded"
        assert refusals[0].payload.get("summary_preview") == (
            "impl done, forgot to stamp map"
        )
        assert not [e for e in events if e.kind == "completed"], \
            "a refused completion must not emit a 'completed' event"
        assert not [
            e for e in events
            if e.kind == "status_changed" and (e.payload or {}).get("to") == "done"
        ], "a refused completion must not land a status_changed -> done"


# ---------------------------------------------------------------------------
# RED 1b — a card that OWNS a PR but is NOT _card_requires_pr (a ``dir:``
#          workspace, e.g. a caseywest.com writing card) must ALSO be refused
# ---------------------------------------------------------------------------


def test_dir_card_with_pr_but_no_owner_map_is_refused_not_done(
    kanban_home: Path,
) -> None:
    """The production false-``done`` (card t_3cef33a1, PR #131): a ``dir:``
    workspace card that owns an OPEN PR is NOT ``_card_requires_pr`` (only
    ``worktree`` is), so the worktree-only refusal branch never fired and the
    completion fell through to ``done`` — skipping the reviewer and Casey.

    A card that carries a resolvable PR artifact is unambiguously review-eligible
    regardless of ``workspace_kind``. With no resolvable review owner it must
    REFUSE (clean no-op), NOT land ``done``.
    """
    with kb.connect() as conn:
        tid = _card_with_pr(conn, stamped=False, workspace_kind="dir")

        ok = kb.complete_task(conn, tid, summary="wrote post, PR open, map unstamped")

        assert ok is False, "a dir card owning a PR must not fall through to done"
        task = kb.get_task(conn, tid)
        assert task.status == "running", "card must not move to done"
        assert task.completed_at is None, "no completion timestamp on a refusal"


def test_dir_card_with_pr_refusal_emits_audit_event(kanban_home: Path) -> None:
    """The dir+PR refusal is auditable with the same event shape as the
    worktree refusal, and no ``completed``/``-> done`` event lands."""
    with kb.connect() as conn:
        tid = _card_with_pr(conn, stamped=False, workspace_kind="dir")
        kb.complete_task(conn, tid, summary="wrote post, PR open, map unstamped")

        events = kb.list_events(conn, tid)
        refusals = [
            e for e in events if e.kind == "completion_redirect_unresolved"
        ]
        assert refusals, "a completion_redirect_unresolved event must be recorded"
        assert refusals[0].payload.get("summary_preview") == (
            "wrote post, PR open, map unstamped"
        )
        assert not [e for e in events if e.kind == "completed"], \
            "a refused completion must not emit a 'completed' event"
        assert not [
            e for e in events
            if e.kind == "status_changed" and (e.payload or {}).get("to") == "done"
        ], "a refused completion must not land a status_changed -> done"


def test_dir_card_with_pr_and_owner_map_still_redirects_to_review(
    kanban_home: Path,
) -> None:
    """A ``dir:`` card owning a PR WITH a stamped owner map redirects to review
    exactly as a worktree card does — the artifact-based refusal only guards the
    UNRESOLVABLE case, it never intercepts a normal stamped redirect."""
    with kb.connect() as conn:
        tid = _card_with_pr(conn, stamped=True, workspace_kind="dir")

        ok = kb.complete_task(conn, tid, summary="post done, map stamped")

        assert ok is True, "a stamped review-eligible card redirects (a move)"
        task = kb.get_task(conn, tid)
        assert task.status == "review", "stamped completion MOVES to review"
        assert task.assignee == "lamport", "assignee is the card's review owner"


def test_dir_card_with_pr_merge_override_still_done(kanban_home: Path) -> None:
    """Casey's merge (``allow_acceptance_complete=True``) completes a dir+PR
    card to ``done`` even with no resolvable review owner — the override that
    bypasses the other guards bypasses the artifact-based refusal too."""
    with kb.connect() as conn:
        tid = _card_with_pr(conn, stamped=False, workspace_kind="dir")

        ok = kb.complete_task(
            conn, tid, summary="merged by Casey", allow_acceptance_complete=True,
        )

        assert ok is True, "the merge override must complete the card"
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# RED 2 — a stamped review-eligible card still redirects normally (no regression)
# ---------------------------------------------------------------------------


def test_stamped_pr_requiring_card_still_redirects_to_review(kanban_home: Path) -> None:
    """When the owner map IS present, the redirect fires exactly as before —
    the refusal only guards the UNRESOLVABLE case, it does not intercept a
    normal stamped redirect."""
    with kb.connect() as conn:
        tid = _worktree_card_with_pr(conn, stamped=True)

        ok = kb.complete_task(conn, tid, summary="impl done, map stamped")

        assert ok is True, "a stamped review-eligible card redirects (a move)"
        task = kb.get_task(conn, tid)
        assert task.status == "review", "stamped completion MOVES to review"
        assert task.assignee == "lamport", "assignee is the card's review owner"


# ---------------------------------------------------------------------------
# RED 3 — Casey's merge override bypasses the new refusal (regression)
# ---------------------------------------------------------------------------


def test_merge_override_bypasses_unresolved_refusal(kanban_home: Path) -> None:
    """``allow_acceptance_complete=True`` (Casey's merge) completes the card to
    ``done`` even with no resolvable review owner — the override that bypasses
    the acceptance and missing-PR guards bypasses this one too."""
    with kb.connect() as conn:
        tid = _worktree_card_with_pr(conn, stamped=False)

        ok = kb.complete_task(
            conn, tid, summary="merged by Casey", allow_acceptance_complete=True,
        )

        assert ok is True, "the merge override must complete the card"
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# RED 4 — non-pipeline cards with no review owner still complete to done
# ---------------------------------------------------------------------------


def test_scratch_card_without_review_owner_still_done(kanban_home: Path) -> None:
    """A plain ``scratch`` task is NOT PR-requiring and has no review lane — the
    refusal must NOT fire; it completes to ``done`` exactly as before."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="scratch task", assignee="salton")
        kb.claim_task(conn, tid)
        assert kb.get_task(conn, tid).workspace_kind == "scratch"

        assert kb.complete_task(conn, tid, summary="done, no review needed") is True
        assert kb.get_task(conn, tid).status == "done"


def test_dir_card_without_review_owner_still_done(kanban_home: Path) -> None:
    """A ``dir`` (shared build dir / edit-in-place) card is NOT PR-requiring —
    an unstamped completion still reaches ``done``."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="dir build", assignee="salton",
            workspace_kind="dir", workspace_path="/Users/x/src/repo",
        )
        kb.claim_task(conn, tid)

        assert kb.complete_task(conn, tid, summary="dir build done") is True
        assert kb.get_task(conn, tid).status == "done"


def test_hermes_home_worktree_without_review_owner_still_done(kanban_home: Path) -> None:
    """A worktree anchored under ``~/.hermes`` (config / live-install edits) is
    NOT PR-requiring, so an unstamped completion still reaches ``done``."""
    hermes_dir = str(Path.home() / ".hermes" / "hermes-agent")
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="edit config in place", assignee="salton",
            workspace_kind="worktree", workspace_path=hermes_dir,
        )
        kb.claim_task(conn, tid)

        assert kb.complete_task(conn, tid, summary="edited config") is True
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# RED 5 — _review_owner_from_owner_map is defined exactly once
# ---------------------------------------------------------------------------


def test_review_owner_from_owner_map_defined_once() -> None:
    """The owner-map reader must exist exactly once — a duplicate definition is
    a latent divergence bug (two readers that can drift apart)."""
    import ast
    import inspect

    source = inspect.getsource(kb)
    tree = ast.parse(source)
    defs = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_review_owner_from_owner_map"
    ]
    assert len(defs) == 1, (
        f"_review_owner_from_owner_map must be defined once, found {len(defs)}"
    )
