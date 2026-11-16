"""``kanban.review_skill`` at the reviewer's session start (``build_kanban_policy``).

The session rebuilds the review run's skills from the card, so it must apply the same
forced-skill rule as the dispatcher claim: unset keeps ``sdlc-review``, a string replaces
it, and a mapping is resolved by the reviewer's profile (the task's assignee).
"""

from __future__ import annotations

import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

ROUTING = {"editor": "editorial-review", "default": "sdlc-review"}


def _review_policy_skills(tmp_path, monkeypatch, config: dict, *, reviewer: str = "default",
                          skills: list[str] | None = None) -> list[str]:
    """Claim a card for ``reviewer``'s review and return the skills its session starts with."""
    from gateway.session_contract import SessionActor
    from gateway.session_kanban import build_kanban_policy

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD", raising=False)
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env",
                        lambda name: str(tmp_path) if name == reviewer else str(tmp_path / name))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kb.create_board("owned")
    with closing(kbc.connect(board="owned")) as conn:
        tid = kb.create_task(conn, title="review the post", assignee=reviewer, workspace_kind="dir",
                             workspace_path=str(workspace), skills=skills)
        kb.recompute_ready(conn)
        run = kb.claim_task(conn, tid)
        assert run is not None
        assert kb.request_review(conn, tid, summary="draft ready", expected_run_id=run.current_run_id)
        task = kb.claim_review_task(conn, tid)
        assert task is not None
    actor = SessionActor("owner", str(tmp_path), frozenset({"session:create"}), "transport")
    connection = SimpleNamespace(authority=SimpleNamespace(profile_id=str(tmp_path)), actor=actor, native_owner=True)
    params = dict(board="owned", task_id=task.id, run_id=task.current_run_id, claim_lock=task.claim_lock)
    config = {"model": {"default": "m", "provider": "custom"}, "platform_toolsets": {"cli": ["terminal"]}, **config}
    policy, _secrets = build_kanban_policy(connection, params, config)
    return json.loads(policy.kanban_json)["skills"]


def test_a_reviewer_session_without_the_key_loads_sdlc_review(tmp_path, monkeypatch):
    assert _review_policy_skills(tmp_path, monkeypatch, {}, skills=["editorial-review"]) == [
        "editorial-review", "sdlc-review"]


@pytest.mark.parametrize(("reviewer", "expected"), [
    ("editor", ["editorial-review"]),
    ("coder", ["sdlc-review"]),
])
def test_a_reviewer_session_resolves_a_mapping_by_its_profile(tmp_path, monkeypatch, reviewer, expected):
    config = {"kanban": {"review_skill": ROUTING}}
    assert _review_policy_skills(tmp_path, monkeypatch, config, reviewer=reviewer) == expected


def test_a_reviewer_session_honours_a_string_review_skill(tmp_path, monkeypatch):
    skills = _review_policy_skills(tmp_path, monkeypatch, {"kanban": {"review_skill": "house-review"}},
                                   skills=["editorial-review"])
    assert skills == ["editorial-review", "house-review"]


def test_a_reviewer_session_with_an_empty_review_skill_force_loads_nothing(tmp_path, monkeypatch):
    skills = _review_policy_skills(tmp_path, monkeypatch, {"kanban": {"review_skill": ""}},
                                   skills=["editorial-review"])
    assert skills == ["editorial-review"]
