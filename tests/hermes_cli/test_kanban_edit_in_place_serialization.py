"""Regression tests: never run two edit-in-place cards on one workspace root.

Edit-in-place cards (``workspace_kind='dir'``) share a single directory — the
canonical case is the ``~/.hermes`` deploy tree. Two such cards running at once
race on the same files: one worker's full-file write silently erases the other's
edits (incident 2026-09-26, t_2c030a11 vs t_3d3af791 both editing
``onecard_common.py``). Fork-core (worktree) and scratch cards get per-task
isolation, so they never collide; ``dir`` cards resolved **in place** do.

The dispatcher serializes in-place ``dir`` cards per resolved workspace root:
at most one running per root. The second waits in ``ready`` and is claimed only
after the first leaves ``running``. Cards on *different* roots, and isolated
(scratch / worktree) cards sharing an anchor, still run in parallel.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Fresh HERMES_HOME with a kanban DB + a spawnable ``worker`` profile."""
    test_home = tempfile.mkdtemp(prefix="kanban_eip_serialize_test_")
    for prof in ("worker", "other", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
            or mod == "hermes_cli.edit_in_place_repos"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db, test_home


def _fake_spawn(*args, **kwargs):
    return 12345


def _mkdir(base: str, name: str) -> str:
    p = os.path.join(base, name)
    os.makedirs(p, exist_ok=True)
    return p


def test_two_dir_cards_same_root_serialize(isolated_kanban_home):
    """Two ready ``dir`` cards on the SAME directory root: only one spawns this
    tick; the other is deferred (stays claimable next tick)."""
    kb, home = isolated_kanban_home
    root = _mkdir(home, "shared_root")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(
            conn, detached=True, title="a", assignee="worker",
            workspace_kind="dir", workspace_path=root,
        )
        kb.create_task(
            conn, detached=True, title="b", assignee="worker",
            workspace_kind="dir", workspace_path=root,
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res.spawned) == 1
    # The deferred card is reported, not silently dropped.
    deferred_ids = [tid for (tid, _root) in res.skipped_workspace_busy]
    assert len(deferred_ids) == 1


def test_second_card_claimed_after_first_leaves_running(isolated_kanban_home):
    """The deferred card is claimed on a later tick once the first card leaves
    ``running`` — the serialization is per-tick state, not a permanent block."""
    kb, home = isolated_kanban_home
    root = _mkdir(home, "shared_root")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(
            conn, detached=True, title="a", assignee="worker",
            workspace_kind="dir", workspace_path=root,
        )
        kb.create_task(
            conn, detached=True, title="b", assignee="worker",
            workspace_kind="dir", workspace_path=root,
        )
    # Tick 1: one spawns, one deferred.
    with kb.connect_closing() as conn:
        res1 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res1.spawned) == 1
    assert len(res1.skipped_workspace_busy) == 1
    first_id = res1.spawned[0][0]
    # First card completes → leaves 'running'.
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', claim_lock = NULL WHERE id = ?",
                (first_id,),
            )
    # Tick 2: the deferred card now spawns.
    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res2.spawned) == 1
    assert res2.spawned[0][0] != first_id
    assert not res2.skipped_workspace_busy


def test_pre_running_card_blocks_same_root(isolated_kanban_home):
    """A card already 'running' on a root defers a ready ``dir`` card that
    resolves to that same root."""
    kb, home = isolated_kanban_home
    root = _mkdir(home, "shared_root")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        running = kb.create_task(
            conn, detached=True, title="running", assignee="worker",
            workspace_kind="dir", workspace_path=root,
        )
        with kb.write_txn(conn):
            # Mark it running AND persist the resolved workspace, exactly as the
            # dispatcher would after claim + resolve.
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = 'test:1', "
                "workspace_path = ? WHERE id = ?",
                (root, running),
            )
        kb.create_task(
            conn, detached=True, title="ready", assignee="worker",
            workspace_kind="dir", workspace_path=root,
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res.spawned) == 0
    assert len(res.skipped_workspace_busy) == 1


def test_different_roots_run_in_parallel(isolated_kanban_home):
    """Control: two ``dir`` cards on DIFFERENT roots both spawn."""
    kb, home = isolated_kanban_home
    root_a = _mkdir(home, "root_a")
    root_b = _mkdir(home, "root_b")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(
            conn, detached=True, title="a", assignee="worker",
            workspace_kind="dir", workspace_path=root_a,
        )
        kb.create_task(
            conn, detached=True, title="b", assignee="worker",
            workspace_kind="dir", workspace_path=root_b,
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res.spawned) == 2
    assert not res.skipped_workspace_busy


def test_scratch_cards_never_serialize(isolated_kanban_home):
    """Scratch cards get a per-task directory, so two of them never collide —
    even though they share the same board workspaces root anchor."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(3):
            kb.create_task(conn, detached=True, title=f"s{i}", assignee="worker")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res.spawned) == 3
    assert not res.skipped_workspace_busy


def test_edit_in_place_root_serializes(isolated_kanban_home):
    """Two ``dir`` cards on the DECLARED edit-in-place root (the HERMES_HOME
    deploy tree) serialize — this is the exact ~/.hermes incident."""
    kb, home = isolated_kanban_home
    from hermes_cli.edit_in_place_repos import is_edit_in_place_root

    # Sanity: the temp HERMES_HOME is classified edit-in-place.
    assert is_edit_in_place_root(home)
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(
            conn, detached=True, title="eip-a", assignee="worker",
            workspace_kind="dir", workspace_path=home,
        )
        kb.create_task(
            conn, detached=True, title="eip-b", assignee="worker",
            workspace_kind="dir", workspace_path=home,
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res.spawned) == 1
    assert len(res.skipped_workspace_busy) == 1


def test_dry_run_reports_serialization(isolated_kanban_home):
    """In dry_run, the serialization decision is still reflected: only one of
    two same-root cards is reported spawnable, the other deferred."""
    kb, home = isolated_kanban_home
    root = _mkdir(home, "shared_root")
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(
            conn, detached=True, title="a", assignee="worker",
            workspace_kind="dir", workspace_path=root,
        )
        kb.create_task(
            conn, detached=True, title="b", assignee="worker",
            workspace_kind="dir", workspace_path=root,
        )
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    assert len(res.spawned) == 1
    assert len(res.skipped_workspace_busy) == 1


def test_dispatch_result_has_skipped_workspace_busy_field():
    """Schema invariant: DispatchResult exposes skipped_workspace_busy as a list
    of (task_id, workspace_root) tuples."""
    # Import without the isolated fixture: this only touches the dataclass.
    from hermes_cli.kanban_db import DispatchResult
    r = DispatchResult()
    assert hasattr(r, "skipped_workspace_busy")
    assert r.skipped_workspace_busy == []
