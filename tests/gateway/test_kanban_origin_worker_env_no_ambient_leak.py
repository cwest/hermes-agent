"""A dispatched worker must never inherit the dispatcher's ambient origin.

``_default_spawn`` builds the worker environment with ``dict(os.environ)``. The
dispatcher runs inside the gateway process, whose ``HERMES_KANBAN_ORIGIN`` is a
last-writer-wins process-global belonging to whichever turn most recently bound
an origin — NOT necessarily the workstream this worker serves. Copying it hands
the worker a foreign delivery surface, and every card that worker creates
inherits it verbatim (``capture_kanban_origin_from_session`` returns an
inherited origin before it ever consults the live session). Cards filed in one
thread then report into an unrelated thread.

The per-card seed (``worker_origin_env``) overwrites the copy — but only when
the card has a routable notify-sub, and the lookup is wrapped in a bare
``except``. When either condition fails the ambient value survives.

These tests drive the REAL ``_default_spawn`` and assert on the env actually
handed to ``subprocess.Popen``, so they stay red if the sanitizing call site is
removed. Asserting against ``sanitize_worker_env`` directly would pass with the
call site reverted — vacuous coverage of the helper rather than the seam.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb

FOREIGN = (
    '{"platform": "discord", "chat_id": "FOREIGN_THREAD", '
    '"thread_id": "FOREIGN_THREAD", "user_id": null, '
    '"session_id": "agent:main:discord:thread:FOREIGN_THREAD:FOREIGN_THREAD", '
    '"_owner_pid": 4242}'
)


@pytest.fixture
def spawn_env(tmp_path, monkeypatch):
    """Drive the real ``_default_spawn`` and return the env it built."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))

    captured: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["env"] = kwargs.get("env", {})
            self.pid = 4242

    monkeypatch.setattr("subprocess.Popen", _FakePopen)

    def _run(task_id: str = "t_origin_leak"):
        task = kb.Task(
            id=task_id,
            title="x",
            body=None,
            assignee="coder",
            status="ready",
            priority=0,
            created_by=None,
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "ws"),
            claim_lock=None,
            claim_expires=None,
            tenant=None,
            branch_name=f"wt/{task_id}",
        )
        kb._default_spawn(task, str(tmp_path / "ws"))
        return captured["env"]

    return _run


def test_spawn_drops_ambient_origin_when_card_has_no_sub(spawn_env, monkeypatch):
    """No routable sub => worker gets NO origin rather than a foreign one."""
    monkeypatch.setenv("HERMES_KANBAN_ORIGIN", FOREIGN)

    env = spawn_env()

    assert "FOREIGN_THREAD" not in env.get("HERMES_KANBAN_ORIGIN", ""), (
        "worker inherited the dispatcher's ambient origin; cards it files would "
        f"route to a foreign thread: {env.get('HERMES_KANBAN_ORIGIN')!r}"
    )


def test_spawn_seeds_this_cards_origin_over_ambient(spawn_env, monkeypatch, tmp_path):
    """The legitimate inheritance path survives: THIS card's sub wins."""
    monkeypatch.setenv("HERMES_KANBAN_ORIGIN", FOREIGN)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder", detached=True)
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="discord",
            chat_id="MY_THREAD",
            thread_id="MY_THREAD",
        )
    finally:
        conn.close()

    env = spawn_env(tid)

    seeded = env.get("HERMES_KANBAN_ORIGIN", "")
    assert "MY_THREAD" in seeded, f"per-card origin seed was lost: {seeded!r}"
    assert "FOREIGN_THREAD" not in seeded
