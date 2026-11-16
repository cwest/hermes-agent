"""``kanban.review_skill`` at the reviewer's session start (``build_kanban_policy``).

The session rebuilds the review run's skills from the card, so it must apply the same
forced-skill rule as the dispatcher claim: unset keeps ``sdlc-review``, a string replaces
it, and a plugin's per-reviewer mapping force-loads nothing.
"""

from __future__ import annotations

import json
from contextlib import closing
from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

PLUGIN_MAPPING = {"perkins": "editorial-review", "default": "sdlc-review"}


def _review_policy_skills(tmp_path, monkeypatch, config: dict) -> list[str]:
    """Claim a card for review and return the skills its reviewer session starts with."""
    from gateway.session_contract import SessionActor
    from gateway.session_kanban import build_kanban_policy

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD", raising=False)
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env",
                        lambda name: str(tmp_path) if name == "default" else str(tmp_path / name))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kb.create_board("owned")
    with closing(kbc.connect(board="owned")) as conn:
        tid = kb.create_task(conn, title="review the post", assignee="default", workspace_kind="dir",
                             workspace_path=str(workspace), skills=["editorial-review"])
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
    assert _review_policy_skills(tmp_path, monkeypatch, {}) == ["editorial-review", "sdlc-review"]


def test_a_reviewer_session_honours_a_plugin_mapping(tmp_path, monkeypatch):
    skills = _review_policy_skills(tmp_path, monkeypatch, {"kanban": {"review_skill": PLUGIN_MAPPING}})
    assert skills == ["editorial-review"]


def test_a_reviewer_session_honours_a_string_review_skill(tmp_path, monkeypatch):
    skills = _review_policy_skills(tmp_path, monkeypatch, {"kanban": {"review_skill": "house-review"}})
    assert skills == ["editorial-review", "house-review"]


def test_a_reviewer_session_with_an_empty_review_skill_force_loads_nothing(tmp_path, monkeypatch):
    skills = _review_policy_skills(tmp_path, monkeypatch, {"kanban": {"review_skill": ""}})
    assert skills == ["editorial-review"]
