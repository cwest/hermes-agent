"""A deterministic workspace-FILING error at dispatch must not burn the retry budget.

Companion to ``test_kanban_edit_in_place_workspace.py``. That file proves the
dispatch-time GUARD is correct: a ``worktree`` card aimed at an edit-in-place
repo root (``~/.hermes`` — the live install) is REFUSED at workspace resolution,
because such a checkout is the deploy target itself and must be edited in place.

This file proves the *classification* of that refusal. The refusal is a
DETERMINISTIC caller/filing error — attempt N+1 is guaranteed to fail identically,
so it can never succeed on retry. The historic defect (card t_c9c6432b): the two
spawn-failure ``except`` blocks in ``dispatch_once`` funnelled EVERY
workspace-resolution error through ``_record_spawn_failure`` →
``_record_task_failure``, which increments ``consecutive_failures`` and, at the
``DEFAULT_FAILURE_LIMIT``, emits a ``gave_up`` event. The gateway notifier
delivers ``gave_up`` to the origin thread as a false "retries exhausted" alarm.
So a correctly-refused misfiled card produced two burned retries plus a spurious
failure alert, even though the work itself was fine once re-filed as ``dir``.

The contract asserted here:

  * a ``WorkspaceFilingError`` at dispatch does NOT increment
    ``consecutive_failures`` (it never consumes the retry budget), and
  * it emits NO ``gave_up`` event (no false "retries exhausted" alarm), and
  * it surfaces ONCE as a ``blocked`` card with a distinct ``filing-error``
    reason naming the fix (so a human sees the misfiling clearly, exactly once),
  * and this holds no matter how many dispatch ticks hit it — a deterministic
    filing error can never trip the circuit breaker.

The dispatch-time GUARD itself is unchanged and still refuses — see the sibling
file. This file only changes what the dispatcher DOES with that refusal.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """A temp HERMES_HOME that is ALSO a git repo root — the edit-in-place case."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "core.excludesfile=/dev/null",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _make_repo_at(path: Path, name_hint: str = "origin") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    bare = path.parent / f"{name_hint}-origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(path, "init", "-b", "main")
    _git(path, "remote", "add", "origin", str(bare))
    (path / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    _git(path, "add", "config.yaml")
    _git(path, "commit", "-m", "init")
    _git(path, "push", "-u", "origin", "main")
    return path


def _file_worktree_at_edit_in_place(conn, kanban_home) -> str:
    """File a ``worktree`` card aimed at the edit-in-place root — the bad shape.

    This is the exact illegal combination the dispatch guard refuses. We set the
    row directly (rather than through the filing gate, whose whole job is to make
    this un-filable) so the DISPATCH behavior can be exercised in isolation, the
    way the historic bad row reached the dispatcher.
    """
    tid = kb.create_task(
        conn,
        title="fix(x): a card wrongly aimed at the live install",
        assignee="easley",
        workspace_kind="worktree",
        workspace_path=str(kanban_home),
        detached=True,
    )
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    conn.commit()
    return tid


# --------------------------------------------------------------------------
# The exception class — a type-based signal, not brittle string matching.
# --------------------------------------------------------------------------


def test_worktree_on_edit_in_place_raises_filing_error_subclass(kanban_home):
    """The edit-in-place worktree refusal is a ``WorkspaceFilingError``.

    A dedicated class (a subclass of ``ValueError``, so every existing
    ``except ValueError`` / ``except Exception`` caller is unaffected) lets the
    dispatcher classify the refusal by TYPE rather than by matching its message.
    """
    from hermes_cli.edit_in_place_repos import WorkspaceFilingError

    _make_repo_at(kanban_home, "hermes-config")
    with kb.connect() as conn:
        tid = _file_worktree_at_edit_in_place(conn, kanban_home)
        task = kb.get_task(conn, tid)

    with pytest.raises(WorkspaceFilingError, match="edit-in-place"):
        kb.resolve_workspace(task)
    # Still a ValueError for legacy callers.
    assert issubclass(WorkspaceFilingError, ValueError)


# --------------------------------------------------------------------------
# The core contract: the refusal must NOT consume a retry or emit gave_up.
# --------------------------------------------------------------------------


def test_dispatch_filing_error_does_not_consume_retry_or_gave_up(
    kanban_home, monkeypatch
):
    """A dispatch-time filing refusal: no counter bump, no gave_up, blocked once."""
    _make_repo_at(kanban_home, "hermes-config")

    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)

    def worker_spawn(task, workspace):  # pragma: no cover - never reached
        raise AssertionError("spawn must not be called for a refused workspace")

    with kb.connect() as conn:
        tid = _file_worktree_at_edit_in_place(conn, kanban_home)

        kb.dispatch_once(conn, spawn_fn=worker_spawn, max_spawn=1)

        task_after = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    kinds = [e.kind for e in events]

    # The deterministic filing error never counts as a transient failure.
    assert task_after.consecutive_failures == 0, (
        f"filing error consumed the retry budget: "
        f"consecutive_failures={task_after.consecutive_failures}"
    )
    # No false "retries exhausted" alarm.
    assert "gave_up" not in kinds, f"unexpected gave_up event; kinds={kinds}"
    # It surfaces once as a blocked card with a distinct filing-error reason.
    assert task_after.status == "blocked", (
        f"expected the misfiled card to be blocked, got {task_after.status!r}"
    )
    blocked_events = [e for e in events if e.kind == "blocked"]
    assert len(blocked_events) == 1, (
        f"expected exactly one blocked event; kinds={kinds}"
    )
    reason = (blocked_events[0].payload or {}).get("reason", "")
    assert "filing" in reason.lower(), (
        f"block reason must name the filing error: {reason!r}"
    )
    # The fix is named the way the dispatch guard names it.
    assert "workspace_kind='dir'" in reason, (
        f"block reason must name the correct filing (dir): {reason!r}"
    )


def test_repeated_dispatch_of_filing_error_never_trips_breaker(
    kanban_home, monkeypatch
):
    """Even across many ticks a deterministic filing error can't ``gave_up``.

    ``DEFAULT_FAILURE_LIMIT`` is small (2). If the refusal counted as a failure,
    the second tick would trip the breaker and emit ``gave_up``. It must not —
    the card stays a cleanly-blocked filing error no matter how often it is seen.
    """
    _make_repo_at(kanban_home, "hermes-config")

    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)

    def worker_spawn(task, workspace):  # pragma: no cover - never reached
        raise AssertionError("spawn must not be called for a refused workspace")

    with kb.connect() as conn:
        tid = _file_worktree_at_edit_in_place(conn, kanban_home)

        # Re-arm and re-dispatch several times, mimicking successive ticks where
        # a human/automation unblocks the still-misfiled card.
        for _ in range(4):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            conn.commit()
            kb.dispatch_once(conn, spawn_fn=worker_spawn, max_spawn=1)

        task_after = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task_after.consecutive_failures == 0
    assert "gave_up" not in [e.kind for e in events]


# --------------------------------------------------------------------------
# Regression fence: an ORDINARY (transient) spawn failure STILL burns retries
# and STILL gives up. The carve-out must be narrow to the filing class only.
# --------------------------------------------------------------------------


def test_ordinary_spawn_failure_still_consumes_retries_and_gives_up(
    kanban_home, monkeypatch
):
    """A non-filing spawn error keeps the existing retry + gave_up behavior."""
    repo = _make_repo_at(kanban_home / "sub" / "office", "office")

    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)

    calls = {"n": 0}

    def flaky_spawn(task, workspace):
        calls["n"] += 1
        raise RuntimeError("boom: a genuine transient spawn failure")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="feat(office): an ordinary card that fails to spawn",
            assignee="easley",
            workspace_kind="dir",
            workspace_path=str(repo),
            detached=True,
        )
        # DEFAULT_FAILURE_LIMIT dispatch ticks should trip the breaker.
        for _ in range(kb.DEFAULT_FAILURE_LIMIT):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            conn.commit()
            kb.dispatch_once(conn, spawn_fn=flaky_spawn, max_spawn=1)

        task_after = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    # An ordinary spawn failure DID consume the budget and DID give up.
    assert calls["n"] >= 1
    assert "gave_up" in [e.kind for e in events], (
        "an ordinary transient spawn failure must still trip the breaker"
    )
