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
from ruamel.yaml import YAML

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


def _yaml(text: str):
    return YAML(typ="safe").load(text)


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


def test_a_later_regeneration_sizes_from_upstreams_text_not_the_last_output(tmp_path):
    """Each Fork-Regenerate replay runs the tool on top of the previous regeneration, whose
    output a newer tool may extend (sizing came after the label rewrite). The Blacksmith labels
    it sizes from are gone by then, so it reads upstream's workflows from the commit before the
    first regeneration instead of the working tree."""
    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c",
                        "commit.gpgsign=false", *args], cwd=tmp_path, check=True,
                       capture_output=True)

    def run(*args):
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path), *args],
                              capture_output=True, text=True, check=False)

    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yml").write_text(SLICED)
    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "upstream")
    # An older tool's output: labels rewritten, nothing sized.
    (wf / "tests.yml").write_text(SLICED.replace("blacksmith-32vcpu-ubuntu-2404", "ubuntu-latest"))
    git("commit", "-q", "-am", "regenerate\n\nFork-Regenerate: python3 scripts/ci/fork_hosted_runners.py")
    assert run("--check").returncode == 1
    assert run().returncode == 0
    assert (wf / "tests.yml").read_text() == _rewrite(SLICED)
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


SLICED = """\
jobs:
  test:
    name: Run tests (${{ matrix.slice }}/3)
    runs-on: blacksmith-32vcpu-ubuntu-2404
    strategy:
      matrix:
        slice: [1, 2, 3]
    steps:
      - name: Run tests
        run: just test-all
        env:
          HERMES_TEST_SLICE: ${{ matrix.slice }}/3
          HERMES_TEST_WORKERS: 32
          KEEP: 3
"""

OS_LANES = """\
jobs:
  os:
    name: ${{ matrix.name }}
    runs-on: ${{ matrix.runner }}
    strategy:
      matrix:
        include:
          - name: macOS-only tests (1/2)
            runner: blacksmith-12vcpu-macos-latest
            marker: macos
            slice: 1/2
          - name: macOS-only tests (2/2)
            runner: blacksmith-12vcpu-macos-latest
            marker: macos
            slice: 2/2
          - name: Windows-only tests
            runner: blacksmith-32vcpu-windows-2025
            marker: windows
    steps:
      - name: Run ${{ matrix.marker }} tests
        env:
          HERMES_TEST_WORKERS: ${{ matrix.marker == 'windows' && '16' || '' }}
          HERMES_TEST_SLICE: ${{ matrix.slice || '' }}
"""


def test_a_sliced_suite_keeps_upstreams_per_core_load():
    data = _yaml(_rewrite(SLICED))
    job = data["jobs"]["test"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["strategy"]["matrix"]["slice"] == list(range(1, 25))
    assert job["name"] == "Run tests (${{ matrix.slice }}/24)"
    env = job["steps"][0]["env"]
    assert env["HERMES_TEST_SLICE"] == "${{ matrix.slice }}/24"
    assert env["HERMES_TEST_WORKERS"] == 4
    assert env["KEEP"] == 3


def test_matrix_include_lanes_split_by_their_own_runner():
    """A 12-vCPU macOS lane becomes 4 slices on 3-vCPU macos-latest; 32-vCPU Windows 8 on 4."""
    data = _yaml(_rewrite(OS_LANES))
    lanes = data["jobs"]["os"]["strategy"]["matrix"]["include"]
    mac = [lane for lane in lanes if lane["marker"] == "macos"]
    win = [lane for lane in lanes if lane["marker"] == "windows"]
    assert [lane["slice"] for lane in mac] == [f"{i}/8" for i in range(1, 9)]
    assert [lane["name"] for lane in mac] == [f"macOS-only tests ({i}/8)" for i in range(1, 9)]
    assert {lane["runner"] for lane in mac} == {"macos-latest"}
    assert [lane["slice"] for lane in win] == [f"{i}/8" for i in range(1, 9)]
    assert [lane["name"] for lane in win] == [f"Windows-only tests ({i}/8)" for i in range(1, 9)]
    assert {lane["runner"] for lane in win} == {"windows-latest"}
    # 16 workers on 32 vCPUs is 2 on 4: the per-core ratio upstream chose for these files
    env = data["jobs"]["os"]["steps"][0]["env"]
    assert env["HERMES_TEST_WORKERS"] == "${{ matrix.marker == 'windows' && '2' || '' }}"


def test_sizing_annotates_upstreams_value_and_is_idempotent():
    mod = _load()
    once = _rewrite(SLICED)
    assert f"HERMES_TEST_WORKERS: 4  # {mod.TIMEOUT_MARK} 32" in once
    assert f"slice: [{', '.join(map(str, range(1, 25)))}]  # {mod.TIMEOUT_MARK} [1, 2, 3]" in once
    assert _rewrite(once) == once
    assert _rewrite(_rewrite(OS_LANES)) == _rewrite(OS_LANES)


def test_jobs_already_sized_for_a_small_runner_are_untouched():
    small = SLICED.replace("blacksmith-32vcpu", "blacksmith-4vcpu")
    out = _rewrite(small)
    assert "HERMES_TEST_WORKERS: 32\n" in out
    assert "slice: [1, 2, 3]\n" in out
    assert "/3)" in out


def test_sizing_stays_inside_the_job_whose_runner_shrank():
    two = SLICED + """\
  lint:
    runs-on: ubuntu-latest
    steps:
      - name: Lint
        env:
          HERMES_TEST_WORKERS: 32
"""
    data = _yaml(_rewrite(two))
    assert data["jobs"]["lint"]["steps"][0]["env"]["HERMES_TEST_WORKERS"] == 32
    assert data["jobs"]["test"]["steps"][0]["env"]["HERMES_TEST_WORKERS"] == 4


JS_CHECKS = """\
jobs:
  check:
    name: JS & TS checks
    runs-on: blacksmith-32vcpu-ubuntu-2404
    timeout-minutes: 30
    steps:
      - name: Run all workspace checks
        run: just check-js-ci
      - name: Check the checkout is clean
        uses: ./.github/actions/check-clean-worktree
"""


def test_js_checks_split_into_shards_with_vitest_capped_to_the_hosted_cores():
    """One 32-vCPU job becomes 8 shards on 4-vCPU runners, each running its checks one at a time
    with vitest's pool at 4, so a check keeps at least upstream's cores per worker."""
    mod = _load()
    out = _rewrite(JS_CHECKS)
    job = _yaml(out)["jobs"]["check"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["name"] == "JS & TS checks (${{ matrix.shard }}/8)"
    assert job["strategy"] == {"fail-fast": False, "matrix": {"shard": list(range(1, 9))}}
    step = job["steps"][0]
    assert step["run"] == ("node .github/scripts/run-workspace-checks.mjs"
                           " --shard ${{ matrix.shard }}/8 --concurrency 1")
    assert step["env"] == {"VITEST_MAX_WORKERS": 4}
    assert f"# {mod.TIMEOUT_MARK} just check-js-ci" in out
    assert job["steps"][1]["uses"] == "./.github/actions/check-clean-worktree"
    assert _rewrite(out) == out


def test_js_checks_on_a_small_runner_stay_one_job():
    out = _rewrite(JS_CHECKS.replace("blacksmith-32vcpu", "blacksmith-4vcpu"))
    job = _yaml(out)["jobs"]["check"]
    assert "strategy" not in job
    assert job["steps"][0]["run"] == "just check-js-ci"


UNSLICED_E2E = """\
jobs:
  e2e:
    if: inputs.e2e
    runs-on: blacksmith-32vcpu-ubuntu-2404
    timeout-minutes: 30
    steps:
      - name: Run messaging-gated gateway cases
        run: |
          scripts/run_tests.sh -rs tests/gateway/test_a.py
        env:
          OPENAI_API_KEY: ""
      - name: Run e2e tests
        run: |
          scripts/run_tests.sh --include-integration "${files[@]}"
        env:
          HERMES_TEST_WORKERS: "3"
          HERMES_TEST_FILE_TIMEOUT: "900"
  e2e-upgrade:
    name: e2e-upgrade (${{ matrix.shard }})
    runs-on: blacksmith-32vcpu-ubuntu-2404
    strategy:
      fail-fast: false
      matrix:
        shard: ${{ fromJSON(needs.plan.outputs.shards) }}
    steps:
      - name: Run upgrade e2e tests (${{ matrix.shard }})
        run: scripts/run_tests.sh --include-integration "${files[@]}"
        env:
          SHARD: ${{ matrix.shard }}
          HERMES_TEST_WORKERS: "6"
  install-update:
    name: Windows install.ps1 + hermes update E2E
    runs-on: blacksmith-32vcpu-windows-2025
    steps:
      - name: Run Windows install + update E2E
        shell: bash
        run: |
          scripts/run_tests.sh tests/e2e/core/windows_update/ -- -m "platforms and integration"
        env:
          HERMES_TEST_WORKERS: '6'
      - name: Every crash cell ran
        shell: pwsh
        run: |
          if (-not (Test-Path crash-cells-ran.txt)) { throw 'no crash cells ran' }
          Get-Content crash-cells-ran.txt
      - name: Upload transcripts, logs and receipts
        if: always()
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: windows-install-update-e2e-${{ github.run_attempt }}
          path: out
"""


def test_an_unsliced_suite_is_sliced_instead_of_serialised():
    """A 32-vCPU e2e job running 3 files at once becomes 8 slices of 1 worker on 4 vCPUs, not
    one job running all its files one at a time under the same cap."""
    out = _rewrite(UNSLICED_E2E)
    jobs = _yaml(out)["jobs"]
    e2e = jobs["e2e"]
    assert e2e["strategy"] == {"fail-fast": False, "matrix": {"slice": list(range(1, 9))}}
    for step in e2e["steps"]:
        assert step["env"]["HERMES_TEST_SLICE"] == "${{ matrix.slice }}/8"
    assert e2e["steps"][1]["env"]["HERMES_TEST_WORKERS"] == "1"
    up = jobs["e2e-upgrade"]
    assert up["name"] == "e2e-upgrade (${{ matrix.shard }}) (${{ matrix.slice }}/8)"
    assert up["strategy"]["matrix"]["slice"] == list(range(1, 9))
    assert up["strategy"]["matrix"]["shard"] == "${{ fromJSON(needs.plan.outputs.shards) }}"
    assert up["steps"][0]["env"]["HERMES_TEST_SLICE"] == "${{ matrix.slice }}/8"
    assert _rewrite(out) == out


def test_a_sliced_suites_per_job_steps_follow_the_slice():
    """Each slice uploads under its own artifact name, and the crash-cell manifest is required
    only of the slice that runs test_crash_cells.py."""
    job = _yaml(_rewrite(UNSLICED_E2E))["jobs"]["install-update"]
    assert job["name"] == "Windows install.ps1 + hermes update E2E (${{ matrix.slice }}/8)"
    crash = next(s for s in job["steps"] if s["name"] == "Every crash cell ran")
    assert "if" not in crash  # stays required in every slice
    assert crash["env"]["HERMES_TEST_SLICE"] == "${{ matrix.slice }}/8"
    guard = crash["run"].split("\n")
    assert guard[0] == "if ($env:HERMES_TEST_SLICE -ne '1/8') {"
    assert "throw" not in guard[2] and guard[2].strip() == "exit 0"
    assert "throw" in crash["run"] and crash["run"].endswith("Get-Content crash-cells-ran.txt\n")
    upload = next(s for s in job["steps"] if s["name"].startswith("Upload"))
    assert upload["with"]["name"] == ("windows-install-update-e2e-${{ github.run_attempt }}"
                                      "-${{ matrix.slice }}")


def test_an_unsliced_suite_on_a_small_runner_stays_one_job():
    out = _rewrite(UNSLICED_E2E.replace("blacksmith-32vcpu", "blacksmith-4vcpu"))
    assert "matrix.slice" not in out and "HERMES_TEST_SLICE" not in out


def test_only_a_suite_whose_workers_were_cut_is_sliced():
    """A job whose test runs set no worker count (an image build that runs its tests at nproc, a
    live probe of one named file) lost no parallelism to the smaller runner, so it stays one job:
    slicing it would rebuild the image per slice and rename the artifact its publish job reads."""
    build = """\
jobs:
  build:
    runs-on: blacksmith-32vcpu-ubuntu-2404
    steps:
      - name: Run docker integration tests
        run: HERMES_TEST_WORKERS=$(nproc) scripts/run_tests.sh tests/docker/
      - name: Upload
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: docker-test-image
"""
    assert "slice" not in _rewrite(build)


def test_the_crash_cell_journey_lands_in_the_slice_that_checks_its_manifest():
    """run_tests_parallel partitions from the checkout alone, so the slice holding
    test_crash_cells.py is fixed; it must be the one the workflow's manifest check runs on.
    A job that runs one journey file per runner (a ``shard`` matrix) needs no worker cut, so
    the rewrite leaves it unsliced and its check keeps upstream's per-shard gate."""
    sys.path.insert(0, str(ROOT))
    from scripts.run_tests_parallel import _discover_files, _slice_files

    wf = _yaml((ROOT / ".github" / "workflows" / "windows-install-update-e2e.yml").read_text())
    job = wf["jobs"]["install-update"]
    check = next(s for s in job["steps"] if s["name"] == "Every crash cell ran")
    matrix = job["strategy"]["matrix"]
    if "slice" not in matrix:
        assert "shard" in matrix and "HERMES_TEST_SLICE" not in str(job), job
        assert check["if"] == "matrix.shard == 'test_crash_cells.py'"
        return
    count = len(matrix["slice"])
    picked = re.search(r"-ne '(\d+)/\d+'", check["run"])
    assert picked, check["run"]
    index = int(picked[1])
    files = _discover_files([ROOT / "tests" / "e2e" / "core" / "windows_update"])
    held = _slice_files(files, index, count, {}, ROOT)
    assert "test_crash_cells.py" in {f.name for f in held}


def test_timing_bound_guards_leave_the_sliced_suite_for_one_single_worker_job():
    """The sliced suite deselects tests/timing_bound.txt; one job runs exactly those tests with
    one worker, so a wall-clock bound never shares the runner's cores with another test file."""
    mod = _load()
    out = _rewrite(SLICED)
    jobs = _yaml(out)["jobs"]
    sliced = jobs["test"]["steps"][0]
    assert sliced["run"] == "scripts/run_tests.sh -- --timing-bound=exclude"
    assert f"# {mod.TIMEOUT_MARK} just test-all" in out
    lone = jobs["test-timing-bound"]
    assert lone["runs-on"] == "ubuntu-latest"
    assert "strategy" not in lone and "matrix.slice" not in str(lone)
    step = lone["steps"][0]
    assert step["run"] == ("scripts/run_tests.sh $(grep -o '^tests/[^:]*' tests/timing_bound.txt"
                           " | sort -u) -- --timing-bound=only")
    assert step["env"]["HERMES_TEST_WORKERS"] == 1
    assert step["env"]["KEEP"] == 3
    assert "HERMES_TEST_SLICE" not in step["env"]
    assert list(jobs) == ["test", "test-timing-bound"]
    assert _rewrite(out) == out


def test_the_timing_bound_job_drops_steps_keyed_by_slice():
    """A step that names its slice (the per-slice duration cache key) has no slice to name."""
    job = SLICED + """\
      - name: Save per-file duration cache (main only)
        uses: actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0
        with:
          key: test-durations-${{ github.run_id }}-${{ matrix.slice }}
      - name: Check the checkout is clean
        uses: ./.github/actions/check-clean-worktree
"""
    lone = _yaml(_rewrite(job))["jobs"]["test-timing-bound"]
    assert [s["name"] for s in lone["steps"]] == ["Run tests", "Check the checkout is clean"]


def test_timing_bound_guards_stay_in_a_suite_on_a_small_runner():
    out = _rewrite(SLICED.replace("blacksmith-32vcpu", "blacksmith-4vcpu"))
    assert "timing-bound" not in out
    assert "run: just test-all\n" in out


def test_the_committed_timing_bound_job_runs_every_listed_file(tmp_path):
    """The job's file selection, run as the runner's shell runs it, is the list's set of files."""
    sys.path.insert(0, str(ROOT))
    from tests._fixtures import timing_bound

    tests = _yaml((ROOT / ".github" / "workflows" / "tests.yml").read_text())
    step = next(s for s in tests["jobs"]["test-timing-bound"]["steps"] if s.get("name") == "Run tests")
    assert step["env"]["HERMES_TEST_WORKERS"] == 1
    pick = re.search(r"\$\((.*)\)", step["run"])
    assert pick and step["run"].endswith("-- --timing-bound=only")
    ran = subprocess.run(["bash", "-c", pick[1]], cwd=ROOT, capture_output=True, text=True, check=True)
    assert ran.stdout.split() == sorted({i.split("::", 1)[0] for i in timing_bound.listed()})
    run = next(s for s in tests["jobs"]["test"]["steps"] if s.get("name") == "Run tests")["run"]
    assert run == "scripts/run_tests.sh -- --timing-bound=exclude"


def test_the_committed_tree_is_the_rewrite_of_upstreams_workflows(tmp_path):
    """--check over the committed tree is clean, so a resync regenerates the same files."""
    ran = subprocess.run([sys.executable, str(SCRIPT), "--root", str(ROOT), "--check"],
                         capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    tests = _yaml((ROOT / ".github" / "workflows" / "tests.yml").read_text())
    env = next(s for s in tests["jobs"]["test"]["steps"] if s.get("name") == "Run tests")["env"]
    assert env["HERMES_TEST_WORKERS"] == 4
    assert len(tests["jobs"]["test"]["strategy"]["matrix"]["slice"]) == 24
