"""The curator never archives an idle skill that an open kanban card names.

Usage only bumps when a skill loads, so a skill a queued card will load or edit can age out the day
before a worker picks that card up. These tests drive the real archive pass against a real board in
a temp kanban home.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)

    import tools.skill_usage as usage
    importlib.reload(usage)
    from agent import curator
    importlib.reload(curator)
    monkeypatch.setattr(curator, "_load_config", dict)
    monkeypatch.setattr(usage, "_prune_builtins_enabled", lambda: False)
    monkeypatch.setattr(curator, "_cron_referenced_skills", set)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    return {"home": home, "curator": curator, "usage": usage, "kb": kb, "kbc": kbc}


def _idle_skill(env, name: str, days: int = 200) -> None:
    d = env["home"] / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n", encoding="utf-8")
    u = env["usage"]
    ts = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    data = u.load_usage()
    data[name] = {**u._empty_record(), "created_by": "agent", "created_at": ts,
                  "last_used_at": ts, "last_activity_at": ts, "use_count": 1}
    u.save_usage(data)


def _card(env, body: str, **kw) -> str:
    with env["kbc"].connect_closing() as conn:
        return env["kb"].create_task(conn, title="work", body=body, assignee="worker", **kw)


def _state(env, name: str) -> str:
    return env["usage"].load_usage()[name]["state"]


def _skip_entries(name: str) -> list[dict]:
    from tools import skill_ledger
    return [e for e in skill_ledger.list_entries(skill=name) if e.get("action") == "skip-archive"]


def test_skill_named_in_ready_card_body_is_not_archived(env):
    _idle_skill(env, "researching-lodging-prices")
    _idle_skill(env, "orphan")
    tid = _card(env, "Load researching-lodging-prices before pricing the trip.")

    counts = env["curator"].apply_automatic_transitions()

    assert _state(env, "researching-lodging-prices") != env["usage"].STATE_ARCHIVED
    assert _state(env, "orphan") == env["usage"].STATE_ARCHIVED, "the guard must not over-protect"
    assert counts["skipped_referenced"] == 1
    [entry] = _skip_entries("researching-lodging-prices")
    assert entry["evidence"]["skipped"] == f"skipped: referenced by {tid}"
    assert entry["actor"] == "curator"


def test_same_skill_is_archived_once_the_card_is_done(env):
    _idle_skill(env, "researching-lodging-prices")
    tid = _card(env, "Load researching-lodging-prices before pricing the trip.")
    env["curator"].apply_automatic_transitions()
    assert _state(env, "researching-lodging-prices") != env["usage"].STATE_ARCHIVED

    with env["kbc"].connect_closing() as conn:
        assert env["kb"].complete_task(conn, tid, summary="done")
    env["curator"].apply_automatic_transitions()

    assert _state(env, "researching-lodging-prices") == env["usage"].STATE_ARCHIVED


def test_board_read_failure_archives_nothing(env, monkeypatch):
    _idle_skill(env, "alpha")
    _idle_skill(env, "beta")
    _card(env, "unrelated")

    def _boom(*a, **k):
        raise OSError("disk I/O error")
    monkeypatch.setattr(env["kb"], "list_tasks", _boom)

    counts = env["curator"].apply_automatic_transitions()

    assert counts["archived"] == 0 and counts["skipped_referenced"] == 2
    for name in ("alpha", "beta"):
        assert _state(env, name) != env["usage"].STATE_ARCHIVED
        assert _skip_entries(name)[0]["evidence"]["skipped"].startswith("skipped: kanban board unreadable")


def test_skills_field_and_comments_count_as_references(env):
    _idle_skill(env, "from-field")
    _idle_skill(env, "from-comment")
    with env["kbc"].connect_closing() as conn:
        tid = env["kb"].create_task(conn, title="t", body="nothing named here", assignee="w", skills=["from-field"])
        env["kb"].add_comment(conn, tid, "orchestrator", "Also relies on from-comment.")

    env["curator"].apply_automatic_transitions()

    assert _state(env, "from-field") != env["usage"].STATE_ARCHIVED
    assert _state(env, "from-comment") != env["usage"].STATE_ARCHIVED


def test_match_is_whole_token_not_substring(env):
    """A card naming ``git-helpers`` must not protect ``git``, nor ``helpers``."""
    _idle_skill(env, "git")
    _idle_skill(env, "helpers")
    _card(env, "Load git-helpers first.")

    env["curator"].apply_automatic_transitions()

    assert _state(env, "git") == env["usage"].STATE_ARCHIVED
    assert _state(env, "helpers") == env["usage"].STATE_ARCHIVED


def test_install_without_a_board_still_archives(env):
    """No kanban.db at all is an empty board, not an unreadable one."""
    _idle_skill(env, "orphan")
    assert not env["kb"].kanban_db_path().exists()

    env["curator"].apply_automatic_transitions()

    assert _state(env, "orphan") == env["usage"].STATE_ARCHIVED
    assert not env["kb"].kanban_db_path().exists(), "the archive pass must not create a board"
