#!/usr/bin/env bash
# Live E2E for card t_251b8b9e: a no-PR ``review-required:`` block must be
# auto-promoted into the review lane on the next dispatcher tick, with the
# reviewer resolved from the card's owner map and spawned.
#
# Runs against a THROWAWAY temp board (HERMES_KANBAN_DB pinned to a tmp file) so
# it never touches the live board. Drives the REAL ``hermes kanban`` CLI end to
# end: create -> stamp owner map -> block review-required -> dispatch, then
# asserts the card is in ``review`` + the owner-map reviewer and that the
# dispatcher reports it spawned.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

export HERMES_KANBAN_DB="$TMP/kanban.db"
HERMES=".venv/bin/hermes"
PY=".venv/bin/python"

echo "== temp board: $HERMES_KANBAN_DB =="

# 1. Init the board.
$HERMES kanban init >/dev/null

# 2. File a throwaway no-PR (edit-in-place) card assigned to the author.
TID="$($PY - <<'PYEOF'
from hermes_cli import kanban_db as kb
with kb.connect() as conn:
    tid = kb.create_task(conn, title="edit-in-place skill fix (E2E throwaway)", assignee="eckert")
    # Stamp the submit-stage owner map — the reviewer resolves from state_owners[review].
    body = (
        "[audit] actor=hollis stage=submit ts=2026-07-26T14:00:00Z\n"
        "notes: state_owners={ready: eckert, review: lamport, "
        "blocked-acceptance: casey} triager=hollis team=engineering"
    )
    kb.add_comment(conn, tid, author="hollis", body=body)
print(tid)
PYEOF
)"
echo "== filed card: $TID =="

# 3. The author claims + does the work, then ends the lane with a clean
#    review-required block (kind=needs_input, the live worker shape).
$PY - "$TID" <<'PYEOF'
import sys
from hermes_cli import kanban_db as kb
tid = sys.argv[1]
with kb.connect() as conn:
    kb.claim_task(conn, tid)
    ok = kb.block_task(
        conn, tid,
        reason="review-required: edited ~/.hermes/skills/homestead/foo/SKILL.md; "
               "61/61 tests green, ruff clean. Please review the edit-in-place change.",
        kind="needs_input",
        expected_run_id=kb.get_task(conn, tid).current_run_id,
    )
    assert ok, "block_task failed"
    t = kb.get_task(conn, tid)
    assert t.status == "blocked" and t.assignee == "eckert", (t.status, t.assignee)
    assert t.worker_pid is None, "clean block must clear worker_pid"
print("blocked review-required OK")
PYEOF

echo "== board state BEFORE dispatch =="
$HERMES kanban show "$TID" 2>/dev/null | grep -iE "status|assignee" | head -4 || true

# 4. Run the REAL dispatcher tick (dry-run so no live worker subprocess is
#    launched, but the promotion + spawn decision is exercised for real).
echo "== dispatch (real CLI, --json --dry-run) =="
OUT="$($HERMES kanban dispatch --json --dry-run 2>/dev/null)"
echo "$OUT" | $PY -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps({k:d[k] for k in ("promoted_no_pr_review","routed_review_bounce","spawned") if k in d}, indent=2))'

# 5. Assert the outcome by ground truth.
$PY - "$TID" "$OUT" <<'PYEOF'
import sys, json
from hermes_cli import kanban_db as kb
tid = sys.argv[1]
d = json.loads(sys.argv[2])

assert d.get("promoted_no_pr_review") == 1, \
    f"expected promoted_no_pr_review==1, got {d.get('promoted_no_pr_review')}"
spawned_ids = [s["task_id"] for s in d.get("spawned", [])]
spawned_who = {s["task_id"]: s["assignee"] for s in d.get("spawned", [])}
assert tid in spawned_ids, f"card {tid} not in dispatcher's spawned list: {spawned_ids}"
assert spawned_who[tid] == "lamport", \
    f"expected reviewer lamport spawned, got {spawned_who[tid]}"

# NB: --dry-run does not persist the claim, so re-check the promotion via a real
# (non-dry) tick's persisted state instead.
PYEOF

echo "== dispatch (real CLI, persisting) — prove the card MOVES to review+lamport =="
$HERMES kanban dispatch --json >/dev/null 2>&1 || true
$PY - "$TID" <<'PYEOF'
import sys
from hermes_cli import kanban_db as kb
tid = sys.argv[1]
with kb.connect() as conn:
    t = kb.get_task(conn, tid)
    # After a persisting tick the card is either in review (assigned lamport) or
    # already claimed to running by the same tick's review-spawn loop.
    assert t.status in ("review", "running"), f"status={t.status} (expected review/running)"
    assert t.assignee == "lamport", f"assignee={t.assignee} (expected lamport)"
    # The §9.1 audit comment naming the dispatcher as the actor.
    audit = [c for c in kb.list_comments(conn, tid)
             if (c.body or "").lstrip().startswith("[audit]")
             and "actor=dispatcher" in (c.body or "")
             and "review-required" in (c.body or "")]
    assert audit, "missing dispatcher audit comment for the promotion"
    print(f"PROMOTED: {tid} -> status={t.status} assignee={t.assignee}")
    print(f"AUDIT: {audit[-1].body.splitlines()[0]}")
PYEOF

echo "== E2E PASS =="
