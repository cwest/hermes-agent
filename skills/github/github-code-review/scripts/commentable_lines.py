#!/usr/bin/env python3
"""Compute the diff-relative lines you may anchor an inline PR review comment to.

Inline review comments must target a line that appears in the PR diff hunks
(added or context lines), NOT an arbitrary line number from the full file.
Anchoring outside the diff returns HTTP 422 "Line could not be resolved" and
fails the ENTIRE review. Run this first to get the valid RIGHT-side line set per
file, then pick your anchors from it.

Usage:
    GH_TOKEN=<token> python commentable_lines.py <owner>/<repo> <pr_number>
    # relies on `gh` being on PATH and authenticated (or GH_TOKEN in env)

Output: per file, the min/max and full list of commentable RIGHT-side lines.
To comment on a deleted line, use side:LEFT with the old-file line number
(this script reports RIGHT-side; extend the walk for LEFT if needed).
"""
import json
import re
import subprocess
import sys

HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    repo, pr = sys.argv[1], sys.argv[2]
    files = json.loads(
        subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{pr}/files", "--paginate"],
            capture_output=True, text=True, check=True,
        ).stdout
    )
    for f in files:
        patch = f.get("patch")
        if not patch:
            print(f"\n{f['filename']}: (no patch / binary)")
            continue
        right = []
        newln = None
        for line in patch.splitlines():
            m = HUNK.match(line)
            if m:
                newln = int(m.group(1))
                continue
            if newln is None:
                continue
            if line.startswith("+"):
                right.append(newln); newln += 1
            elif line.startswith("-"):
                pass  # deleted line: no new-file number
            else:
                right.append(newln); newln += 1  # context line
        rng = f"min={min(right)} max={max(right)}" if right else "none"
        print(f"\n{f['filename']}: {len(right)} commentable RIGHT lines ({rng})")
        print(f"  {right}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
