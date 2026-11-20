"""The wall-clock guards that a small CI runner runs apart from the rest of the suite.

``tests/timing_bound.txt`` lists the tests that assert a scan or format finishes inside a
wall-clock bound (the linear-time and ReDoS guards). Upstream runs them among 32 workers on
32 cores. On a 4-vCPU hosted runner they share each core with other test files, and a 2 s bound
fails on CPU contention rather than on the scan, so the fork's CI runs them in a single-worker
job of their own (``scripts/ci/fork_runner_sizing.py``). No bound is changed.

``--timing-bound=exclude`` deselects the listed tests and ``--timing-bound=only`` keeps only
them. Both modes use one predicate, so across the two runs every test runs exactly once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

MARK = "timing_bound"
LIST = Path(__file__).resolve().parents[1] / "timing_bound.txt"
MODES = ("all", "exclude", "only")


def listed(path: Path = LIST) -> frozenset[str]:
    """Node ids in the list, without parametrize ids; ``#`` starts a comment."""
    ids = (line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines())
    return frozenset(i for i in ids if i)


def is_listed(nodeid: str, ids: frozenset[str]) -> bool:
    return nodeid.split("[", 1)[0] in ids


def partition(nodeids: list[str], ids: frozenset[str], mode: str) -> tuple[list[str], list[str]]:
    """(kept, deselected) node ids for ``mode``."""
    if mode not in MODES:
        raise ValueError(f"--timing-bound must be one of {', '.join(MODES)}, got {mode!r}")
    if mode == "all":
        return list(nodeids), []
    want = mode == "only"
    kept = [n for n in nodeids if is_listed(n, ids) == want]
    return kept, [n for n in nodeids if is_listed(n, ids) != want]


def addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--timing-bound", choices=MODES, default="all",
        help="exclude or keep only the wall-clock guards listed in tests/timing_bound.txt",
    )


def configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", f"{MARK}: a wall-clock guard listed in tests/timing_bound.txt")


def modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    ids = listed()
    for item in items:
        if is_listed(item.nodeid, ids):
            item.add_marker(MARK)
    kept, dropped = partition([i.nodeid for i in items], ids, config.getoption("--timing-bound"))
    if dropped:
        keep = set(kept)
        config.hook.pytest_deselected(items=[i for i in items if i.nodeid not in keep])
        items[:] = [i for i in items if i.nodeid in keep]
