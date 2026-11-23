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


def _corrupt(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    path.write_bytes(b"this is not a sqlite database" * 64)


def test_board_read_failure_archives_nothing(env):
    _idle_skill(env, "alpha")
    _idle_skill(env, "beta")
    _card(env, "unrelated")
    _corrupt(env["kb"].kanban_db_path())

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


def test_skills_field_leading_slash_still_protects(env):
    """``/github-code-review`` in a card's skills field names ``github-code-review``."""
    _idle_skill(env, "slashed")
    with env["kbc"].connect_closing() as conn:
        env["kb"].create_task(conn, title="t", body="nothing named here", assignee="w", skills=["/slashed"])

    env["curator"].apply_automatic_transitions()

    assert _state(env, "slashed") != env["usage"].STATE_ARCHIVED


# ─── every board, read-only ────────────────────────────────────────────────


def _board_card(env, board: str, body: str) -> str:
    with env["kbc"].connect_closing(board=board) as conn:
        return env["kb"].create_task(conn, title="t", body=body, assignee="w")


def test_cards_on_other_and_archived_boards_protect(env):
    from hermes_cli import kanban_db_boards as kbb
    _idle_skill(env, "other-board-skill")
    _idle_skill(env, "archived-board-skill")
    kbb.create_board("other")
    _board_card(env, "other", "Needs other-board-skill.")
    kbb.create_board("parked")
    _board_card(env, "parked", "Needs archived-board-skill.")
    kbb.write_board_metadata("parked", archived=True)

    env["curator"].apply_automatic_transitions()

    assert _state(env, "other-board-skill") != env["usage"].STATE_ARCHIVED
    assert _state(env, "archived-board-skill") != env["usage"].STATE_ARCHIVED


@pytest.mark.parametrize("pinned", ["default", "other"])
def test_single_board_db_pin_does_not_narrow_the_scan(env, monkeypatch, pinned):
    """Workers carry HERMES_KANBAN_DB pinned to one board; the scan must still read every board."""
    from hermes_cli import kanban_db_boards as kbb
    _idle_skill(env, "default-skill")
    _idle_skill(env, "other-skill")
    _card(env, "Needs default-skill.")
    kbb.create_board("other")
    _board_card(env, "other", "Needs other-skill.")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(env["kb"].kanban_db_path(pinned)))

    env["curator"].apply_automatic_transitions()

    assert _state(env, "default-skill") != env["usage"].STATE_ARCHIVED
    assert _state(env, "other-skill") != env["usage"].STATE_ARCHIVED


def test_off_tree_pinned_db_is_scanned_too(env, monkeypatch, tmp_path):
    _idle_skill(env, "default-skill")
    _idle_skill(env, "pinned-skill")
    _card(env, "Needs default-skill.")
    off_tree = tmp_path / "elsewhere" / "kanban.db"
    off_tree.parent.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(off_tree))
    _card(env, "Needs pinned-skill.")

    env["curator"].apply_automatic_transitions()

    assert _state(env, "default-skill") != env["usage"].STATE_ARCHIVED
    assert _state(env, "pinned-skill") != env["usage"].STATE_ARCHIVED


def test_board_is_read_without_the_read_write_connector(env, monkeypatch):
    """The scan opens each board ``mode=ro``: never the kanban connector, which opens read-write and runs
    schema migrations on whatever board file it is pointed at."""
    _idle_skill(env, "card-skill")
    tid = _card(env, "Needs card-skill.")

    def _forbidden(*a, **k):
        raise AssertionError("the curator's board scan must not open a read-write kanban connection")
    for mod, attr in ((env["kbc"], "connect"), (env["kbc"], "connect_closing"), (env["kb"], "init_db")):
        monkeypatch.setattr(mod, attr, _forbidden)

    env["curator"].apply_automatic_transitions()

    [entry] = _skip_entries("card-skill")
    assert entry["evidence"]["skipped"] == f"skipped: referenced by {tid}"


# ─── every archive path honours the same guard ─────────────────────────────


def _prune(days: int = 30) -> int:
    from types import SimpleNamespace
    import hermes_cli.curator as curator_cli
    return curator_cli._cmd_prune(SimpleNamespace(days=days, yes=True, dry_run=False))


def test_cli_prune_skips_card_referenced_skill(env, capsys):
    _idle_skill(env, "card-skill")
    _idle_skill(env, "orphan")
    tid = _card(env, "Needs card-skill.", initial_status="blocked")

    assert _prune() == 0

    assert (env["home"] / "skills" / "card-skill").exists()
    assert _state(env, "orphan") == env["usage"].STATE_ARCHIVED
    out = capsys.readouterr().out
    assert "card-skill" in out and tid in out, "a held skill is reported with its card"


def test_cli_prune_skips_cron_referenced_skill(env, monkeypatch):
    _idle_skill(env, "cron-skill")
    monkeypatch.setattr(env["curator"], "_cron_referenced_skills", lambda: {"cron-skill"})

    assert _prune() == 0

    assert (env["home"] / "skills" / "cron-skill").exists()


def test_cli_prune_archives_nothing_when_board_unreadable(env, capsys):
    _idle_skill(env, "orphan")
    _card(env, "unrelated")
    _corrupt(env["kb"].kanban_db_path())

    assert _prune() == 1

    assert (env["home"] / "skills" / "orphan").exists()
    assert "unreadable" in capsys.readouterr().out


def test_archive_skill_refuses_card_referenced_skill(env):
    _idle_skill(env, "card-skill")
    tid = _card(env, "Needs card-skill.")

    ok, msg = env["usage"].archive_skill("card-skill")

    assert ok is False and tid in msg
    assert (env["home"] / "skills" / "card-skill").exists()


def test_archive_skill_refuses_when_board_unreadable(env):
    _idle_skill(env, "orphan")
    _card(env, "unrelated")
    _corrupt(env["kb"].kanban_db_path())

    ok, msg = env["usage"].archive_skill("orphan")

    assert ok is False and "unreadable" in msg
    assert (env["home"] / "skills" / "orphan").exists()


def test_archive_skill_still_archives_unreferenced_skill(env):
    _idle_skill(env, "orphan")
    _card(env, "unrelated")

    ok, msg = env["usage"].archive_skill("orphan")

    assert ok, msg


def test_candidate_list_marks_card_referenced_skills(env):
    _idle_skill(env, "card-skill")
    _idle_skill(env, "orphan")
    _card(env, "Needs card-skill.")

    lines = env["curator"]._render_candidate_list().splitlines()

    assert "card=yes" in next(line for line in lines if "- card-skill " in line)
    assert "card=no" in next(line for line in lines if "- orphan " in line)
    assert "card=yes" in env["curator"].CURATOR_REVIEW_PROMPT
