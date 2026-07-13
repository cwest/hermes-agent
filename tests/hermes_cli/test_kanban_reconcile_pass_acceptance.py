"""Reconcile a PASS'd card into the atomic acceptance lane (``blocked`` + casey).

Card t_cc36de51. The PASS -> acceptance transition must be ATOMIC: a card Lamport
PASSes lands in Casey's lane as ``status=blocked`` AND ``assignee=casey`` together,
with a ``blocked`` event emitted (that event is what fires the acceptance
notification — ``gateway/kanban_watchers.py`` pings on ``kind == "blocked"``).

The root-cause bug this pins: on the PR path the reviewer was documented to
``kanban_block`` and let a (non-existent) orchestrator hop ``assign casey``. In
practice the OPPOSITE half fired — cards got ``assigned {to: casey}`` with NO
``block`` event, stranding them at ``review``/casey: Lamport's lane with Casey's
name on it. That state is contradictory AND silent (no ``blocked`` event -> the
acceptance ping never fires). The primary fix is the ``sdlc-review`` skill making
the reviewer do BOTH halves itself. THIS test pins the board-internal safety net:
a housekeeping reconciler repairs a stray ``review``/casey card to the atomic
``blocked``/casey acceptance state, emitting the ``blocked`` event — so the
invariant holds even if a reviewer (or a stale orchestrator hop) drops half the
transition.

The invariant, stated once: **a PASS'd card in Casey's lane is ``blocked`` AND
``casey``, atomically, with a ``blocked``/``awaiting-casey-signoff`` event.**
``assign casey`` without ``block`` = wrong lane, silent -> reconcile.

Card t_5c5718f4 adds the STALE-PASS guard: the reconcile must only promote a
``review`` card when the PASS it found is CURRENT for the live PR — i.e. the PR is
UNDRAFTED and the PASS gist was written AT OR AFTER the live head's commit time
(so the review covered this head, not a superseded one). A reworked/re-drafted
card whose only PASS predates a newer pushed head (or whose PR is back in draft
for a fresh round) must NOT be force-promoted on the stale verdict; it stays in
``review`` for its round-N reviewer.

The freshness gate keys on data already on the board — the gist's own
``created_at`` versus the live head's commit time — so it needs NO producer to
pin a SHA in prose. A gist WITHOUT a ``at head <sha>`` token is therefore still
reconciled when it is fresh + undrafted (the deployed reviewer PASS reason
carries no SHA); this is the "happy path preserved" contract. When a gist DOES
pin a SHA, it is an additional belt-and-suspenders check that must match the head.
The live head/draft/commit-time hook is monkeypatched off the network for every
test — it returns a ``(head_sha, is_draft, head_committed_epoch)`` triple.
"""

from __future__ import annotations

import time
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
_HEAD_SHA = "bc3e527aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
# An hour in the past — a head committed BEFORE the PASS gist (which tests add
# "now") so the freshness gate passes on every happy-path case. The stale cases
# override the hook to return a head committed AFTER the gist.
_HEAD_BEFORE = int(time.time()) - 3600
_HEAD_AFTER = int(time.time()) + 3600

# The deployed reviewer PASS reason carries NO SHA token (sdlc-review SKILL.md):
# ``awaiting-casey-signoff: reviewed PASS — <PR url>; threads resolved; <N> tests
# green. Ready to merge.`` This is the gist the happy path must reconcile.
_SIGNOFF_GIST = (
    f"awaiting-casey-signoff: reviewed PASS — {_PR_URL}; "
    "threads resolved; 240 tests green. Ready to merge."
)
# A gist that ALSO pins the reviewed head SHA (belt-and-suspenders extra).
_SIGNOFF_GIST_WITH_SHA = (
    f"awaiting-casey-signoff: reviewed PASS at head {_HEAD_SHA} — {_PR_URL}; "
    "threads resolved; 240 tests green. Ready to merge."
)


@pytest.fixture(autouse=True)
def _stub_pr_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: the PR is undrafted and its head was committed before the PASS.

    Every test that expects a genuine reconcile relies on the current +
    undrafted happy path; the stale / draft / no-network cases override this hook
    explicitly. Isolating the ``gh`` call behind a monkeypatchable hook keeps the
    whole suite off the network.
    """
    monkeypatch.setattr(
        kb, "_resolve_pr_head_and_draft",
        lambda pr_url: (_HEAD_SHA, False, _HEAD_BEFORE),
    )


def _stage_review_with_reviewer(conn, *, author: str = "eckert",
                                reviewer: str = "lamport") -> str:
    """Reproduce the live flow up to the card sitting in ``review`` + reviewer.

    Author builds + opens PR -> card MOVES to ``review`` + reviewer (the
    ``assigned`` event carries ``from=author, to=reviewer``). Returns the card id
    in ``review`` (the lane, not yet claimed to running) — the state the stray-lane
    bug decorates with an ``assign casey`` that forgets the ``block``.
    """
    tid = kb.create_task(conn, title="feature work", assignee=author)
    kb.claim_task(conn, tid)
    kb.complete_task(conn, tid, result=f"PR opened: {_PR_URL}")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='review', assignee=? WHERE id=?", (reviewer, tid)
        )
        kb._append_event(
            conn, tid, "status_changed",
            {"from": "ready", "to": "review", "by": "onecard:move_card"},
        )
        kb._append_event(
            conn, tid, "assigned",
            {"from": author, "to": reviewer, "by": "onecard:move_card"},
        )
    task = kb.get_task(conn, tid)
    assert task.status == "review" and task.assignee == reviewer
    return tid


def _stage_wrong_lane_review_casey(conn, *, author: str = "eckert",
                                   reviewer: str = "lamport",
                                   gist: str = _SIGNOFF_GIST) -> str:
    """Reproduce the BUG state: a PASS that did ``assign casey`` WITHOUT ``block``.

    The card ends ``review``/casey — Lamport's lane with Casey's name — and NO
    ``blocked`` event ever fired, so the acceptance ping never triggered. This is
    the exact stranded state live cards hit this session. Returns the card id.
    """
    tid = _stage_review_with_reviewer(conn, author=author, reviewer=reviewer)
    # The reviewer PASS'd and (per the broken hop) reassigned to casey without a
    # status flip: status stays 'review', assignee becomes 'casey'. The PASS gist
    # is recorded as an audit comment (mirrors the §9.1 audit shape).
    kb.add_comment(conn, tid, author="lamport", body=f"[audit] status=PASS {gist}")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET assignee='casey' WHERE id=?", (tid,))
        kb._append_event(
            conn, tid, "assigned",
            {"from": reviewer, "to": "casey", "by": "reviewer:pass"},
        )
    task = kb.get_task(conn, tid)
    assert task.status == "review" and task.assignee == "casey", "staged the bug state"
    return tid


# ---------------------------------------------------------------------------
# RED 1 — a stray review/casey card is reconciled to atomic blocked/casey
# ---------------------------------------------------------------------------


def test_wrong_lane_review_casey_reconciled_to_blocked_casey(kanban_home: Path) -> None:
    """A card stranded at ``review``/casey (assign-casey WITHOUT block) must be
    repaired to ``blocked``/casey ATOMICALLY, emitting the ``blocked`` event that
    fires the acceptance ping — when the PASS is CURRENT (fresh, undrafted).

    The gist carries NO SHA token (the deployed reviewer PASS reason) — proving
    the happy path fires on a real gist, not only when a SHA is pinned."""
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 1, "exactly one stray card should reconcile"
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "card must land in the acceptance lane (blocked)"
        assert task.assignee == "casey", "card must stay with casey"
        # A ``blocked`` event MUST be emitted — that is what fires the acceptance
        # notification (kanban_watchers pings on kind == 'blocked').
        events = kb.list_events(conn, tid)
        blocked = [e for e in events if e.kind == "blocked"]
        assert blocked, "a 'blocked' event is required to fire the acceptance ping"
        # The sticky block carries the awaiting-casey-signoff reason.
        reason = kb._latest_sticky_block_reason(conn, tid)
        assert reason and "awaiting-casey-signoff" in reason, \
            "the acceptance block reason must be awaiting-casey-signoff"


def test_reconcile_emits_audit_comment_naming_pr(kanban_home: Path) -> None:
    """The reconcile must leave an audit trail naming the PR so the lane and the
    trail are honest."""
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)
        kb.reconcile_pass_acceptance(conn)
        comments = kb.list_comments(conn, tid)
        audit = [c for c in comments
                 if (c.body or "").lstrip().startswith("[audit]")
                 and "awaiting-casey-signoff" in (c.body or "")]
        assert audit, "an audit comment recording the acceptance reconcile is required"
        assert any("pull/71" in (c.body or "") for c in comments), \
            "the audit comment must name the PR"


# ---------------------------------------------------------------------------
# RED 2 — idempotency
# ---------------------------------------------------------------------------


def test_correct_blocked_casey_card_is_untouched(kanban_home: Path) -> None:
    """A card ALREADY correctly at ``blocked``/casey (the reviewer did both halves)
    must NOT be reconciled — it's already in the right lane."""
    with kb.connect() as conn:
        tid = _stage_review_with_reviewer(conn)
        # Reviewer claims the review card, then does it right: clean block THEN
        # assign casey (both halves).
        rt = kb.claim_review_task(conn, tid)
        assert rt is not None and rt.status == "running"
        assert kb.block_task(
            conn, tid, reason=_SIGNOFF_GIST,
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee='casey' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "assigned",
                             {"from": "lamport", "to": "casey", "by": "reviewer:pass"})
        assert kb.get_task(conn, tid).status == "blocked"

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 0, "a correct blocked/casey card needs no reconcile"
        task = kb.get_task(conn, tid)
        assert task.status == "blocked" and task.assignee == "casey"


def test_reconcile_is_idempotent_across_ticks(kanban_home: Path) -> None:
    """Two consecutive housekeeping ticks reconcile the stray card exactly once."""
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)

        first = kb.reconcile_pass_acceptance(conn)
        second = kb.reconcile_pass_acceptance(conn)

        assert first == 1
        assert second == 0, "second tick must not re-reconcile the fixed card"
        comments = kb.list_comments(conn, tid)
        audit = [c for c in comments
                 if (c.body or "").lstrip().startswith("[audit]")
                 and "awaiting-casey-signoff" in (c.body or "")
                 and "reconcile" in (c.body or "").lower()]
        assert len(audit) == 1, f"exactly one reconcile audit comment, got {len(audit)}"


# ---------------------------------------------------------------------------
# RED 3 — a genuine review card (still under review) must NOT be reconciled
# ---------------------------------------------------------------------------


def test_review_card_with_reviewer_is_untouched(kanban_home: Path) -> None:
    """A card genuinely under review (``review``/lamport, reviewer still holding it)
    must NOT be swept into acceptance — only the casey-owned stray is a bug."""
    with kb.connect() as conn:
        tid = _stage_review_with_reviewer(conn)  # review + lamport, still running
        assert kb.get_task(conn, tid).assignee == "lamport"

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 0, "an in-flight review card must stay put"
        task = kb.get_task(conn, tid)
        assert task.status == "review" and task.assignee == "lamport"


def test_review_card_with_reviewer_and_pass_comment_is_untouched(
    kanban_home: Path,
) -> None:
    """A card still owned by its reviewer (``review``/lamport) that ALSO carries an
    ``awaiting-casey-signoff`` PASS comment must NOT be reconciled to
    ``blocked``/lamport.

    This is the case the guard has to exclude: the reviewer posted the PASS gist
    but has not yet done the ``assign`` half, so the card is still legitimately in
    the reviewer's hands. Reconciling here would produce ``blocked``/lamport — a
    block with no acceptance owner, the exact wrong-lane state this reconciler
    exists to prevent. The owner resolution must key on a reassignment that moved
    the card OFF the reviewer (``from == reviewer``, ``to != reviewer``); with no
    such move, there is no acceptance owner and the card is left alone.
    """
    with kb.connect() as conn:
        tid = _stage_review_with_reviewer(conn)  # review + lamport
        # The PASS gist is recorded, but the card is STILL owned by the reviewer —
        # no assign-off-reviewer move has happened.
        kb.add_comment(
            conn, tid, author="lamport", body=f"[audit] status=PASS {_SIGNOFF_GIST}"
        )
        assert kb.get_task(conn, tid).assignee == "lamport"

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 0, (
            "a review card still owned by its reviewer must not be reconciled, "
            "even with a PASS comment present"
        )
        task = kb.get_task(conn, tid)
        assert task.status == "review" and task.assignee == "lamport", (
            "the card must stay review/lamport, never become blocked/lamport"
        )
        blocked = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"]
        assert not blocked, "no blocked event may fire for an in-flight review card"


# ---------------------------------------------------------------------------
# RED 4 — fires inside the real housekeeping tick
# ---------------------------------------------------------------------------


def test_reconcile_runs_via_dispatch_once(
    kanban_home: Path, all_assignees_spawnable
) -> None:
    """The reconcile must fire inside ``dispatch_once`` (the housekeeping tick),
    not only when called directly. After the tick the stray card is blocked/casey
    (a blocked card is not dispatchable, so it stays parked for Casey)."""
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)
        result = kb.dispatch_once(conn, spawn_fn=lambda *_: 4321, dry_run=False)
        assert getattr(result, "reconciled_pass_acceptance", 0) == 1
        task = kb.get_task(conn, tid)
        assert task.status == "blocked" and task.assignee == "casey"


# ---------------------------------------------------------------------------
# RED 5 — config toggle off disables the reconcile
# ---------------------------------------------------------------------------


def test_reconcile_disabled_by_flag(kanban_home: Path) -> None:
    """``kanban.reconcile_pass_acceptance: false`` leaves the stray card as-is."""
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)
        reconciled = kb.reconcile_pass_acceptance(conn, enabled=False)
        assert reconciled == 0
        task = kb.get_task(conn, tid)
        assert task.status == "review" and task.assignee == "casey"


# ---------------------------------------------------------------------------
# RED 6 (card t_5c5718f4) — STALE-PASS guard: fresh (post-dates head) + non-draft
# ---------------------------------------------------------------------------


def test_stale_pass_predating_new_head_is_not_reconciled(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card whose only PASS PREDATES a newer pushed head must NOT be promoted.

    This is the t_6d8c9da6 loop stripped to its freshness core: round 1 PASSed at
    an older head and parked for acceptance; round 2 pushed a NEW head (its commit
    time is AFTER the round-1 PASS gist). The reconcile must NOT re-promote on the
    superseded PASS — the head moved on after the reviewer PASSed, so the PASS is
    for a head that no longer exists. The card stays in ``review`` for round N.
    """
    live_head = "8421cf5bb22c00ffee1122334455667788990011"
    # The live head was committed AFTER the PASS gist (staged "now"), so the PASS
    # is stale relative to the current head.
    monkeypatch.setattr(
        kb, "_resolve_pr_head_and_draft",
        lambda pr_url: (live_head, False, _HEAD_AFTER),
    )
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 0, "a PASS predating the new head must not force-promote"
        task = kb.get_task(conn, tid)
        assert task.status == "review", "the reworked card must stay in review"
        blocked = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"]
        assert not blocked, "no blocked event may fire on a stale PASS"


def test_stale_sha_gist_is_not_reconciled(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-suspenders: when a gist DOES pin a SHA and it no longer matches the
    live head, the card is not promoted even if the timestamps happen to line up.

    Guards the case where a reviewer pinned an explicit ``at head <sha>`` token
    and a later same-second force-push moved the head to a different SHA.
    """
    live_head = "8421cf5bb22c00ffee1122334455667788990011"
    # Head resolves fresh (committed before the gist) so ONLY the SHA-mismatch
    # belt check can stop this promotion.
    monkeypatch.setattr(
        kb, "_resolve_pr_head_and_draft",
        lambda pr_url: (live_head, False, _HEAD_BEFORE),
    )
    with kb.connect() as conn:
        # The gist pins the round-1 SHA (_HEAD_SHA), which != the live head.
        tid = _stage_wrong_lane_review_casey(conn, gist=_SIGNOFF_GIST_WITH_SHA)

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 0, "a pinned SHA that != live head must not promote"
        task = kb.get_task(conn, tid)
        assert task.status == "review", "the card stays in review on a SHA mismatch"


def test_draft_pr_is_not_reconciled(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card whose PR is back in DRAFT (a fresh review round is in progress) must
    NOT be reconciled even when the PASS is fresh — a draft PR has not passed its
    current-round review. This alone kills the reported t_6d8c9da6 loop, since
    round 2 re-drafted the PR."""
    monkeypatch.setattr(
        kb, "_resolve_pr_head_and_draft",
        lambda pr_url: (_HEAD_SHA, True, _HEAD_BEFORE),
    )
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 0, "a draft PR must not be force-promoted"
        task = kb.get_task(conn, tid)
        assert task.status == "review", "the draft card must stay in review"


def test_no_pinned_sha_gist_still_reconciles_when_fresh(
    kanban_home: Path,
) -> None:
    """A PASS gist with NO ``at head <sha>`` token STILL reconciles when it is
    fresh + undrafted.

    This is the load-bearing "happy path preserved" contract and the fix for the
    review finding: the deployed reviewer PASS reason carries no SHA, so keying the
    gate on a SHA token would make the net a no-op in production. The freshness
    gate (gist post-dates the head) keys on data already on the board and does not
    need the producer to pin a SHA. ``_stub_pr_head`` returns a head committed
    before the (no-SHA) gist, so the reconcile fires.
    """
    with kb.connect() as conn:
        # _SIGNOFF_GIST has no SHA token — the real reviewer reason.
        tid = _stage_wrong_lane_review_casey(conn, gist=_SIGNOFF_GIST)

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 1, "a fresh, undrafted, no-SHA PASS must reconcile"
        task = kb.get_task(conn, tid)
        assert task.status == "blocked" and task.assignee == "casey"


def test_no_pr_url_gist_is_skipped(kanban_home: Path) -> None:
    """A PASS gist with NO resolvable PR URL is skipped — the reconcile cannot
    verify the live head, so it must not promote on an unverifiable gist."""
    no_url_gist = (
        "awaiting-casey-signoff: reviewed PASS; threads resolved; "
        "240 tests green. Ready to merge."
    )
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn, gist=no_url_gist)

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 0, "a gist with no PR URL must be skipped"
        task = kb.get_task(conn, tid)
        assert task.status == "review", "the card stays in review for a human"


def test_unresolvable_live_head_is_skipped(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the live PR head cannot be resolved (gh missing / network error →
    ``(None, None, None)``), the reconcile must SKIP rather than promote on an
    unverifiable comparison — fail closed on the acceptance side."""
    monkeypatch.setattr(
        kb, "_resolve_pr_head_and_draft", lambda pr_url: (None, None, None)
    )
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 0, "an unresolvable live head must not promote"
        task = kb.get_task(conn, tid)
        assert task.status == "review"


def test_unresolvable_head_commit_time_is_skipped(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the head resolves but its commit time does NOT (empty commits list →
    epoch None), the reconcile must fail closed — the freshness comparison is
    unverifiable, so skip rather than promote."""
    monkeypatch.setattr(
        kb, "_resolve_pr_head_and_draft",
        lambda pr_url: (_HEAD_SHA, False, None),
    )
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 0, "an unresolvable head commit time must not promote"
        task = kb.get_task(conn, tid)
        assert task.status == "review"


def test_current_fresh_and_undrafted_reconciles_once(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path preserved: a card whose CURRENT PASS post-dates the live head
    AND is undrafted reconciles to blocked/casey exactly once (idempotent)."""
    monkeypatch.setattr(
        kb, "_resolve_pr_head_and_draft",
        lambda pr_url: (_HEAD_SHA, False, _HEAD_BEFORE),
    )
    with kb.connect() as conn:
        tid = _stage_wrong_lane_review_casey(conn)

        first = kb.reconcile_pass_acceptance(conn)
        second = kb.reconcile_pass_acceptance(conn)

        assert first == 1, "a fresh undrafted PASS reconciles"
        assert second == 0, "idempotent — the second tick is a no-op"
        task = kb.get_task(conn, tid)
        assert task.status == "blocked" and task.assignee == "casey"


def test_prefers_latest_pass_gist(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When several PASS gists exist across rounds, the FRESHEST one is matched.

    Round 1 posted an early PASS; round 2 (the later comment) is the current PASS.
    The live head was committed before round 2 but after round 1. Keying on the
    latest gist means the freshness check passes (round-2 gist post-dates the
    head); keying on the first would fail it. The reconcile must honor the latest.
    """
    live_head = "8421cf5bb22c00ffee1122334455667788990011"
    monkeypatch.setattr(
        kb, "_resolve_pr_head_and_draft",
        lambda pr_url: (live_head, False, _HEAD_BEFORE),
    )
    round1 = (
        f"awaiting-casey-signoff: reviewed PASS — {_PR_URL}; round 1. Ready to merge."
    )
    round2 = (
        f"awaiting-casey-signoff: reviewed PASS — {_PR_URL}; round 2. Ready to merge."
    )
    with kb.connect() as conn:
        tid = _stage_review_with_reviewer(conn)
        kb.add_comment(conn, tid, author="lamport",
                       body=f"[audit] status=PASS {round1}")
        kb.add_comment(conn, tid, author="lamport",
                       body=f"[audit] status=PASS {round2}")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee='casey' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "assigned",
                             {"from": "lamport", "to": "casey", "by": "reviewer:pass"})

        reconciled = kb.reconcile_pass_acceptance(conn)

        assert reconciled == 1, "the freshest PASS must reconcile"
        task = kb.get_task(conn, tid)
        assert task.status == "blocked" and task.assignee == "casey"
