#!/usr/bin/env python3
"""Reconcile PATCHES.md against a new upstream base tag.

This fork carries a small set of local changes on top of a tagged upstream
release. Each change is recorded as one row in PATCHES.md. When we rebase onto
a newer release, some of those changes may already be in the new base — their
upstream pull request merged and shipped. Carrying a patch the base already
contains is pointless at best and a conflict at worst, so we drop it.

This script is the read side of that decision. Given a target base tag, it:

  1. Parses the patch table out of PATCHES.md.
  2. For every row in the `upstream-pending` bucket, asks GitHub whether that
     pull request is merged, and if so whether its merge commit is an ancestor
     of the target tag (i.e. it actually shipped in that release).
  3. Prints a plan: which rows retire, which stay, and why.

With --write it also rewrites PATCHES.md: retired rows are removed and the Base
section is updated to the target tag. It never touches code — the rebase itself
drops the now-redundant commits (they apply empty against a base that already
contains them). This script only keeps the manifest honest about what survived.

Rows in the `permanent-local` bucket are never retired here; they are removed
only by a deliberate human edit.

Usage:
    # Report only (default) — prints the retire/keep plan as text.
    python scripts/fork_retire_patches.py --base-tag v2026.7.1

    # Same, but also rewrite PATCHES.md in place.
    python scripts/fork_retire_patches.py --base-tag v2026.7.1 --write

    # Emit machine-readable JSON (used by the daily-sync workflow).
    python scripts/fork_retire_patches.py --base-tag v2026.7.1 --json

Environment:
    GH_TOKEN / GITHUB_TOKEN   Used by `gh` for the merge-state queries. When no
                              token is available the script degrades to "keep
                              everything" rather than guessing, so an unverified
                              run never drops a patch by accident.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCHES_FILE = REPO_ROOT / "PATCHES.md"

# Upstream the pull requests live against. Patch rows reference upstream PR
# numbers, so merge-state queries target this repo, not the fork.
UPSTREAM_REPO = "NousResearch/hermes-agent"

# Matches a PR reference anywhere in a table cell, e.g.
#   [#44023](https://github.com/NousResearch/hermes-agent/pull/44023)
# or a bare "#44023". We only need the number.
PR_REF_RE = re.compile(r"#(\d+)")

PENDING_BUCKET = "upstream-pending"
PERMANENT_BUCKET = "permanent-local"


@dataclass
class PatchRow:
    raw: str  # the original table line, verbatim
    pr_number: int | None
    intent: str
    bucket: str
    base_tag: str
    # Filled in during reconciliation.
    decision: str = "keep"  # "keep" | "retire"
    reason: str = ""
    extra: dict = field(default_factory=dict)


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, check=check, capture_output=True, text=True, cwd=REPO_ROOT
    )


def parse_patches(text: str) -> tuple[list[str], list[PatchRow], list[str]]:
    """Split PATCHES.md into (head_lines, rows, tail_lines).

    Rows are the data rows of the single Markdown table under "## Patches".
    head_lines is everything up to and including the table's header+separator;
    tail_lines is everything after the last data row. This lets --write rebuild
    the file by dropping retired rows without disturbing prose.
    """
    lines = text.splitlines()
    head: list[str] = []
    rows: list[PatchRow] = []
    tail: list[str] = []

    # Find the table header (a line whose next line is the |---|---| separator).
    table_start = None
    for i in range(len(lines) - 1):
        if lines[i].lstrip().startswith("|") and re.match(
            r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]
        ):
            table_start = i
            break

    if table_start is None:
        # No table — treat the whole file as head, nothing to retire.
        return lines, [], []

    head = lines[: table_start + 2]  # header row + separator row
    rest = lines[table_start + 2 :]

    in_table = True
    for line in rest:
        stripped = line.strip()
        is_data_row = in_table and stripped.startswith("|") and stripped.endswith("|")
        if is_data_row:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            # Expected columns: upstream-PR# | intent | bucket | base-tag
            pr_cell = cells[0] if len(cells) > 0 else ""
            intent = cells[1] if len(cells) > 1 else ""
            bucket = cells[2] if len(cells) > 2 else ""
            base_tag = cells[3] if len(cells) > 3 else ""
            m = PR_REF_RE.search(pr_cell)
            pr_number = int(m.group(1)) if m else None
            rows.append(
                PatchRow(
                    raw=line,
                    pr_number=pr_number,
                    intent=intent,
                    bucket=bucket,
                    base_tag=base_tag,
                )
            )
        else:
            in_table = False
            tail.append(line)

    return head, rows, tail


def have_gh_token() -> bool:
    return bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))


def pr_merge_state(pr_number: int) -> dict:
    """Return {'merged': bool, 'merge_commit': str|None} for an upstream PR.

    On any failure (no token, network, gh missing) raise so the caller can
    decide to keep the patch rather than silently retire it.
    """
    proc = _run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            UPSTREAM_REPO,
            "--json",
            "merged,mergeCommit,state",
        ],
        check=True,
    )
    data = json.loads(proc.stdout)
    merge_commit = (data.get("mergeCommit") or {}).get("oid")
    return {
        "merged": bool(data.get("merged")),
        "merge_commit": merge_commit,
        "state": data.get("state"),
    }


def commit_in_tag(commit: str, tag: str) -> bool:
    """True if `commit` is an ancestor of `tag` (shipped in that release)."""
    if not commit:
        return False
    # Make sure we actually have the objects locally.
    proc = _run(["git", "merge-base", "--is-ancestor", commit, tag], check=False)
    return proc.returncode == 0


def reconcile(rows: list[PatchRow], base_tag: str) -> list[PatchRow]:
    token_available = have_gh_token()
    for row in rows:
        if row.bucket == PERMANENT_BUCKET:
            row.decision = "keep"
            row.reason = "permanent-local bucket — never auto-retired"
            continue
        if row.bucket != PENDING_BUCKET:
            row.decision = "keep"
            row.reason = f"unknown bucket {row.bucket!r} — kept for human review"
            continue
        if row.pr_number is None:
            row.decision = "keep"
            row.reason = "no upstream PR number — cannot verify, kept"
            continue
        if not token_available:
            row.decision = "keep"
            row.reason = "no GH token — merge state unverifiable, kept (safe default)"
            continue
        try:
            state = pr_merge_state(row.pr_number)
        except Exception as exc:  # noqa: BLE001 — degrade to keep on any error
            row.decision = "keep"
            row.reason = f"merge-state query failed ({exc}) — kept (safe default)"
            continue
        row.extra = state
        if not state["merged"]:
            row.decision = "keep"
            row.reason = f"PR #{row.pr_number} is {state['state']}, not merged — kept"
            continue
        shipped = commit_in_tag(state["merge_commit"], base_tag)
        if shipped:
            row.decision = "retire"
            row.reason = (
                f"PR #{row.pr_number} merged ({state['merge_commit'][:9]}) and "
                f"shipped in {base_tag} — drop local copy"
            )
        else:
            row.decision = "keep"
            row.reason = (
                f"PR #{row.pr_number} merged but not yet in {base_tag} — kept"
            )
    return rows


def rewrite_patches(
    text: str,
    head: list[str],
    rows: list[PatchRow],
    tail: list[str],
    base_tag: str,
    base_commit: str | None = None,
) -> str:
    kept = [r for r in rows if r.decision == "keep"]

    new_lines = list(head)
    for row in kept:
        # Refresh the base-tag column of surviving upstream-pending rows so the
        # manifest reflects the tag we just rebased onto.
        if row.bucket == PENDING_BUCKET:
            cells = [c.strip() for c in row.raw.strip().strip("|").split("|")]
            while len(cells) < 4:
                cells.append("")
            cells[3] = base_tag
            new_lines.append("| " + " | ".join(cells) + " |")
        else:
            new_lines.append(row.raw)
    new_lines.extend(tail)

    rebuilt = "\n".join(new_lines)
    if text.endswith("\n"):
        rebuilt += "\n"

    # Update the Base section's tag line, and the commit if we were given one.
    if base_commit:
        rebuilt = re.sub(
            r"(\*\*Base tag:\*\*\s*)`[^`]+`(\s*\(commit\s*)`[^`]+`",
            lambda m: f"{m.group(1)}`{base_tag}`{m.group(2)}`{base_commit}`",
            rebuilt,
            count=1,
        )
    else:
        rebuilt = re.sub(
            r"(\*\*Base tag:\*\*\s*)`[^`]+`",
            lambda m: f"{m.group(1)}`{base_tag}`",
            rebuilt,
            count=1,
        )
    return rebuilt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-tag",
        required=True,
        help="Target upstream release tag we are rebasing onto, e.g. v2026.7.1",
    )
    parser.add_argument(
        "--base-commit",
        default=None,
        help="Commit SHA the base tag points at; refreshes the Base section note.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Rewrite PATCHES.md in place (drop retired rows, update base tag).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the plan as JSON instead of human-readable text.",
    )
    args = parser.parse_args()

    text = PATCHES_FILE.read_text(encoding="utf-8")
    head, rows, tail = parse_patches(text)
    reconcile(rows, args.base_tag)

    retired = [r for r in rows if r.decision == "retire"]
    kept = [r for r in rows if r.decision == "keep"]

    if args.as_json:
        payload = {
            "base_tag": args.base_tag,
            "retire": [
                {
                    "pr": r.pr_number,
                    "intent": r.intent,
                    "reason": r.reason,
                    "merge_commit": r.extra.get("merge_commit"),
                }
                for r in retired
            ],
            "keep": [
                {"pr": r.pr_number, "bucket": r.bucket, "reason": r.reason}
                for r in kept
            ],
            "token_available": have_gh_token(),
        }
        print(json.dumps(payload, indent=2))
    else:
        print(f"Reconciling PATCHES.md against base tag {args.base_tag}")
        print(f"  token available: {have_gh_token()}")
        print(f"  rows: {len(rows)} total — {len(retired)} retire, {len(kept)} keep")
        print()
        for r in rows:
            mark = "RETIRE" if r.decision == "retire" else "keep  "
            pr = f"#{r.pr_number}" if r.pr_number else "(no PR)"
            print(f"  [{mark}] {pr:<9} {r.reason}")

    if args.write:
        new_text = rewrite_patches(
            text, head, rows, tail, args.base_tag, args.base_commit
        )
        PATCHES_FILE.write_text(new_text, encoding="utf-8")
        if not args.as_json:
            print()
            print(f"Wrote {PATCHES_FILE.relative_to(REPO_ROOT)} "
                  f"({len(retired)} row(s) removed).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
