"""CLI-layer tests for ``hermes kanban reconcile-acceptance <task_id>``.

The reconcile itself (:func:`kanban_db.reconcile_merged_acceptance`) is proven by
``test_kanban_reconcile_merged_acceptance.py``. THIS file pins the thin CLI shim
around it:

* success moves the card to ``done`` and PRINTS the proven merge commit + PR URL
  (the values stamped into the ``completion_reconciled_merge`` audit event);
* each refusal prints WHICH precondition failed — a bare ``False`` / silent no-op
  is exactly what drove the raw-module debugging the verb exists to replace. The
  operator must be told whether the PR is unmerged, the URL is missing, or the
  card is not in the acceptance lane.

The shim is NOT a reimplementation and NOT a relaxation: it never bypasses the
function's GitHub-ground-truth proof. It reproduces only the cheap DB-side
precondition reads to name a refusal reason; the merge gate stays owned by
``reconcile_merged_acceptance`` and its ``_resolve_pr_merge_commit`` ground truth.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
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
    with the PR URL linked in a comment (the same fixture shape as
    ``test_kanban_reconcile_merged_acceptance.py``)."""
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
    assert kb.get_task(conn, tid).status == "blocked"
    return tid


def _run(task_id: str, capsys) -> tuple[int, str, str]:
    """Invoke the CLI handler directly and return (exit_code, stdout, stderr)."""
    args = argparse.Namespace(task_id=task_id, board=None)
    rc = kc._cmd_reconcile_acceptance(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ---------------------------------------------------------------------------
# Success — a verifiably-merged acceptance card reconciles to done and PRINTS
# the proven merge commit + PR URL
# ---------------------------------------------------------------------------


def test_cli_reconcile_success_moves_to_done_and_prints_proof(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(
        kb, "_resolve_pr_merge_commit", lambda url: ("merged", _MERGE_OID)
    )
    with kb.connect() as conn:
        tid = _stage_acceptance_card(conn)

    rc, out, err = _run(tid, capsys)

    assert rc == 0, f"a verifiably-merged card must reconcile; stderr={err!r}"
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "done"
    # The operator is shown the proven values (merge commit + PR URL).
    assert _MERGE_OID in out, "success must print the proven merge commit"
    assert _PR_URL in out, "success must print the PR URL"


# ---------------------------------------------------------------------------
# Refusal — an OPEN PR: names the merge gate as the failing precondition
# ---------------------------------------------------------------------------


def test_cli_reconcile_refuses_open_pr_names_the_reason(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(kb, "_resolve_pr_merge_commit", lambda url: ("open", None))
    with kb.connect() as conn:
        tid = _stage_acceptance_card(conn)

    rc, out, err = _run(tid, capsys)

    assert rc == 1
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked", "card stays in the lane"
    msg = (out + err).lower()
    assert "not merged" in msg or "open" in msg, \
        f"an OPEN-PR refusal must name the unmerged PR; got {out + err!r}"


def test_cli_reconcile_refuses_merged_without_oid(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(kb, "_resolve_pr_merge_commit", lambda url: ("merged", None))
    with kb.connect() as conn:
        tid = _stage_acceptance_card(conn)

    rc, out, err = _run(tid, capsys)

    assert rc == 1
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
    assert (out + err).strip(), "a merged-without-oid refusal must print a reason"


def test_cli_reconcile_refuses_unresolvable_state(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(kb, "_resolve_pr_merge_commit", lambda url: ("unknown", None))
    with kb.connect() as conn:
        tid = _stage_acceptance_card(conn)

    rc, out, err = _run(tid, capsys)

    assert rc == 1
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
    assert (out + err).strip(), "an unresolvable-state refusal must print a reason"


# ---------------------------------------------------------------------------
# Refusal — no linked PR URL: named WITHOUT ever consulting gh
# ---------------------------------------------------------------------------


def test_cli_reconcile_refuses_no_pr_url_without_calling_gh(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def _boom(url):  # pragma: no cover - must not be reached
        raise AssertionError("gh must not be consulted when no PR is linked")

    monkeypatch.setattr(kb, "_resolve_pr_merge_commit", _boom)
    with kb.connect() as conn:
        tid = _stage_acceptance_card(conn, pr_url=None)

    rc, out, err = _run(tid, capsys)

    assert rc == 1
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
    msg = (out + err).lower()
    assert "pr" in msg and ("no " in msg or "missing" in msg or "not" in msg), \
        f"a no-PR refusal must name the missing PR URL; got {out + err!r}"


# ---------------------------------------------------------------------------
# Refusal — not in the acceptance lane (generic block / running): named,
# and gh is never consulted
# ---------------------------------------------------------------------------


def test_cli_reconcile_refuses_non_acceptance_block_names_lane(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def _boom(url):  # pragma: no cover - must not be reached
        raise AssertionError("gh must not be consulted for a non-acceptance card")

    monkeypatch.setattr(kb, "_resolve_pr_merge_commit", _boom)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="generic", assignee="eckert", detached=True)
        kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, author="easley", body=f"PR: {_PR_URL}")
        assert kb.block_task(
            conn, tid, reason="needs_input: which ACL default?",
            kind="needs_input",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )

    rc, out, err = _run(tid, capsys)

    assert rc == 1
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
    assert "acceptance lane" in (out + err).lower(), \
        f"a non-acceptance refusal must name the lane gate; got {out + err!r}"


def test_cli_reconcile_refuses_running_card_names_lane(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def _boom(url):  # pragma: no cover - must not be reached
        raise AssertionError("gh must not be consulted for a running card")

    monkeypatch.setattr(kb, "_resolve_pr_merge_commit", _boom)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="in flight", assignee="easley", detached=True)
        kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, author="easley", body=f"PR: {_PR_URL}")
        assert kb.get_task(conn, tid).status == "running"

    rc, out, err = _run(tid, capsys)

    assert rc == 1
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"
    assert "acceptance lane" in (out + err).lower()


# ---------------------------------------------------------------------------
# Unknown id
# ---------------------------------------------------------------------------


def test_cli_reconcile_unknown_id(kanban_home: Path, capsys) -> None:
    rc, out, err = _run("t_does_not_exist", capsys)
    assert rc == 1
    assert "unknown" in (out + err).lower() or "no such" in (out + err).lower()


# ---------------------------------------------------------------------------
# Parser wiring — the verb exists in the argparse tree and in --help
# ---------------------------------------------------------------------------


def test_reconcile_acceptance_verb_is_wired_and_documented(kanban_home: Path) -> None:
    wrap = argparse.ArgumentParser(add_help=False)
    top = wrap.add_subparsers(dest="_top")
    kanban_parser = kc.build_parser(top)

    # It parses.
    args = kanban_parser.parse_args(["reconcile-acceptance", "t_abc123"])
    assert args.kanban_action == "reconcile-acceptance"
    assert args.task_id == "t_abc123"

    # It is registered in the handler dispatch table.
    assert "reconcile-acceptance" in kc._HANDLERS

    # It appears in the kanban --help text.
    help_text = kanban_parser.format_help()
    assert "reconcile-acceptance" in help_text
