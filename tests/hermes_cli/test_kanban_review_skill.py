"""``kanban.review_skill``: which skill the dispatcher's review lane force-loads.

Unset keeps the bundled ``sdlc-review`` (upstream behaviour); a string replaces it;
empty, ``null`` or a per-reviewer mapping (a workflow plugin's routing table, which writes
the chosen skill onto the card itself) force-loads nothing. The reviewer's session start
reads the same key: ``tests/gateway/test_session_kanban_review_skill.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PLUGIN_MAPPING = {"perkins": "editorial-review", "default": "sdlc-review"}


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _dispatch_review(monkeypatch, kanban: dict, skills: list[str] | None) -> list[list[str]]:
    """Request review on a card carrying ``skills``; return the skills the review spawn saw."""
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {"kanban": {"review_dispatch": True, **kanban}})
    captured: list[list[str]] = []

    def spawn(task, _workspace):
        captured.append(list(task.skills or []))
        return 4242

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="edit the post", assignee="editor", skills=skills)
        run = kb.claim_task(conn, tid)
        assert run is not None
        assert kb.request_review(conn, tid, summary="draft ready", expected_run_id=run.current_run_id)
        monkeypatch.setattr(kbd, "check_respawn_guard", lambda *_a, **_k: None)
        kbd.dispatch_once(conn, spawn_fn=spawn)
    return captured


def test_a_writing_review_does_not_also_load_the_code_review_skill(kanban_home, monkeypatch):
    # A plugin's per-reviewer table is configured: the card's own review skill is all it loads.
    assert _dispatch_review(monkeypatch, {"review_skill": PLUGIN_MAPPING}, ["editorial-review"]) == [
        ["editorial-review"]]


def test_unset_review_skill_still_force_loads_sdlc_review(kanban_home, monkeypatch):
    assert _dispatch_review(monkeypatch, {}, ["domain-specific-review"]) == [
        ["domain-specific-review", "sdlc-review"]]


def test_the_shipped_default_is_sdlc_review():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["kanban"]["review_skill"] == "sdlc-review"


def test_the_default_config_dispatches_review_with_sdlc_review(kanban_home, monkeypatch):
    from hermes_cli.config import DEFAULT_CONFIG

    kanban = {"review_skill": DEFAULT_CONFIG["kanban"]["review_skill"]}
    assert _dispatch_review(monkeypatch, kanban, None) == [["sdlc-review"]]


def test_a_string_review_skill_replaces_the_forced_default(kanban_home, monkeypatch):
    assert _dispatch_review(monkeypatch, {"review_skill": "editorial-review"}, None) == [["editorial-review"]]


@pytest.mark.parametrize("value", ["", "   ", None])
def test_an_empty_review_skill_force_loads_nothing(kanban_home, monkeypatch, value):
    assert _dispatch_review(monkeypatch, {"review_skill": value}, ["domain-specific-review"]) == [
        ["domain-specific-review"]]
