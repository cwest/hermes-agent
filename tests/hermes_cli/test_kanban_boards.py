"""Tests for the multi-board kanban layer (``hermes kanban boards …``).

Covers the pieces added when boards became a first-class concept:

* Slug validation and normalisation.
* Path resolution for ``default`` (legacy ``<root>/kanban.db``) vs
  named boards (``<root>/kanban/boards/<slug>/kanban.db``).
* Current-board persistence via ``<root>/kanban/current`` and
  ``HERMES_KANBAN_BOARD`` env var.
* ``connect(board=)`` isolation — writes on one board don't leak.
* ``create_board`` / ``list_boards`` / ``remove_board`` round trip.
* CLI surface: ``hermes kanban boards list/create/switch/rm``.
* ``_default_spawn`` injects ``HERMES_KANBAN_BOARD`` into worker env.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure the worktree (not the stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with no prior kanban state.

    The autouse hermetic conftest already nukes credentials + TZ; this
    fixture layers a per-test HERMES_HOME plus a path-init cache reset
    so each test sees a truly empty board set.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    # Also reset hermes_constants cache so get_default_hermes_root() re-reads.
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    # Kanban module-level init cache must not leak between tests.
    kb._INITIALIZED_PATHS.clear()
    return home


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

class TestSlugValidation:
    @pytest.mark.parametrize("good", [
        "default", "atm10-server", "hermes-agent", "proj_1", "a",
        "very-long-but-still-ok-slug-with-hyphens-and-numbers-1234",
    ])
    def test_accepts_valid(self, good):
        assert kb._normalize_board_slug(good) == good

    @pytest.mark.parametrize("bad", [
        "-leading-hyphen", "_leading_underscore",
        "with/slash", "with space",
        "has.dot", "has?question",
        "..", "../etc", "foo\x00bar",
    ])
    def test_rejects_invalid(self, bad):
        with pytest.raises(ValueError):
            kb._normalize_board_slug(bad)

    def test_empty_returns_none(self):
        assert kb._normalize_board_slug(None) is None
        assert kb._normalize_board_slug("") is None
        assert kb._normalize_board_slug("   ") is None

    def test_auto_lowercases(self):
        # Uppercase is auto-downcased (friendlier than rejecting). ``Default``
        # → ``default``, ``ATM10`` → ``atm10``. The on-disk slug is always
        # lowercase regardless of what the user typed.
        assert kb._normalize_board_slug("Default") == "default"
        assert kb._normalize_board_slug("ATM10-Server") == "atm10-server"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

class TestPathResolution:
    def test_default_board_legacy_path(self, fresh_home):
        """The default board's DB lives at ``<root>/kanban.db`` for back-compat."""
        assert kb.kanban_db_path() == fresh_home / "kanban.db"
        assert kb.kanban_db_path(board="default") == fresh_home / "kanban.db"

    def test_named_board_under_boards_dir(self, fresh_home):
        p = kb.kanban_db_path(board="atm10-server")
        assert p == fresh_home / "kanban" / "boards" / "atm10-server" / "kanban.db"

    def test_workspaces_per_board(self, fresh_home):
        assert kb.workspaces_root() == fresh_home / "kanban" / "workspaces"
        # Uppercase input gets auto-downcased to the on-disk slug.
        assert kb.workspaces_root(board="projA") == (
            fresh_home / "kanban" / "boards" / "proja" / "workspaces"
        )

    def test_logs_per_board(self, fresh_home):
        assert kb.worker_logs_dir() == fresh_home / "kanban" / "logs"
        assert kb.worker_logs_dir(board="other") == (
            fresh_home / "kanban" / "boards" / "other" / "logs"
        )

    def test_env_var_db_override_still_wins(self, fresh_home, tmp_path, monkeypatch):
        """``HERMES_KANBAN_DB`` pins the file regardless of board= arg."""
        forced = tmp_path / "custom.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
        assert kb.kanban_db_path() == forced
        assert kb.kanban_db_path(board="ignored") == forced

    def test_env_var_workspaces_override(self, fresh_home, tmp_path, monkeypatch):
        forced = tmp_path / "ws"
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(forced))
        assert kb.workspaces_root(board="any") == forced


# ---------------------------------------------------------------------------
# Current-board resolution
# ---------------------------------------------------------------------------

class TestCurrentBoard:
    def test_default_when_unset(self, fresh_home):
        assert kb.get_current_board() == "default"

    def test_env_var_takes_precedence(self, fresh_home, monkeypatch):
        # Create the board so the env-var value is honoured (get_current_board
        # trusts env-var validity, but the resolution chain doesn't require
        # the board to exist; we just test that env trumps).
        kb.create_board("envboard")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "envboard")
        assert kb.get_current_board() == "envboard"

    def test_file_pointer_honoured(self, fresh_home):
        kb.create_board("filepick")
        kb.set_current_board("filepick")
        assert kb.get_current_board() == "filepick"

    def test_stale_file_pointer_falls_back_to_default(self, fresh_home):
        current = fresh_home / "kanban" / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("missing-board\n", encoding="utf-8")

        assert kb.get_current_board() == "default"
        assert not kb.board_exists("missing-board")
        assert [b["slug"] for b in kb.list_boards()] == ["default"]

    def test_empty_board_dir_does_not_count_as_existing(self, fresh_home):
        ghost = fresh_home / "kanban" / "boards" / "ghost"
        ghost.mkdir(parents=True)

        assert not kb.board_exists("ghost")
        assert [b["slug"] for b in kb.list_boards()] == ["default"]

    def test_env_beats_file(self, fresh_home, monkeypatch):
        kb.create_board("a")
        kb.create_board("b")
        kb.set_current_board("a")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "b")
        assert kb.get_current_board() == "b"

    def test_stale_env_falls_through_to_file_pointer(self, fresh_home, monkeypatch):
        kb.create_board("persisted")
        kb.set_current_board("persisted")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "missing-board")
        assert kb.get_current_board() == "persisted"

    def test_invalid_env_falls_through(self, fresh_home, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "!!bad!!")
        # Should not crash — falls through to default.
        assert kb.get_current_board() == "default"

    def test_clear_current_board(self, fresh_home):
        kb.create_board("x")
        kb.set_current_board("x")
        kb.clear_current_board()
        assert kb.get_current_board() == "default"

    def test_kanban_db_path_reads_current(self, fresh_home):
        """kanban_db_path() with no args respects the on-disk pointer."""
        kb.create_board("my-proj")
        kb.set_current_board("my-proj")
        expected = fresh_home / "kanban" / "boards" / "my-proj" / "kanban.db"
        assert kb.kanban_db_path() == expected


# ---------------------------------------------------------------------------
# Board CRUD
# ---------------------------------------------------------------------------

class TestBoardCRUD:
    def test_create_and_list(self, fresh_home):
        assert [b["slug"] for b in kb.list_boards()] == ["default"]
        kb.create_board("foo", name="Foo Board", description="test")
        slugs = [b["slug"] for b in kb.list_boards()]
        assert slugs == ["default", "foo"]

    def test_create_is_idempotent(self, fresh_home):
        kb.create_board("bar")
        kb.create_board("bar")  # no error
        slugs = [b["slug"] for b in kb.list_boards()]
        assert slugs == ["default", "bar"]

    def test_create_writes_metadata(self, fresh_home):
        meta = kb.create_board(
            "baz",
            name="Baz",
            description="desc",
            icon="📦",
            color="#abcdef",
        )
        assert meta["slug"] == "baz"
        assert meta["name"] == "Baz"
        assert meta["icon"] == "📦"
        # Round-trip via read_board_metadata.
        again = kb.read_board_metadata("baz")
        assert again["name"] == "Baz"
        assert again["description"] == "desc"
        assert again["icon"] == "📦"

    def test_remove_archive(self, fresh_home):
        kb.create_board("toremove")
        res = kb.remove_board("toremove")
        assert res["action"] == "archived"
        assert Path(res["new_path"]).exists()
        assert "toremove" not in [b["slug"] for b in kb.list_boards()]

    def test_remove_hard_delete(self, fresh_home):
        kb.create_board("nuke")
        d = kb.board_dir("nuke")
        assert d.exists()
        res = kb.remove_board("nuke", archive=False)
        assert res["action"] == "deleted"
        assert not d.exists()

    def test_remove_default_forbidden(self, fresh_home):
        with pytest.raises(ValueError, match="default"):
            kb.remove_board("default")

    def test_remove_nonexistent_raises(self, fresh_home):
        with pytest.raises(ValueError, match="does not exist"):
            kb.remove_board("nosuch")

    def test_remove_clears_current_pointer(self, fresh_home):
        kb.create_board("pinned")
        kb.set_current_board("pinned")
        kb.remove_board("pinned")
        assert kb.get_current_board() == "default"

    @pytest.mark.parametrize("archive", [True, False])
    def test_remove_clears_init_cache_for_recreated_db(self, fresh_home, archive):
        # Regression for #23833: poll loops that call connect(board=slug) right
        # after remove_board() recreate an empty kanban.db at the same path
        # (connect() does mkdir(exist_ok=True)). If _INITIALIZED_PATHS still
        # contains the resolved path, the CREATE TABLE pass is skipped and
        # downstream readers hit `no such table: task_events`.
        kb.create_board("recycle")
        # First connect populates _INITIALIZED_PATHS for this DB.
        with kb.connect(board="recycle") as conn:
            kb.create_task(conn, title="t1", assignee="dev")
        db_path = kb.board_dir("recycle") / "kanban.db"
        assert str(db_path.resolve()) in kb._INITIALIZED_PATHS

        kb.remove_board("recycle", archive=archive)
        # remove_board must drop the cache entry so a re-create through
        # connect() gets a fresh schema-init pass.
        assert str(db_path.resolve()) not in kb._INITIALIZED_PATHS

        # Simulate the event-stream poll: re-open the same slug. connect()
        # recreates the directory + empty .db; the schema must be re-applied.
        with kb.connect(board="recycle") as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "task_events" in tables
        assert "tasks" in tables

    def test_rename_updates_metadata(self, fresh_home):
        kb.create_board("slug-immutable")
        kb.write_board_metadata("slug-immutable", name="New Display Name")
        assert kb.read_board_metadata("slug-immutable")["name"] == "New Display Name"
        # Slug must not change.
        assert kb.board_exists("slug-immutable")


# ---------------------------------------------------------------------------
# Connection isolation
# ---------------------------------------------------------------------------

class TestConnectionIsolation:
    def test_tasks_do_not_leak_across_boards(self, fresh_home):
        kb.create_board("alpha")
        kb.create_board("beta")

        with kb.connect(board="alpha") as conn:
            kb.create_task(conn, title="alpha-task-1", assignee="dev")
            kb.create_task(conn, title="alpha-task-2", assignee="dev")

        with kb.connect(board="beta") as conn:
            kb.create_task(conn, title="beta-only", assignee="dev")

        with kb.connect(board="alpha") as conn:
            a = kb.list_tasks(conn)
        with kb.connect(board="beta") as conn:
            b = kb.list_tasks(conn)
        with kb.connect(board="default") as conn:
            d = kb.list_tasks(conn)

        assert {t.title for t in a} == {"alpha-task-1", "alpha-task-2"}
        assert {t.title for t in b} == {"beta-only"}
        assert d == []

    def test_connect_without_args_uses_current(self, fresh_home):
        kb.create_board("curr")
        kb.set_current_board("curr")
        with kb.connect() as conn:
            kb.create_task(conn, title="implicit", assignee="x")
        with kb.connect(board="curr") as conn:
            tasks = kb.list_tasks(conn)
        assert [t.title for t in tasks] == ["implicit"]

    def test_connect_env_var_overrides_current(self, fresh_home, monkeypatch):
        kb.create_board("persist")
        kb.create_board("envwin")
        kb.set_current_board("persist")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "envwin")
        with kb.connect() as conn:
            kb.create_task(conn, title="via-env", assignee="x")
        with kb.connect(board="envwin") as conn:
            assert [t.title for t in kb.list_tasks(conn)] == ["via-env"]
        with kb.connect(board="persist") as conn:
            assert kb.list_tasks(conn) == []

    def test_connect_stale_env_uses_fallback_board_without_recreating_it(
        self, fresh_home, monkeypatch,
    ):
        kb.create_board("ephemeral")
        kb.remove_board("ephemeral")
        kb.create_board("persist")
        kb.set_current_board("persist")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "ephemeral")

        with kb.connect() as conn:
            kb.create_task(conn, title="via-fallback", assignee="x")

        with kb.connect(board="persist") as conn:
            assert [t.title for t in kb.list_tasks(conn)] == ["via-fallback"]
        assert not kb.board_exists("ephemeral")


# ---------------------------------------------------------------------------
# Worker spawn env injection
# ---------------------------------------------------------------------------

class TestWorkerSpawnEnv:
    """Ensure the dispatcher pins ``HERMES_KANBAN_BOARD`` / DB / workspaces on spawn.

    We monkey-patch ``subprocess.Popen`` to capture the child env without
    actually spawning anything.
    """

    def test_default_spawn_sets_env_vars(self, fresh_home, monkeypatch):
        captured = {}

        class FakeProc:
            pid = 12345

        def fake_popen(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        kb.create_board("spawntest")

        task = kb.Task(
            id="t_abc",
            title="worker test",
            body=None,
            assignee="teknium",
            status="ready",
            priority=0,
            created_by="user",
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="scratch",
            workspace_path=None,
            claim_lock=None,
            claim_expires=None,
            tenant=None,
        )

        kb._default_spawn(task, str(fresh_home / "ws"), board="spawntest")

        env = captured["env"]
        assert env["HERMES_KANBAN_BOARD"] == "spawntest"
        assert env["HERMES_KANBAN_TASK"] == "t_abc"
        # DB path should match the per-board DB, not the legacy default.
        expected_db = fresh_home / "kanban" / "boards" / "spawntest" / "kanban.db"
        assert env["HERMES_KANBAN_DB"] == str(expected_db)
        expected_ws = fresh_home / "kanban" / "boards" / "spawntest" / "workspaces"
        assert env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(expected_ws)

    def test_default_board_spawn_keeps_legacy_paths(self, fresh_home, monkeypatch):
        captured = {}

        class FakeProc:
            pid = 1

        def fake_popen(cmd, *args, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        task = kb.Task(
            id="t_def",
            title="",
            body=None,
            assignee="teknium",
            status="ready",
            priority=0,
            created_by=None,
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="scratch",
            workspace_path=None,
            claim_lock=None,
            claim_expires=None,
            tenant=None,
        )
        kb._default_spawn(task, str(fresh_home / "ws"), board=None)
        env = captured["env"]
        assert env["HERMES_KANBAN_BOARD"] == "default"
        assert env["HERMES_KANBAN_DB"] == str(fresh_home / "kanban.db")


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def _cli(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``hermes kanban …`` with PYTHONPATH pinned to the worktree."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_WORKTREE)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban"] + args,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_WORKTREE),
        timeout=30,
    )


class TestCLI:
    def test_boards_list_default_only(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        res = _cli(["boards", "list", "--json"], env_extra=env)
        assert res.returncode == 0, res.stderr
        data = json.loads(res.stdout)
        slugs = [b["slug"] for b in data]
        assert slugs == ["default"]
        assert data[0]["is_current"] is True

    def test_boards_create_and_switch(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        r1 = _cli(
            ["boards", "create", "myproj", "--name", "My Project", "--switch"],
            env_extra=env,
        )
        assert r1.returncode == 0, r1.stderr
        assert "created" in r1.stdout
        assert "Switched" in r1.stdout

        r2 = _cli(["boards", "list", "--json"], env_extra=env)
        data = json.loads(r2.stdout)
        cur = [b for b in data if b["is_current"]][0]
        assert cur["slug"] == "myproj"

    def test_per_board_task_isolation_via_cli(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        assert _cli(["boards", "create", "projA"], env_extra=env).returncode == 0
        assert _cli(["boards", "create", "projB"], env_extra=env).returncode == 0

        # Create one task on each via --board.
        r = _cli(["--board", "projA", "create", "Task A", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr
        r = _cli(["--board", "projB", "create", "Task B", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr

        # list on each board only shows its own.
        listA = _cli(["--board", "projA", "list", "--json"], env_extra=env)
        listB = _cli(["--board", "projB", "list", "--json"], env_extra=env)
        listD = _cli(["list", "--json"], env_extra=env)

        titlesA = [t["title"] for t in json.loads(listA.stdout)]
        titlesB = [t["title"] for t in json.loads(listB.stdout)]
        titlesD = [t["title"] for t in json.loads(listD.stdout)]

        assert titlesA == ["Task A"]
        assert titlesB == ["Task B"]
        assert titlesD == []

    def test_board_flag_rejects_unknown(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        r = _cli(["--board", "ghost", "list"], env_extra=env)
        # main.py's dispatcher doesn't propagate return codes today, so we
        # assert the user-visible signal: a stderr error message. Whether
        # the exit code stays 0 is a separate (pre-existing) issue.
        assert "does not exist" in r.stderr

    def test_board_flag_rejects_empty_board_dir(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        ghost = tmp_path / "kanban" / "boards" / "ghost"
        ghost.mkdir(parents=True)
        r = _cli(["--board", "ghost", "list"], env_extra=env)
        assert "does not exist" in r.stderr

    def test_boards_rm_archives(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        _cli(["boards", "create", "rmme"], env_extra=env)
        r = _cli(["boards", "rm", "rmme"], env_extra=env)
        assert r.returncode == 0, r.stderr
        assert "archived" in r.stdout
        # Default board list no longer shows it.
        res = _cli(["boards", "list", "--json"], env_extra=env)
        slugs = [b["slug"] for b in json.loads(res.stdout)]
        assert "rmme" not in slugs


# ---------------------------------------------------------------------------
# allowed_boards guard (opt-in single-board enforcement)
# ---------------------------------------------------------------------------

class TestAllowedBoardsGuard:
    """``kanban.allowed_boards`` gates ``create_board`` at the choke point.

    Default (absent/None) is permissive so the upstream multi-board feature
    is preserved. When set to a non-empty list, only listed slugs (plus the
    always-implicit ``default``) may be created; existing boards outside the
    list still read/return metadata (mkdir -p semantics).
    """

    def test_restricted_slug_is_refused(self, fresh_home):
        with pytest.raises(ValueError, match="restricted"):
            kb.create_board("knowledge", kanban_cfg={"allowed_boards": ["default"]})
        # And no board dir was created as a side effect.
        assert not kb.board_exists("knowledge")

    def test_none_is_permissive_upstream_preserved(self, fresh_home):
        # allowed_boards absent → any slug still works (no regression).
        meta = kb.create_board("anything", kanban_cfg={})
        assert meta["slug"] == "anything"
        assert kb.board_exists("anything")

    def test_none_via_absent_key_is_permissive(self, fresh_home):
        # kanban_cfg with no allowed_boards key at all behaves as unrestricted.
        meta = kb.create_board("freeboard", kanban_cfg={"dispatch_interval_seconds": 60})
        assert meta["slug"] == "freeboard"

    def test_empty_list_is_permissive(self, fresh_home):
        # An empty list is treated as "no restriction", not "allow nothing".
        meta = kb.create_board("openboard", kanban_cfg={"allowed_boards": []})
        assert meta["slug"] == "openboard"

    def test_default_always_allowed(self, fresh_home):
        # The base board is creatable even if the list omits it.
        meta = kb.create_board("default", kanban_cfg={"allowed_boards": ["knowledge"]})
        assert meta["slug"] == "default"

    def test_listed_slug_allowed(self, fresh_home):
        meta = kb.create_board("knowledge", kanban_cfg={"allowed_boards": ["knowledge"]})
        assert meta["slug"] == "knowledge"
        assert kb.board_exists("knowledge")

    def test_existing_board_outside_list_is_idempotent(self, fresh_home):
        # Create while permissive, then a later restricted create must NOT raise
        # (mkdir -p semantics: never break reads/existing boards).
        kb.create_board("legacy", kanban_cfg={})
        meta = kb.create_board("legacy", kanban_cfg={"allowed_boards": ["default"]})
        assert meta["slug"] == "legacy"

    def test_slug_normalized_before_guard(self, fresh_home):
        # Guard compares the normalized slug, so an allow-list of the
        # normalized form matches an un-normalized input.
        meta = kb.create_board("Knowledge", kanban_cfg={"allowed_boards": ["knowledge"]})
        assert meta["slug"] == "knowledge"

    def test_cli_refuses_restricted_slug(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        (tmp_path / "config.yaml").write_text(
            "kanban:\n  allowed_boards:\n    - default\n",
            encoding="utf-8",
        )
        r = _cli(["boards", "create", "knowledge"], env_extra=env)
        # main.py's dispatcher doesn't propagate subcommand return codes today
        # (see test_board_flag_rejects_unknown), so we assert the user-visible
        # signal: the policy error on stderr, plus that no board was created.
        combined = (r.stdout + r.stderr).lower()
        assert "not permitted" in combined or "single-board" in combined
        assert "restricted" in combined
        # default board still reachable and no new board landed.
        res = _cli(["boards", "list", "--json"], env_extra=env)
        slugs = [b["slug"] for b in json.loads(res.stdout)]
        assert slugs == ["default"]

    def test_cli_returns_nonzero_on_restricted(self):
        # Unit-level assertion that the CLI handler itself returns a non-zero
        # exit code when the guard fires — independent of main.py's dispatcher
        # (which currently discards subcommand return codes).
        import argparse
        from hermes_cli import kanban as kbcli

        ns = argparse.Namespace(
            slug="knowledge",
            name=None,
            description=None,
            icon=None,
            color=None,
            default_workdir=None,
            switch=False,
        )
        # Force the guard on regardless of ambient config.
        orig = kb.create_board

        def _fake_create_board(slug, **kw):
            return orig(slug, kanban_cfg={"allowed_boards": ["default"]}, **kw)

        kbcli.kb.create_board = _fake_create_board
        try:
            rc = kbcli._cmd_boards_create(ns)
        finally:
            kbcli.kb.create_board = orig
        assert rc != 0


# ---------------------------------------------------------------------------
# allowed_boards guard on the implicit connect()/init_db() create path
# ---------------------------------------------------------------------------

class TestConnectAllowedBoardsGuard:
    """``connect(board=<slug>)`` must not materialize a board behind the
    ``allowed_boards`` guard.

    ``create_board`` was the documented single choke point, but any caller
    reaching ``connect(board=<unknown-slug>)`` used to run an unconditional
    ``mkdir -p`` and silently create ``<root>/kanban/boards/<slug>/`` +
    a fresh ``kanban.db``. These tests drive the real config-resolution
    chain by writing ``config.yaml`` under the isolated ``HERMES_HOME``,
    exercising ``_allowed_boards_config`` for real (no mocks).
    """

    @staticmethod
    def _write_allowed(home: Path, slugs: list[str]) -> None:
        body = "kanban:\n  allowed_boards:\n"
        for s in slugs:
            body += f"    - {s}\n"
        (home / "config.yaml").write_text(body, encoding="utf-8")

    def test_blocked_new_board_raises_and_leaves_no_dir(self, fresh_home):
        # allowed_boards=[default]; a connect() for an unknown slug must raise
        # rather than create the board directory as a side effect.
        self._write_allowed(fresh_home, ["default"])
        with pytest.raises(ValueError, match="restricted"):
            kb.connect(board="apitest-probe")
        # Regression: no board directory nor DB left on disk after refusal.
        assert not kb.board_exists("apitest-probe")
        assert not (fresh_home / "kanban" / "boards" / "apitest-probe").exists()

    def test_allowed_new_board_connects(self, fresh_home):
        # A slug on the allow-list connects and materializes normally.
        self._write_allowed(fresh_home, ["knowledge"])
        conn = kb.connect(board="knowledge")
        try:
            assert kb.board_exists("knowledge")
        finally:
            conn.close()

    def test_existing_board_outside_list_still_connects(self, fresh_home):
        # Create a board while unrestricted, then restrict the allow-list to
        # exclude it. Reads of an existing board must never break (mkdir -p
        # contract): connect() still works.
        kb.create_board("legacy", kanban_cfg={})
        assert kb.board_exists("legacy")
        self._write_allowed(fresh_home, ["default"])
        conn = kb.connect(board="legacy")
        try:
            assert kb.board_exists("legacy")
        finally:
            conn.close()

    def test_guard_disabled_passthrough(self, fresh_home):
        # With allowed_boards unset (no config), connect(board=<new>) behaves
        # exactly as today — the upstream multi-board feature is preserved.
        conn = kb.connect(board="freshproj")
        try:
            assert kb.board_exists("freshproj")
        finally:
            conn.close()

    def test_default_always_connects(self, fresh_home):
        # The base board can never be locked out, even when the allow-list
        # omits it.
        self._write_allowed(fresh_home, ["knowledge"])
        conn = kb.connect(board="default")
        try:
            assert kb.board_exists("default")
        finally:
            conn.close()

    def test_init_db_blocked_new_board_raises(self, fresh_home):
        # init_db(board=<slug>) is a sibling create path and must be guarded
        # too — the fix belongs at the slug→path resolution, not one caller.
        self._write_allowed(fresh_home, ["default"])
        with pytest.raises(ValueError, match="restricted"):
            kb.init_db(board="apitest-probe")
        assert not (fresh_home / "kanban" / "boards" / "apitest-probe").exists()

    def test_explicit_db_path_is_unguarded(self, fresh_home):
        # connect(db_path=...) has no board slug to check — legacy/test
        # callers that pass a raw path are never gated by allowed_boards.
        self._write_allowed(fresh_home, ["default"])
        raw = fresh_home / "raw.db"
        conn = kb.connect(db_path=raw)
        try:
            assert raw.exists()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# allowed_boards guard resolves config from the ISOLATED kanban root
# ---------------------------------------------------------------------------

class TestAllowedBoardsRespectsIsolatedRoot:
    """``_allowed_boards_config`` must read the allow-list from the isolated
    kanban root when ``HERMES_KANBAN_HOME`` is set.

    Regression: the guard resolved its allow-list from the *live*
    ``~/.hermes/config.yaml`` (via ``load_config()``) even when the caller
    had explicitly isolated the kanban root via ``HERMES_KANBAN_HOME``. A
    correctly-isolated test harness — one that never touches the live board —
    still had the production allow-list (``allowed_boards: [default]``)
    applied and failed at ``connect(board="test")``. This is the
    self-defeating-safety shape: the guard punishing the correct isolation.

    These tests wire the two roots to *different* temp dirs: ``HERMES_HOME``
    (the stand-in for the live root) carries ``allowed_boards: [default]``,
    while ``HERMES_KANBAN_HOME`` (the isolated kanban root) has no config.
    An isolated root with no config yields no allow-list, which under the
    documented semantics means unrestricted — so an isolated board is
    permitted while the live root keeps its restriction.
    """

    @staticmethod
    def _split_roots(tmp_path, monkeypatch, *, live_slugs):
        """Point HERMES_HOME (live) and HERMES_KANBAN_HOME (isolated) apart.

        The live root gets a ``config.yaml`` restricting ``allowed_boards``
        to ``live_slugs``; the isolated kanban root gets no config at all.
        Returns ``(live_home, kanban_home)``.
        """
        live_home = tmp_path / "live_home"
        live_home.mkdir()
        body = "kanban:\n  allowed_boards:\n"
        for s in live_slugs:
            body += f"    - {s}\n"
        (live_home / "config.yaml").write_text(body, encoding="utf-8")

        kanban_home = tmp_path / "kanban_home"
        kanban_home.mkdir()

        monkeypatch.setenv("HERMES_HOME", str(live_home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
        for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
            monkeypatch.delenv(var, raising=False)
        try:
            import hermes_constants
            hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
        except Exception:
            pass
        kb._INITIALIZED_PATHS.clear()
        return live_home, kanban_home

    def test_isolated_root_allows_test_board(self, tmp_path, monkeypatch):
        # HERMES_KANBAN_HOME isolated (no config) but the live HERMES_HOME
        # restricts to [default]. connect(board="test") must SUCCEED because
        # the guard reads the isolated root's (absent) config, not the live one.
        _live, kanban_home = self._split_roots(
            tmp_path, monkeypatch, live_slugs=["default"]
        )
        conn = kb.connect(board="test")
        try:
            assert kb.board_exists("test")
            # And the board materialized under the ISOLATED root, never live.
            assert (kanban_home / "kanban" / "boards" / "test").exists()
        finally:
            conn.close()

    def test_isolated_root_yields_no_restriction(self, tmp_path, monkeypatch):
        # _allowed_boards_config() must resolve None (unrestricted) from the
        # isolated root even though the live config lists [default].
        self._split_roots(tmp_path, monkeypatch, live_slugs=["default"])
        assert kb._allowed_boards_config() is None

    def test_isolated_root_honours_its_own_allowlist(self, tmp_path, monkeypatch):
        # When the ISOLATED root itself carries an allow-list, THAT list wins
        # (not the live one). Proves resolution is anchored on the kanban root.
        _live, kanban_home = self._split_roots(
            tmp_path, monkeypatch, live_slugs=["default"]
        )
        (kanban_home / "config.yaml").write_text(
            "kanban:\n  allowed_boards:\n    - knowledge\n", encoding="utf-8"
        )
        # "knowledge" is on the isolated list → permitted.
        conn = kb.connect(board="knowledge")
        try:
            assert kb.board_exists("knowledge")
        finally:
            conn.close()
        # "test" is on neither list → refused by the isolated root's own guard.
        with pytest.raises(ValueError, match="restricted"):
            kb.connect(board="test")

    def test_live_root_still_refuses_stray_board(self, fresh_home):
        # Negative: with NO isolated root set and allowed_boards: [default] in
        # the live config, a stray board is still refused. The guard's policy
        # is untouched — this card fixes WHERE the list is read, not WHETHER
        # it is enforced.
        (fresh_home / "config.yaml").write_text(
            "kanban:\n  allowed_boards:\n    - default\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match=r"restricted to \['default'\]"):
            kb.create_board("stray")
        assert not kb.board_exists("stray")

    def test_injected_cfg_override_unchanged(self, tmp_path, monkeypatch):
        # The injected-kanban_cfg path is word-for-word unchanged: an explicit
        # cfg always wins, regardless of HERMES_KANBAN_HOME. Isolated root has
        # no config, but the injected [knowledge] list still governs.
        self._split_roots(tmp_path, monkeypatch, live_slugs=["default"])
        assert kb._allowed_boards_config({"allowed_boards": ["knowledge"]}) == {
            "knowledge"
        }
        # And an injected empty/absent cfg is still unrestricted (None).
        assert kb._allowed_boards_config({}) is None
        assert kb._allowed_boards_config({"allowed_boards": []}) is None


class TestAllowedBoardsGuardArmedWhenDbPinnedAtLiveRoot:
    """The isolated-root branch must gate on where the DB actually resolves,
    not on the mere presence of ``HERMES_KANBAN_HOME``.

    Regression (acceptance-gate finding): ``HERMES_KANBAN_DB`` has strictly
    higher precedence than ``HERMES_KANBAN_HOME`` in :func:`kanban_db_path`, so
    a process can point the kanban ROOT at a throwaway dir while every write
    still lands in the live ``kanban.db``. The first cut of the isolated-root
    fix keyed off ``HERMES_KANBAN_HOME`` being set, so in that state it read the
    throwaway root's (absent) config → unrestricted → the guard was silently
    DISARMED on the live board. That is strictly worse than the false-positive
    this whole change fixes: the operator loses the single-board protection they
    already shipped. The dispatcher injects ``HERMES_KANBAN_DB`` into every
    worker's env, so this is the common path, not a corner case.

    The correct predicate is "does the resolved DB actually live under the
    kanban root?" — a fact derived from the two existing resolution functions,
    not a proxy signal.
    """

    @staticmethod
    def _pin_db_at_live(tmp_path, monkeypatch, *, live_slugs, extra_env=()):
        """Live root restricts to ``live_slugs``; kanban root is a throwaway
        dir; ``HERMES_KANBAN_DB`` is pinned at the LIVE db path.

        Returns ``(live_home, kanban_root, live_db)``.
        """
        live_home = tmp_path / "live_home"
        live_home.mkdir()
        body = "kanban:\n  allowed_boards:\n"
        for s in live_slugs:
            body += f"    - {s}\n"
        (live_home / "config.yaml").write_text(body, encoding="utf-8")

        kanban_root = tmp_path / "kanban_root"
        kanban_root.mkdir()
        live_db = live_home / "kanban.db"

        monkeypatch.setenv("HERMES_HOME", str(live_home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_root))
        monkeypatch.setenv("HERMES_KANBAN_DB", str(live_db))
        for var, val in extra_env:
            monkeypatch.setenv(var, val)
        try:
            import hermes_constants
            hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
        except Exception:
            pass
        kb._INITIALIZED_PATHS.clear()
        return live_home, kanban_root, live_db

    def test_db_pinned_at_live_keeps_restriction(self, tmp_path, monkeypatch):
        # HERMES_KANBAN_HOME set (throwaway) BUT HERMES_KANBAN_DB pins the live
        # db. The allow-list must resolve from the LIVE config ([default]), not
        # the empty isolated root — the guard stays armed on the live board.
        self._pin_db_at_live(tmp_path, monkeypatch, live_slugs=["default"])
        assert kb._allowed_boards_config() == {"default"}

    def test_db_pinned_at_live_refuses_stray_board(self, tmp_path, monkeypatch):
        # Both entry points must refuse a stray board when the resolved DB is
        # the live one, even with HERMES_KANBAN_HOME pointed elsewhere.
        _live, kanban_root, live_db = self._pin_db_at_live(
            tmp_path, monkeypatch, live_slugs=["default"]
        )
        with pytest.raises(ValueError, match=r"restricted to \['default'\]"):
            kb.create_board("stray")
        with pytest.raises(ValueError, match=r"restricted to \['default'\]"):
            kb.connect(board="stray")
        assert not kb.board_exists("stray")
        # The stray board must not have materialized under EITHER root.
        assert not (kanban_root / "kanban" / "boards" / "stray").exists()

    def test_db_pinned_at_live_with_board_and_workspaces_env(
        self, tmp_path, monkeypatch
    ):
        # The dispatcher co-sets HERMES_KANBAN_BOARD and
        # HERMES_KANBAN_WORKSPACES_ROOT alongside HERMES_KANBAN_DB. The
        # DB-anchoring predicate — not any single env var — must still resolve
        # the live allow-list and refuse a stray board.
        _live, _root, live_db = self._pin_db_at_live(
            tmp_path,
            monkeypatch,
            live_slugs=["default"],
            extra_env=(
                ("HERMES_KANBAN_BOARD", "default"),
                ("HERMES_KANBAN_WORKSPACES_ROOT", str(tmp_path / "ws")),
            ),
        )
        assert kb._allowed_boards_config() == {"default"}
        with pytest.raises(ValueError, match=r"restricted to \['default'\]"):
            kb.create_board("stray")

    def test_no_input_the_base_restricted_becomes_unrestricted(
        self, tmp_path, monkeypatch
    ):
        # Parity assertion: the head must not classify as unrestricted anything
        # a live restricted config would have classified as restricted. This is
        # the criterion the isolated-root-only tests missed. With the live db
        # pinned, the answer is the live [default] set — never None.
        self._pin_db_at_live(tmp_path, monkeypatch, live_slugs=["default"])
        assert kb._allowed_boards_config() is not None
        # And a genuinely isolated DB (no pin) IS unrestricted — the card's fix.
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        kb._INITIALIZED_PATHS.clear()
        assert kb._allowed_boards_config() is None

    def test_root_anchors_db_predicate(self, tmp_path, monkeypatch):
        # Direct unit coverage of the derived predicate.
        _live, kanban_root, _db = self._pin_db_at_live(
            tmp_path, monkeypatch, live_slugs=["default"]
        )
        # DB pinned at the live root → NOT anchored under the kanban root.
        assert kb._kanban_root_anchors_db() is False
        # Drop the pin → the DB resolves under the kanban root → anchored.
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        kb._INITIALIZED_PATHS.clear()
        assert kb._kanban_root_anchors_db() is True


class TestAllowedBoardsGuardArmedInProfileMode:
    """The isolated-root branch must fire only when the caller DECLARED an
    isolated root (``HERMES_KANBAN_HOME`` explicitly set), not for the ambient
    shared default root.

    Regression (round-2 review finding): the DB-anchoring predicate keyed off
    "does the resolved DB live under ``kanban_home()``?" alone. But with NO
    isolation env set, ``kanban_home()`` returns the shared default root and the
    default DB ``<root>/kanban.db`` is trivially anchored under it — so the
    predicate was true in the ORDINARY live case. The direct-read branch then
    read ``<root>/config.yaml`` instead of the active profile's config. In
    profile mode (``HERMES_HOME=<root>/profiles/<name>``) those are different
    files: ``hermes config set`` writes the profile config, so
    ``allowed_boards`` naturally lives in ``<root>/profiles/<name>/config.yaml``
    while the shared-root ``<root>/config.yaml`` is absent → unrestricted → the
    guard silently DISARMED on the live board.

    ``load_config()`` reads the active profile's config; the shared-root direct
    read does not. So the guard must fall through to ``load_config()`` whenever
    ``HERMES_KANBAN_HOME`` is unset.
    """

    @staticmethod
    def _profile_mode(tmp_path, monkeypatch, *, profile_slugs, shared_slugs=None):
        """Wire a profile-mode layout: ``HERMES_HOME=<root>/profiles/<name>``.

        The PROFILE config restricts ``allowed_boards`` to ``profile_slugs``.
        The shared-root ``<root>/config.yaml`` is absent unless ``shared_slugs``
        is given. No kanban env vars are set — this is the ordinary live case.

        Returns ``(root, profile_home)``.
        """
        root = tmp_path / "root"
        profile_home = root / "profiles" / "coder"
        profile_home.mkdir(parents=True)

        def _body(slugs):
            b = "kanban:\n  allowed_boards:\n"
            for s in slugs:
                b += f"    - {s}\n"
            return b

        (profile_home / "config.yaml").write_text(
            _body(profile_slugs), encoding="utf-8"
        )
        if shared_slugs is not None:
            (root / "config.yaml").write_text(_body(shared_slugs), encoding="utf-8")

        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        for var in (
            "HERMES_KANBAN_HOME",
            "HERMES_KANBAN_DB",
            "HERMES_KANBAN_BOARD",
            "HERMES_KANBAN_WORKSPACES_ROOT",
        ):
            monkeypatch.delenv(var, raising=False)
        try:
            import hermes_constants
            hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
        except Exception:
            pass
        kb._INITIALIZED_PATHS.clear()
        return root, profile_home

    def test_profile_config_restriction_is_honoured(self, tmp_path, monkeypatch):
        # allowed_boards in the PROFILE config, shared-root config absent,
        # no kanban isolation env. The allow-list must resolve from the profile
        # config via load_config() — never from the (absent) shared-root config.
        self._profile_mode(tmp_path, monkeypatch, profile_slugs=["default"])
        assert kb._allowed_boards_config() == {"default"}

    def test_profile_mode_refuses_stray_board(self, tmp_path, monkeypatch):
        # Ground-truth parity: the base refused a stray board in this exact
        # state; the head must too. Guard stays armed on the live board.
        self._profile_mode(tmp_path, monkeypatch, profile_slugs=["default"])
        with pytest.raises(ValueError, match=r"restricted to \['default'\]"):
            kb.create_board("stray")
        with pytest.raises(ValueError, match=r"restricted to \['default'\]"):
            kb.connect(board="stray")
        assert not kb.board_exists("stray")

    def test_unset_kanban_home_never_anchors(self, tmp_path, monkeypatch):
        # The predicate must be False whenever HERMES_KANBAN_HOME is unset, even
        # though the default DB is anchored under the ambient shared root.
        self._profile_mode(tmp_path, monkeypatch, profile_slugs=["default"])
        assert kb._kanban_root_anchors_db() is False

    def test_shared_root_config_does_not_override_profile(
        self, tmp_path, monkeypatch
    ):
        # Even when the shared-root config exists, an unset HERMES_KANBAN_HOME
        # means the PROFILE config governs (load_config path), not the shared
        # root's direct read. The profile restricts to [default]; a shared-root
        # list of [knowledge] must NOT leak in to permit a stray board.
        self._profile_mode(
            tmp_path,
            monkeypatch,
            profile_slugs=["default"],
            shared_slugs=["knowledge"],
        )
        assert kb._allowed_boards_config() == {"default"}
        with pytest.raises(ValueError, match=r"restricted to \['default'\]"):
            kb.create_board("knowledge")
