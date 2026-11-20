"""tests/timing_bound.txt: the wall-clock guards a 4-vCPU CI runner runs apart from the suite.

The sliced suite runs with ``--timing-bound=exclude`` and one single-worker job runs the listed
files with ``--timing-bound=only`` (scripts/ci/fork_runner_sizing.py). Between them every test
runs exactly once: none twice, none dropped.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._fixtures import timing_bound

ROOT = Path(__file__).resolve().parents[2]


def test_the_two_modes_partition_any_collection():
    ids = frozenset({"tests/a.py::test_linear", "tests/b.py::C::test_fast"})
    nodeids = ["tests/a.py::test_linear", "tests/a.py::test_other", "tests/b.py::C::test_fast[x]",
               "tests/b.py::C::test_fast[y]", "tests/b.py::C::test_slow"]
    only, _ = timing_bound.partition(nodeids, ids, "only")
    exclude, _ = timing_bound.partition(nodeids, ids, "exclude")
    assert only == ["tests/a.py::test_linear", "tests/b.py::C::test_fast[x]", "tests/b.py::C::test_fast[y]"]
    assert set(only).isdisjoint(exclude)
    assert sorted(only + exclude) == sorted(nodeids)
    assert timing_bound.partition(nodeids, ids, "all") == (nodeids, [])


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        timing_bound.partition([], frozenset(), "some")


def test_the_list_reads_ids_and_ignores_comments(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("# header\n\ntests/a.py::test_x  # why\ntests/b.py::C::test_y\n", encoding="utf-8")
    assert timing_bound.listed(path) == {"tests/a.py::test_x", "tests/b.py::C::test_y"}


def test_the_rule_matches_a_named_wall_clock_bound_only():
    src = (
        "import time\n"
        "def test_scan_is_linear():\n    t = time.perf_counter()\n"
        "class TestX:\n    def test_fails_fast(self):\n        time.monotonic()\n"
        "def test_fast_but_untimed():\n    pass\n"
        "def test_timed_but_unnamed():\n    time.perf_counter()\n"
    )
    assert timing_bound.rule_matches(src, "tests/a.py") == ["tests/a.py::test_scan_is_linear",
                                                           "tests/a.py::TestX::test_fails_fast"]


def test_every_test_the_rule_matches_is_listed():
    """The list holds every test in the sliced suite's tree that ``rule_matches`` finds, so a new
    wall-clock guard cannot land in the parallel lanes unlisted."""
    found = timing_bound.rule_matches_under(ROOT)
    assert sorted(set(found) - timing_bound.listed()) == []


def _collect(files: list[str], *mode: str) -> list[str]:
    ran = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *mode, *files],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert ran.returncode == 0, ran.stdout[-4000:] + ran.stderr[-4000:]
    return [ln for ln in ran.stdout.splitlines() if "::" in ln and not ln.startswith(" ")]


def test_every_listed_test_runs_once_across_the_two_lanes():
    """Collected over the listed files, ``only`` is exactly the list and ``exclude`` is the rest of
    those files: an entry that names no test (a typo, a rename) fails here."""
    ids = timing_bound.listed()
    files = sorted({i.split("::", 1)[0] for i in ids})
    everything = _collect(files)
    only = _collect(files, "--timing-bound=only")
    exclude = _collect(files, "--timing-bound=exclude")
    assert set(only).isdisjoint(exclude)
    assert sorted(only + exclude) == sorted(everything)
    assert {n.split("[", 1)[0] for n in only} == ids
