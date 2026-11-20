"""The routing replay runs a lane's selection step once per distinct lane, not once per slice.

The fork's runner sizing splits each OS lane into slices (``slice: i/n``, a name per slice).
The selection step reads neither, so slice cells select the same files. Replaying every one
multiplied tests/ci/test_update_ci_routing.py's runtime past its file cap on hosted runners.
"""

from tests.ci import test_update_ci_routing as routing

STEP = "Run ${{ matrix.marker }} tests"


def _node(run: str, cells: list[dict]) -> dict:
    return {
        "body": {"runs-on": "${{ matrix.runner }}", "strategy": {"matrix": {"include": cells}},
                 "steps": [{"name": STEP, "run": run, "env": {"HERMES_TEST_SLICE": "${{ matrix.slice }}"}}]},
        "ctx": {"inputs": {}, "github": {}},
    }


def _sliced(marker: str, runner: str, n: int) -> list[dict]:
    return [{"name": f"{marker} ({i}/{n})", "runner": runner, "marker": marker, "slice": f"{i}/{n}"}
            for i in range(1, n + 1)]


def _replayed(monkeypatch, node: dict, **kw) -> list[dict]:
    seen = []
    monkeypatch.setattr(routing.workflow_steps, "required", lambda body, ctx: None)

    def selected(step, ctx, repo):
        seen.append(ctx["matrix"])
        return {f"tests/{ctx['matrix']['marker']}.py"}

    monkeypatch.setattr(routing.workflow_steps, "selected_files", selected)
    assert routing._selected_test_files(node, STEP, **kw)
    return seen


def test_slice_cells_of_one_lane_replay_once(monkeypatch):
    cells = _sliced("macos", "macos-latest", 8) + _sliced("windows", "windows-latest", 8) \
        + _sliced("windows", "windows-11-arm", 16)
    seen = _replayed(monkeypatch, _node("scripts/run_tests.sh --files x", cells))
    assert [(c["marker"], c["runner"]) for c in seen] == [
        ("macos", "macos-latest"), ("windows", "windows-latest"), ("windows", "windows-11-arm")]
    windows = _replayed(monkeypatch, _node("scripts/run_tests.sh --files x", cells), windows_only=True)
    assert [c["runner"] for c in windows] == ["windows-latest", "windows-11-arm"]


def test_a_step_that_reads_its_slice_replays_every_cell(monkeypatch):
    cells = _sliced("macos", "macos-latest", 4)
    seen = _replayed(monkeypatch, _node("scripts/run_tests.sh --slice ${{ matrix.slice }}", cells))
    assert [c["slice"] for c in seen] == ["1/4", "2/4", "3/4", "4/4"]
