#!/usr/bin/env python3
"""Point the fork's workflows at runners a personal-account fork actually has.

Upstream runs CI on Blacksmith (a GitHub App installed on the NousResearch org)
and on org-only larger runners (``windows-latest-32-core``). A fork has neither,
so every job targeting one queues forever. This rewrites those labels to their
GitHub-hosted equivalents and swaps the Blacksmith-only actions for the upstream
actions they wrap.

The fork's resync regenerates the rewrite on each new upstream base by running
this script (the commit carrying it has a ``Fork-Regenerate:`` trailer), so the
fork patch never has to rebase 40 hand-edited workflow files through upstream
churn. A Blacksmith reference this table does not cover fails the run instead of
being left to queue. Literal ``timeout-minutes`` in a file that used a 12+ vCPU
runner are scaled for the 4-vCPU hosted runner that replaces it.

    python scripts/ci/fork_hosted_runners.py            # rewrite in place
    python scripts/ci/fork_hosted_runners.py --check    # exit 1 if a rewrite is due
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fork_runner_sizing import size_jobs

# name -> (replacement repo, pinned commit, version comment)
ACTIONS = {
    "checkout": (
        "actions/checkout",
        "f548e57e544e1ff5a4c46bf1e1b8685f8e4a348a",
        "v7.0.1",
    ),
    "setup-docker-builder": (
        "docker/setup-buildx-action",
        "8d2750c68a42422c14e847fe6c8ac0403b4cbd6f",
        "v3",
    ),
    "build-push-action": (
        "docker/build-push-action",
        "10e90e3645eae34f1e60eeb005ba3a3d33f178e8",
        "v6.19.2",
    ),
}

_ACTION = re.compile(r"useblacksmith/([\w.-]+)@[0-9a-f]{40}(?:\s+#.*)?$")
_BLACKSMITH = re.compile(
    r"\bblacksmith-\d+vcpu-(?P<os>ubuntu|macos|windows)-(?P<ver>[\w.]+?)(?P<arm>-arm)?(?=$|[^\w.-])"
)
_LARGER = re.compile(r"\b(?P<os>ubuntu|windows)-latest-\d+-(?P<arm>arm-)?core\b")
_LEFTOVER = re.compile(r"useblacksmith/|\bblacksmith-\d+vcpu-")
# A ``#`` that starts a YAML comment: at line start or after whitespace.
_COMMENT = re.compile(r"(^|\s)#")

# Upstream sizes job timeouts for 12-32 vCPU runners; a public repo's hosted runner
# has 3-4. A file that used a large runner gets its literal timeouts scaled.
_LARGE_RUNNER = re.compile(r"\bblacksmith-(?:1[2-9]|[2-9]\d|\d{3,})vcpu-|\b(?:ubuntu|windows)-latest-\d+-")
_TIMEOUT = re.compile(r"^(\s*timeout-minutes:\s*)(\d+)\s*$")
TIMEOUT_FACTOR = 3
TIMEOUT_CAP = 360  # GitHub-hosted job limit (6 h)
TIMEOUT_MARK = "fork: hosted runner; upstream"
# The trailer on a commit that is this tool's output (git log --grep, one line).
_REGENERATED = r"^Fork-Regenerate: .*scripts/ci/fork_hosted_runners\.py"


def _hosted(os_name: str, version: str, arm: bool) -> str:
    if os_name == "ubuntu":
        # No ``-latest`` alias exists for arm; keep the image's own release.
        return f"ubuntu-{version[:2]}.{version[2:]}-arm" if arm else "ubuntu-latest"
    if os_name == "windows":
        return "windows-11-arm" if arm else "windows-latest"
    return "macos-latest"


def _label(m: re.Match) -> str:
    return _hosted(m["os"], m["ver"], bool(m["arm"]))


def _larger(m: re.Match) -> str:
    return _hosted(m["os"], "2404", bool(m["arm"]))


def _action(m: re.Match) -> str:
    repo, sha, version = ACTIONS[m[1]]
    return f"{repo}@{sha}  # {version}"


def rewrite(text: str) -> tuple[str, list[tuple[int, str]]]:
    """Rewrite one file's text; returns (text, [(line number, unhandled line)])."""
    text = size_jobs(text)
    out, problems = [], []
    large = any(
        _LARGE_RUNNER.search(_COMMENT.split(ln, maxsplit=1)[0]) for ln in text.splitlines()
    )
    for n, line in enumerate(text.splitlines(keepends=True), 1):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        cut = _COMMENT.search(body)
        code = body[: cut.start()] if cut and not _ACTION.search(body) else body
        rest = body[len(code):]
        m = _ACTION.search(code)
        if m and m[1] in ACTIONS:
            code = code[: m.start()] + _action(m)
        code = _LARGER.sub(_larger, _BLACKSMITH.sub(_label, code))
        t = _TIMEOUT.match(code)
        if large and t and not rest and int(t[2]) < TIMEOUT_CAP:
            minutes = int(t[2])
            scaled = min(minutes * TIMEOUT_FACTOR, TIMEOUT_CAP)
            code = f"{t[1]}{scaled}  # {TIMEOUT_MARK} {minutes}"
        if _LEFTOVER.search(code):
            problems.append((n, body.strip()))
        out.append(code + rest + eol)
    return "".join(out), problems


def workflow_files(root: Path) -> list[Path]:
    gh = root / ".github"
    files = [*(gh / "workflows").glob("*.y*ml"), *(gh / "actions").rglob("*.y*ml")]
    return sorted(p for p in files if p.is_file())


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)


def upstream_base(root: Path) -> str | None:
    """The commit holding upstream's own workflows: the parent of the first commit that carries
    this tool's output (its ``Fork-Regenerate:`` trailer). A later regeneration runs on top of
    that output, where the Blacksmith labels the sizing reads are already gone, so it must start
    from upstream's text again. None when ``root`` is not a checkout or nothing was generated yet.
    """
    top = _git(root, "rev-parse", "--show-toplevel")
    if top.returncode or Path(top.stdout.decode().strip()).resolve() != root.resolve():
        return None
    log = _git(root, "log", "--format=%H", f"--grep={_REGENERATED}", "HEAD")
    shas = log.stdout.decode().split() if log.returncode == 0 else []
    if not shas:
        return None
    parent = _git(root, "rev-parse", "--verify", "-q", f"{shas[-1]}^")
    return parent.stdout.decode().strip() if parent.returncode == 0 else None


def source_text(root: Path, path: Path, base: str | None) -> str:
    """Upstream's text for ``path``: from ``base`` when it has the file, else the working tree."""
    if base:
        blob = _git(root, "show", f"{base}:{path.relative_to(root).as_posix()}")
        if blob.returncode == 0:
            return blob.stdout.decode("utf-8-sig")
    return path.read_text(encoding="utf-8-sig")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument("--check", action="store_true", help="report, don't write")
    a = ap.parse_args(argv)

    base = upstream_base(a.root)
    due, problems = [], []
    for path in workflow_files(a.root):
        text = path.read_text(encoding="utf-8-sig")
        new, bad = rewrite(source_text(a.root, path, base))
        rel = path.relative_to(a.root)
        problems += [f"{rel}:{n}: {line}" for n, line in bad]
        if new != text:
            due.append(rel)
            if not a.check:
                path.write_text(new, encoding="utf-8")
    for p in problems:
        print(f"unhandled Blacksmith reference: {p}", file=sys.stderr)
    verb = "needs rewrite" if a.check else "rewrote"
    for rel in due:
        print(f"{verb}: {rel}")
    if problems:
        return 2
    return 1 if a.check and due else 0


if __name__ == "__main__":
    sys.exit(main())
