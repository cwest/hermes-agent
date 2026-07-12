"""Regression tests: ``complete_task`` must refuse a PR-requiring card that
carries no PR artifact.

``done`` on the board means exactly one thing — the declared reviewable
artifact exists (Casey merged/accepted the work). That invariant had a hole:
a worker building in a git worktree could write its deliverable, exit WITHOUT
committing / pushing / opening a PR, and still call ``kanban_complete`` — which
flipped the card to ``done`` anyway. It happened live (2026-07-08): a research
card wrote three files into its worktree, never opened a PR, and completed; its
gated children auto-promoted onto a foundation that did not exist.

``complete_task`` already guards two false-``done`` classes before its write
txn — phantom ``created_cards`` (raises ``HallucinatedCardsError`` +
``completion_blocked_hallucination``) and the acceptance-lane park (returns
``False`` + ``completion_refused_acceptance``). These tests pin the THIRD guard,
in the same shape:

* **Opt-in per card, not global.** A card requires a PR only when it builds in a
  real git repo worktree — ``workspace_kind in ('worktree','dir')`` with a
  ``workspace_path`` outside ``~/.hermes``. Edit-in-place / no-PR cards
  (``scratch`` kind, or a ``~/.hermes`` workdir) are NOT guarded and complete
  exactly as before.
* **The artifact check** reuses the existing PR->card linkage: a resolvable
  GitHub ``pull/<n>`` URL in a task comment (the ready-for-review handoff the
  implementer lane posts on PR open), matched by the same
  ``_RESPAWN_GUARD_PR_URL_RE`` the ``active_pr`` respawn guard trusts.
* **The merge path** (``allow_acceptance_complete=True``, Casey's
  ``github-pr-closed`` webhook) BYPASSES this guard, exactly as it bypasses the
  acceptance guard.
* A refused completion is a clean no-op: returns ``False``, never mutates task
  state, and emits an auditable ``completion_refused_missing_pr`` event.
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


def _stage_running_worktree_card(
    conn, *, workspace_kind: str = "worktree", workspace_path: str = "/Users/x/src/repo"
) -> str:
    """A claimed (``running``) card whose workspace is a real git worktree —
    i.e. a card that owes a reviewable PR, exactly as an implementer card is."""
    tid = kb.create_task(
        conn,
        title="feature work",
        assignee="eckert",
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
    )
    kb.claim_task(conn, tid)
    assert kb.get_task(conn, tid).status == "running"
    return tid


# ---------------------------------------------------------------------------
# RED 1 — a PR-requiring card with no PR artifact is REFUSED (the bug class)
# ---------------------------------------------------------------------------


def test_complete_refuses_worktree_card_without_pr(kanban_home: Path) -> None:
    """A worktree-backed card with no ``pull/<n>`` reference is a clean no-op
    refusal: returns False, card stays ``running``, no ``done``."""
    with kb.connect() as conn:
        tid = _stage_running_worktree_card(conn)

        ok = kb.complete_task(conn, tid, summary="wrote files, never opened a PR")

        assert ok is False, "a PR-requiring card with no PR must not complete"
        task = kb.get_task(conn, tid)
        assert task.status == "running", "card must not move to done"
        assert task.completed_at is None, "no completion timestamp on a refusal"


def test_complete_refusal_emits_missing_pr_audit_event(kanban_home: Path) -> None:
    """The rejected attempt is auditable and no ``completed`` event lands."""
    with kb.connect() as conn:
        tid = _stage_running_worktree_card(conn)
        kb.complete_task(conn, tid, summary="wrote files, never opened a PR")

        events = kb.list_events(conn, tid)
        refusals = [e for e in events if e.kind == "completion_refused_missing_pr"]
        assert refusals, "a completion_refused_missing_pr event must be recorded"
        assert refusals[0].payload.get("summary_preview") == (
            "wrote files, never opened a PR"
        )
        assert not [e for e in events if e.kind == "completed"], \
            "a refused completion must not emit a 'completed' event"


def test_complete_allows_dir_workspace_card_without_pr(kanban_home: Path) -> None:
    """A ``dir`` (plain shared directory) workspace is NOT PR-requiring — it is
    not an isolated git worktree cut for a branch/PR (it can be a persistent
    build dir or an edit-in-place config tree), so it completes with no PR.
    Only ``worktree`` cards owe a PR."""
    with kb.connect() as conn:
        tid = _stage_running_worktree_card(
            conn, workspace_kind="dir", workspace_path="/Users/x/src/repo"
        )

        assert kb.complete_task(conn, tid, summary="dir build, no PR") is True
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# RED 2 — a PR-requiring card WITH a PR artifact completes
# ---------------------------------------------------------------------------


def test_complete_allows_worktree_card_with_pr_comment(kanban_home: Path) -> None:
    """When the card carries a resolvable ``pull/<n>`` URL in a comment (the
    implementer's ready-for-review handoff), completion proceeds normally."""
    with kb.connect() as conn:
        tid = _stage_running_worktree_card(conn)
        kb.add_comment(
            conn, tid, author="eckert",
            body=f"Draft PR opened: {_PR_URL} @ head abc1234. 240 tests green.",
        )

        ok = kb.complete_task(conn, tid, summary="merged", allow_acceptance_complete=False)

        # NOTE: allow_acceptance_complete=False here proves the PR presence — not
        # the override — is what unlocks completion.
        assert ok is True, "a worktree card WITH a PR artifact must complete"
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# RED 3 — an edit-in-place / no-PR card completes exactly as before
# ---------------------------------------------------------------------------


def test_complete_allows_scratch_card_without_pr(kanban_home: Path) -> None:
    """A ``scratch`` (no git worktree) card is NOT PR-requiring and completes
    with no PR — the default, most common card shape is unchanged."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="scratch task", assignee="salton")
        kb.claim_task(conn, tid)
        assert kb.get_task(conn, tid).workspace_kind == "scratch"

        assert kb.complete_task(conn, tid, summary="done, no PR needed") is True
        assert kb.get_task(conn, tid).status == "done"


def test_complete_allows_edit_in_place_hermes_home_card(kanban_home: Path) -> None:
    """A ``worktree`` card whose workspace is under ``~/.hermes`` (config /
    live-install edits — ``cwest/hermes-config`` / homestead work) correctly
    completes with no PR. The signal excludes any ``~/.hermes``-anchored path,
    so even a worktree-kind card there is not held to a PR."""
    hermes_dir = str(Path.home() / ".hermes" / "hermes-agent")
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="edit config in place", assignee="salton",
            workspace_kind="worktree", workspace_path=hermes_dir,
        )
        kb.claim_task(conn, tid)

        assert kb.complete_task(conn, tid, summary="edited config, no PR") is True
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# RED 4 — the merge override bypasses the guard
# ---------------------------------------------------------------------------


def test_merge_override_completes_worktree_card_without_pr(kanban_home: Path) -> None:
    """Casey's merge path (``allow_acceptance_complete=True``) completes a
    worktree card even with no PR reference on the board — the override that
    bypasses the acceptance guard bypasses this one too."""
    with kb.connect() as conn:
        tid = _stage_running_worktree_card(conn)

        ok = kb.complete_task(
            conn, tid, summary="merged by Casey", allow_acceptance_complete=True,
        )

        assert ok is True, "the merge override must complete the card"
        assert kb.get_task(conn, tid).status == "done"
