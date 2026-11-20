"""scripts/ci/fork_hosted_runners.py: the fork's workflows target runners the fork actually has.

Upstream's workflows run on Blacksmith (a GitHub App) and on org-only larger runners.
Neither exists on a personal fork, so a job targeting one queues forever.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "fork_hosted_runners.py"

# Standard GitHub-hosted labels available to a public repo on a personal account.
HOSTED = re.compile(
    r"^(ubuntu-(latest|\d{2}\.\d{2})(-arm)?|windows-(latest|\d{4})|windows-11-arm"
    r"|macos-(latest|\d{2})(-intel|-large|-xlarge)?)$"
)


def _load():
    spec = importlib.util.spec_from_file_location("fork_hosted_runners", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rewrite(text: str) -> str:
    out, problems = _load().rewrite(text)
    assert problems == []
    return out


@pytest.mark.parametrize(
    ("label", "hosted"),
    [
        ("blacksmith-2vcpu-ubuntu-2404", "ubuntu-latest"),
        ("blacksmith-4vcpu-ubuntu-2404", "ubuntu-latest"),
        ("blacksmith-32vcpu-ubuntu-2404", "ubuntu-latest"),
        ("blacksmith-4vcpu-ubuntu-2404-arm", "ubuntu-24.04-arm"),
        ("blacksmith-32vcpu-ubuntu-2404-arm", "ubuntu-24.04-arm"),
        ("blacksmith-6vcpu-macos-latest", "macos-latest"),
        ("blacksmith-12vcpu-macos-15", "macos-latest"),
        ("blacksmith-2vcpu-windows-2025", "windows-latest"),
        ("blacksmith-32vcpu-windows-2025", "windows-latest"),
        ("windows-latest-32-core", "windows-latest"),
        ("windows-latest-32-arm-core", "windows-11-arm"),
    ],
)
def test_runner_labels_map_to_hosted_equivalents(label, hosted):
    assert _rewrite(f"    runs-on: {label}\n") == f"    runs-on: {hosted}\n"
    assert _rewrite(f"            runner: {label}\n") == f"            runner: {hosted}\n"
    assert HOSTED.match(hosted)


def test_labels_inside_expressions_and_lists_are_mapped():
    src = (
        "    runs-on: ${{ inputs.arch == 'arm64' && 'windows-latest-32-arm-core' || 'windows-latest-32-core' }}\n"
        "        os: [blacksmith-4vcpu-windows-2025, blacksmith-6vcpu-macos-latest]\n"
    )
    assert _rewrite(src) == (
        "    runs-on: ${{ inputs.arch == 'arm64' && 'windows-11-arm' || 'windows-latest' }}\n"
        "        os: [windows-latest, macos-latest]\n"
    )


def test_runner_specific_actions_become_their_upstream_equivalents():
    mod = _load()
    src = (
        "      - uses: useblacksmith/checkout@25227e61ff9dafe400e22fa487b673eac4e4409a  # v1\n"
        "        uses: useblacksmith/setup-docker-builder@19215110ab936351210feebdfa5b440b4493e184  # v2\n"
        "        uses: useblacksmith/build-push-action@9b0579bbec7a6cad2f171596c57e7ac1e7658850  # v2\n"
    )
    out = _rewrite(src).splitlines()
    for line, name in zip(out, ("checkout", "setup-docker-builder", "build-push-action")):
        repo, sha, version = mod.ACTIONS[name]
        assert line.split("uses: ")[1] == f"{repo}@{sha}  # {version}"
        assert re.fullmatch(r"[0-9a-f]{40}", sha)
    assert out[0].startswith("      - uses: ") and out[1].startswith("        uses: ")


def test_comments_are_left_alone():
    src = "    # blacksmith-4vcpu-ubuntu-2404 runner: a 2s checkout\n    runs-on: ubuntu-latest  # not blacksmith-2vcpu-ubuntu-2404\n"
    assert _rewrite(src) == src


def test_an_unknown_blacksmith_shape_fails_closed():
    _, problems = _load().rewrite(
        "      - uses: useblacksmith/stickydisk@0123456789012345678901234567890123456789\n"
        "    runs-on: blacksmith-8vcpu-freebsd-14\n"
    )
    assert [p[0] for p in problems] == [1, 2]


def test_rewrite_is_idempotent():
    src = "    runs-on: blacksmith-4vcpu-ubuntu-2404-arm\n      - uses: useblacksmith/checkout@25227e61ff9dafe400e22fa487b673eac4e4409a  # v1\n"
    once = _rewrite(src)
    assert _rewrite(once) == once


def test_timeouts_scale_where_a_large_runner_became_a_hosted_one():
    """32-vCPU Blacksmith budgets time out on a 4-vCPU hosted runner."""
    mod = _load()
    src = (
        "    runs-on: blacksmith-32vcpu-ubuntu-2404\n"
        "    timeout-minutes: 30\n"
        "        timeout-minutes: ${{ inputs.slow && 60 || 20 }}\n"
    )
    out = _rewrite(src)
    assert out.splitlines()[1] == (
        f"    timeout-minutes: {30 * mod.TIMEOUT_FACTOR}  # {mod.TIMEOUT_MARK} 30"
    )
    assert out.splitlines()[2] == src.splitlines()[2]  # an expression is left alone
    assert _rewrite(out) == out
    # a file whose runners were already small keeps upstream's budget
    small = "    runs-on: blacksmith-4vcpu-ubuntu-2404\n    timeout-minutes: 3\n"
    assert _rewrite(small).splitlines()[1] == "    timeout-minutes: 3"
    # never past the hosted-runner job limit; an upstream budget already at it stays put
    big = "    runs-on: blacksmith-32vcpu-ubuntu-2404\n    timeout-minutes: 180\n    timeout-minutes: 900\n"
    assert _rewrite(big).splitlines()[1:] == [
        f"    timeout-minutes: {mod.TIMEOUT_CAP}  # {mod.TIMEOUT_MARK} 180",
        "    timeout-minutes: 900",
    ]


def test_cli_rewrites_a_tree_and_check_reports_drift(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("jobs:\n  a:\n    runs-on: blacksmith-4vcpu-ubuntu-2404\n")

    def run(*args):
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path), *args],
                              capture_output=True, text=True, check=False)

    assert run("--check").returncode == 1
    assert run().returncode == 0
    assert (wf / "ci.yml").read_text() == "jobs:\n  a:\n    runs-on: ubuntu-latest\n"
    assert run("--check").returncode == 0


def test_the_fork_tree_targets_only_runners_the_fork_has():
    """Every literal runner label in the committed workflows is GitHub-hosted."""
    ran = subprocess.run([sys.executable, str(SCRIPT), "--root", str(ROOT), "--check"],
                         capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    label = re.compile(r"^\s*(?:runs-on|runner):\s*([A-Za-z0-9][\w.-]*)\s*(?:#.*)?$")
    bad = []
    for path in sorted((ROOT / ".github").rglob("*.y*ml")):
        for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            m = label.match(line)
            if m and not HOSTED.match(m.group(1)):
                bad.append(f"{path.relative_to(ROOT)}:{n}: {m.group(1)}")
    assert bad == []
