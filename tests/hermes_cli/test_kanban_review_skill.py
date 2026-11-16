"""``kanban.review_skill``: which skill the dispatcher's review lane force-loads.

Unset keeps the bundled ``sdlc-review`` (upstream behaviour); a string replaces it; empty or
``null`` force-loads nothing; a mapping is resolved per review spawn by the reviewer's profile
(``mapping[reviewer]``, else ``mapping["default"]``, else nothing). The choice lives on the
spawn only and is never written to the card. The reviewer's session start reads the same key:
``tests/gateway/test_session_kanban_review_skill.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

ROUTING = {"editor": "editorial-review", "default": "sdlc-review"}


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _configure(monkeypatch, kanban: dict) -> list[tuple[str, list[str]]]:
    """Point the dispatcher at ``kanban`` config; return the list its spawns are recorded in."""
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {"kanban": {"review_dispatch": True, **kanban}})
    monkeypatch.setattr(kbd, "check_respawn_guard", lambda *_a, **_k: None)
    return []


def _spawn_into(captured: list):
    def spawn(task, _workspace):
        captured.append((task.assignee, list(task.skills or [])))
        return 4242
    return spawn


def _dispatch_review(monkeypatch, kanban: dict, skills: list[str] | None,
                     reviewer: str | None = None) -> list[list[str]]:
    """Request review on a card carrying ``skills``; return the skills the review spawn saw."""
    captured = _configure(monkeypatch, kanban)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="edit the post", assignee="author", skills=skills)
        run = kb.claim_task(conn, tid)
        assert run is not None
        assert kb.request_review(conn, tid, summary="draft ready", expected_run_id=run.current_run_id,
                                 reviewer=reviewer)
        kbd.dispatch_once(conn, spawn_fn=_spawn_into(captured))
    return [skills for _who, skills in captured]


def test_a_writing_review_does_not_also_load_the_code_review_skill(kanban_home, monkeypatch):
    skills = _dispatch_review(monkeypatch, {"review_skill": ROUTING}, ["editorial-review"], reviewer="editor")
    assert skills == [["editorial-review"]]


@pytest.mark.parametrize(("reviewer", "expected"), [
    ("editor", [["editorial-review"]]),
    ("coder", [["sdlc-review"]]),
])
def test_a_mapping_routes_the_native_review_by_reviewer(kanban_home, monkeypatch, reviewer, expected):
    # The card carries no skills: the reviewer's skill comes from core alone, at spawn time.
    assert _dispatch_review(monkeypatch, {"review_skill": ROUTING}, None, reviewer=reviewer) == expected


def test_a_mapping_without_a_default_force_loads_nothing_for_other_reviewers(kanban_home, monkeypatch):
    kanban = {"review_skill": {"editor": "editorial-review"}}
    assert _dispatch_review(monkeypatch, kanban, None, reviewer="coder") == [[]]


def test_a_mapping_entry_may_be_empty(kanban_home, monkeypatch):
    kanban = {"review_skill": {"editor": "", "default": "sdlc-review"}}
    assert _dispatch_review(monkeypatch, kanban, ["house-style"], reviewer="editor") == [["house-style"]]


def test_the_review_skill_stays_off_the_card_and_out_of_the_rework_spawn(kanban_home, monkeypatch):
    captured = _configure(monkeypatch, {"review_skill": ROUTING})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="edit the post", assignee="writer")
        run = kb.claim_task(conn, tid)
        assert run is not None
        assert kb.request_review(conn, tid, summary="draft ready", expected_run_id=run.current_run_id,
                                 reviewer="editor")
        kbd.dispatch_once(conn, spawn_fn=_spawn_into(captured))
        assert captured == [("editor", ["editorial-review"])]
        task = kb.get_task(conn, tid)
        assert task is not None and not task.skills
        ok, author = kb.request_changes(conn, tid, reason="tighten the lede", expected_run_id=task.current_run_id)
        assert (ok, author) == (True, "writer")
        kbd.dispatch_once(conn, spawn_fn=_spawn_into(captured))
    assert captured[1:] == [("writer", [])]


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


@pytest.mark.parametrize("value", [3, ["editorial-review"], {"editor": 3}])
def test_a_malformed_review_skill_keeps_sdlc_review(kanban_home, monkeypatch, value):
    assert _dispatch_review(monkeypatch, {"review_skill": value}, None, reviewer="editor") == [["sdlc-review"]]
