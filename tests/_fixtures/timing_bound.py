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

import ast
import re
from pathlib import Path

import pytest

MARK = "timing_bound"
LIST = Path(__file__).resolve().parents[1] / "timing_bound.txt"
MODES = ("all", "exclude", "only")


def listed(path: Path = LIST) -> frozenset[str]:
    """Node ids in the list, without parametrize ids; ``#`` starts a comment."""
    ids = (line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines())
    return frozenset(i for i in ids if i)


_RULE_NAME = re.compile(r"linear|quadratic|backtrack|redos|fast", re.IGNORECASE)
_RULE_CLOCKS = ("perf_counter", "monotonic")
# The sliced suite's discovery skips these parts (scripts/run_tests_parallel.py _SKIP_PARTS); their
# tests run in their own workflows, which never pass --timing-bound.
_SUITE_SKIP_PARTS = {"integration", "e2e", "docker"}


def rule_matches(source: str, relpath: str) -> list[str]:
    """Node ids (without parametrize ids) of the tests in ``source`` whose name says linear,
    quadratic, backtrack, ReDoS or fast and whose body reads ``perf_counter``/``monotonic``:
    a wall-clock bound that CPU contention on a small runner can trip."""
    found: list[str] = []

    def visit(node: ast.AST, prefix: list[str]) -> None:
        for child in getattr(node, "body", []):
            if isinstance(child, ast.ClassDef):
                visit(child, [*prefix, child.name])
            elif (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and child.name.startswith("test") and _RULE_NAME.search(child.name)
                  and any(clock in ast.unparse(child) for clock in _RULE_CLOCKS)):
                found.append("::".join([relpath, *prefix, child.name]))

    visit(ast.parse(source), [])
    return found


def rule_matches_under(root: Path) -> list[str]:
    """``rule_matches`` over every test file the sliced suite discovers under ``root/tests``."""
    found: list[str] = []
    for path in sorted((root / "tests").rglob("test_*.py")):
        rel = path.relative_to(root)
        if _SUITE_SKIP_PARTS.intersection(rel.parts):
            continue
        found += rule_matches(path.read_text(encoding="utf-8"), rel.as_posix())
    return found


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


def mode(config: pytest.Config) -> str:
    """The run's ``--timing-bound`` mode; ``all`` where only the collection hook was borrowed
    (a test's own conftest re-exporting it without ``pytest_addoption``)."""
    return config.getoption("--timing-bound", default="all")


def modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    ids = listed()
    for item in items:
        if is_listed(item.nodeid, ids):
            item.add_marker(MARK)
    kept, dropped = partition([i.nodeid for i in items], ids, mode(config))
    if dropped:
        keep = set(kept)
        config.hook.pytest_deselected(items=[i for i in items if i.nodeid not in keep])
        items[:] = [i for i in items if i.nodeid in keep]
