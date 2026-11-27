"""A dispatched Kanban worker may write the protected instruction file its card declares.

A headless worker has nobody to answer the protected-instruction prompt, so a card whose
deliverable IS ``AGENTS.md`` could never finish. The card body may declare it
(``protected-writes: AGENTS.md``); that grant covers exactly the declared paths inside the
card's own workspace, for the card's own worker, and nothing else.
"""

import json

import pytest


@pytest.fixture
def headless_worker(monkeypatch, tmp_path):
    """A dispatcher-owned worker in a ``-q`` session, plus a factory that files and claims its card."""
    import tools.file_tools_write_guards as guards
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools.terminal_tool import set_approval_callback

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    for name in ("HERMES_CRON_SESSION", "HERMES_SESSION_PLATFORM", "HERMES_KANBAN_DB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(guards, "_protected_instruction_config", lambda: (True, []))
    set_approval_callback(None)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    def file_card(body: str, workspace) -> str:
        workspace.mkdir(parents=True, exist_ok=True)
        conn = kbc.connect()
        try:
            tid = kb.create_task(conn, title="add AGENTS.md", assignee="easley", body=body,
                                 workspace_kind="dir", workspace_path=str(workspace))
            kb.claim_task(conn, tid)
            run_id = kb._current_run_id(conn, tid)
        finally:
            conn.close()
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        return tid

    yield file_card
    set_approval_callback(None)


def _write(path) -> dict:
    from tools.file_tools import write_file_tool
    return json.loads(write_file_tool(str(path), "# policy\n"))


def test_declared_agents_md_in_own_workspace_is_written(headless_worker, tmp_path):
    ws = tmp_path / "repo"
    headless_worker("## What\nAdd the policy.\n\nprotected-writes: AGENTS.md\n", ws)

    res = _write(ws / "AGENTS.md")

    assert "error" not in res, res
    assert (ws / "AGENTS.md").read_text() == "# policy\n"


def test_grant_is_recorded_on_the_card(headless_worker, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    ws = tmp_path / "repo"
    tid = headless_worker("protected-writes: AGENTS.md\n", ws)
    _write(ws / "AGENTS.md")

    conn = kbc.connect()
    try:
        granted = [e.payload for e in kb.list_events(conn, tid) if e.kind == "protected_write_granted"]
    finally:
        conn.close()
    assert granted == [{"paths": ["AGENTS.md"]}]


def test_undeclared_agents_md_is_still_refused(headless_worker, tmp_path):
    ws = tmp_path / "repo"
    headless_worker("## What\nFix a typo in README.\n", ws)

    res = _write(ws / "AGENTS.md")

    assert "BLOCKED" in res["error"]
    assert not (ws / "AGENTS.md").exists()


def test_declaration_names_one_file_not_every_protected_file(headless_worker, tmp_path):
    ws = tmp_path / "repo"
    headless_worker("protected-writes: AGENTS.md\n", ws)

    for target in (ws / "CLAUDE.md", ws / "docs" / "AGENTS.md"):
        target.parent.mkdir(parents=True, exist_ok=True)
        res = _write(target)
        assert "BLOCKED" in res["error"], target
        assert not target.exists()


def test_declared_agents_md_outside_the_workspace_is_refused(headless_worker, tmp_path):
    ws = tmp_path / "repo"
    other = tmp_path / "other-repo"
    other.mkdir()
    headless_worker("protected-writes: AGENTS.md\n", ws)

    res = _write(other / "AGENTS.md")

    assert "BLOCKED" in res["error"]
    assert not (other / "AGENTS.md").exists()


def test_symlink_out_of_the_workspace_is_refused(headless_worker, tmp_path):
    ws = tmp_path / "repo"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    headless_worker("protected-writes: AGENTS.md\n", ws)
    (ws / "AGENTS.md").symlink_to(outside / "AGENTS.md")

    res = _write(ws / "AGENTS.md")

    assert "BLOCKED" in res["error"]
    assert not (outside / "AGENTS.md").exists()


@pytest.mark.parametrize("entry", ["../other/AGENTS.md", "/abs/AGENTS.md", "*.md"])
def test_escaping_or_wildcard_declarations_grant_nothing(headless_worker, tmp_path, entry):
    ws = tmp_path / "repo"
    headless_worker(f"protected-writes: {entry}\n", ws)

    res = _write(ws / "AGENTS.md")

    assert "BLOCKED" in res["error"]


def test_body_edited_during_the_run_grants_nothing(headless_worker, tmp_path):
    """A worker that rewrites its own card mid-run (``hermes kanban edit`` via terminal) must not
    mint itself a grant: only a declaration made before the run started counts."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    ws = tmp_path / "repo"
    tid = headless_worker("## What\nFix a typo.\n", ws)
    conn = kbc.connect()
    try:
        kb.edit_task(conn, tid, body="protected-writes: AGENTS.md\n")
    finally:
        conn.close()

    res = _write(ws / "AGENTS.md")

    assert "BLOCKED" in res["error"]
    assert not (ws / "AGENTS.md").exists()


def test_delegated_child_of_the_worker_gets_no_grant(headless_worker, tmp_path, monkeypatch):
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    ws = tmp_path / "repo"
    headless_worker("protected-writes: AGENTS.md\n", ws)
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, "1")

    res = _write(ws / "AGENTS.md")

    assert "BLOCKED" in res["error"]
    assert not (ws / "AGENTS.md").exists()


def test_patch_on_a_declared_file_is_allowed(headless_worker, tmp_path):
    from tools.file_tools import patch_tool

    ws = tmp_path / "repo"
    headless_worker("protected-writes: AGENTS.md, docs/CLAUDE.md\n", ws)
    (ws / "docs").mkdir()
    (ws / "docs" / "CLAUDE.md").write_text("old line\n")

    res = json.loads(patch_tool(mode="replace", path=str(ws / "docs" / "CLAUDE.md"),
                                old_string="old line", new_string="new line"))

    assert "error" not in res, res
    assert (ws / "docs" / "CLAUDE.md").read_text() == "new line\n"


def test_card_filed_by_a_worker_grants_nothing(headless_worker, tmp_path, monkeypatch):
    """An injected worker must not mint a grant by filing a child card that declares one."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    parent = headless_worker("## What\nFix a typo.\n", tmp_path / "parent")
    ws = tmp_path / "repo"
    ws.mkdir()
    conn = kbc.connect()
    try:
        child = kb.create_task(conn, title="child", assignee="easley", body="protected-writes: AGENTS.md\n",
                               workspace_kind="dir", workspace_path=str(ws), creator_task_id=parent)
        kb.claim_task(conn, child)
        run_id = kb._current_run_id(conn, child)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", child)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    res = _write(ws / "AGENTS.md")

    assert "BLOCKED" in res["error"]
    assert not (ws / "AGENTS.md").exists()


def test_superseded_run_grants_nothing(headless_worker, tmp_path, monkeypatch):
    ws = tmp_path / "repo"
    headless_worker("protected-writes: AGENTS.md\n", ws)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "999999")

    res = _write(ws / "AGENTS.md")

    assert "BLOCKED" in res["error"]
    assert not (ws / "AGENTS.md").exists()


def test_declared_entries_parse_from_the_body():
    from tools.file_tools_kanban_grant import declared_protected_writes

    body = ("## Scope\n- protected-writes: `AGENTS.md`, docs/CLAUDE.md\n"
            "Protected-Writes: ../x/AGENTS.md, ./AGENTS.md, a//b.md, *.md, AGENTS.md\n")
    assert declared_protected_writes(body) == ("AGENTS.md", "docs/CLAUDE.md")
    assert declared_protected_writes("mentions protected-writes inline only") == ()
