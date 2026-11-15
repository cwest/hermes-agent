"""Archive guards for the curator's prune paths.

Two classes of skill must never be archived by the curator, on any path
(the automatic inactivity walk, ``hermes curator prune``, or a direct
``archive_skill`` call):

* bundled built-ins and hub-installed skills — the same provenance test
  ``hermes curator pin`` applies, regardless of any config flag;
* skills a non-terminal kanban card force-loads via its ``skills`` field —
  archiving one makes the card's next worker spawn fail with
  "Unknown skill(s)".
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # A user config that explicitly opts into the legacy built-in prune —
    # the exact shape of the install that lost its bundled skills.
    (home / "config.yaml").write_text(
        "curator:\n  prune_builtins: true\n", encoding="utf-8",
    )

    import tools.skill_usage as usage
    importlib.reload(usage)
    import agent.curator as curator
    importlib.reload(curator)
    monkeypatch.setattr(curator, "_run_llm_review", lambda prompt: "llm-stub")
    monkeypatch.setattr(curator, "_cron_referenced_skills", lambda: set())

    from hermes_cli import kanban_db as kb
    kb.init_db()
    return SimpleNamespace(home=home, skills=home / "skills", usage=usage,
                           curator=curator, kb=kb)


def _write_skill(skills_dir: Path, name: str, category: str = "") -> Path:
    d = skills_dir / category / name if category else skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: x\n---\n", encoding="utf-8",
    )
    return d


def _age(usage, name: str, *, days: int = 500, agent: bool = False) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    data = usage.load_usage()
    rec = usage._empty_record()
    rec["created_at"] = old
    rec["last_used_at"] = old
    rec["use_count"] = 1
    if agent:
        rec["created_by"] = "agent"
    data[name] = rec
    usage.save_usage(data)


def _bundle(env, name: str, category: str = "github") -> Path:
    d = _write_skill(env.skills, name, category)
    manifest = env.skills / ".bundled_manifest"
    prior = manifest.read_text(encoding="utf-8") if manifest.exists() else ""
    manifest.write_text(prior + f"{name}:abc\n", encoding="utf-8")
    _age(env.usage, name)
    return d


def _card(env, skills, *, status=None) -> str:
    with env.kb.connect_closing() as conn:
        tid = env.kb.create_task(conn, title="t", assignee="worker", skills=skills,
                                 detached=True)
        if status is not None:
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
            conn.commit()
    return tid


def _prune_cli(days: int = 90) -> int:
    import hermes_cli.curator as curator_cli
    return curator_cli._cmd_prune(SimpleNamespace(days=days, yes=True, dry_run=False))


# ─── bundled / hub provenance ──────────────────────────────────────────────


def test_auto_transitions_never_archive_idle_bundled_skill(env):
    d = _bundle(env, "github-code-review")

    counts = env.curator.apply_automatic_transitions()

    assert counts["archived"] == 0
    assert d.exists()
    assert "github-code-review" not in env.usage.read_suppressed_names()
    assert "github-code-review" not in env.usage.list_agent_created_skill_names()


def test_cli_prune_never_archives_idle_bundled_skill(env, capsys):
    d = _bundle(env, "github-code-review")

    assert _prune_cli() == 0

    assert d.exists()
    assert "github-code-review" not in capsys.readouterr().out


def test_archive_skill_refuses_bundled_even_with_prune_builtins_config(env):
    d = _bundle(env, "github-code-review")

    ok, msg = env.usage.archive_skill("github-code-review")

    assert ok is False
    assert "bundled" in msg
    assert d.exists()
    assert env.usage.is_curation_eligible("github-code-review") is False


def test_pin_and_prune_agree_on_provenance(env):
    """Anything `pin` refuses as bundled/hub is never a prune candidate."""
    _bundle(env, "bundled-one")
    _write_skill(env.skills, "hub-one")
    hub = env.skills / ".hub"
    hub.mkdir()
    (hub / "lock.json").write_text(
        '{"version": 1, "installed": {"hub-one": {"install_path": "hub-one"}}}',
        encoding="utf-8",
    )
    _age(env.usage, "hub-one")
    _write_skill(env.skills, "mine")
    _age(env.usage, "mine", agent=True)

    candidates = set(env.usage.list_agent_created_skill_names())
    for name in ("bundled-one", "hub-one", "mine"):
        assert (name in candidates) == env.usage.is_agent_created(name), name


def test_restore_still_recovers_a_previously_pruned_bundled_skill(env):
    """Built-ins archived before the guard existed must stay restorable."""
    _bundle(env, "github-auth")
    src = env.skills / "github" / "github-auth"
    archive = env.skills / ".archive"
    archive.mkdir()
    src.rename(archive / "github-auth")
    env.usage.add_suppressed_name("github-auth")

    ok, msg = env.usage.restore_skill("github-auth")

    assert ok, msg
    assert (env.skills / "github-auth" / "SKILL.md").exists()
    assert "github-auth" not in env.usage.read_suppressed_names()


# ─── open kanban card references ───────────────────────────────────────────


def test_auto_transitions_skip_skill_pinned_on_open_card(env):
    d = _write_skill(env.skills, "card-skill")
    _age(env.usage, "card-skill", agent=True)
    _card(env, ["card-skill"])

    counts = env.curator.apply_automatic_transitions()

    assert counts["archived"] == 0
    assert d.exists()


def test_cli_prune_skips_skill_pinned_on_open_card(env, capsys):
    d = _write_skill(env.skills, "card-skill")
    _age(env.usage, "card-skill", agent=True)
    _card(env, ["card-skill"], status="blocked")

    assert _prune_cli() == 0

    assert d.exists()
    assert "card-skill" not in capsys.readouterr().out


@pytest.mark.parametrize("status", ["done", "archived"])
def test_terminal_card_reference_does_not_protect(env, status):
    d = _write_skill(env.skills, "old-skill")
    _age(env.usage, "old-skill", agent=True)
    _card(env, ["old-skill"], status=status)

    counts = env.curator.apply_automatic_transitions()

    assert counts["archived"] == 1
    assert not d.exists()


def test_cli_prune_skips_cron_referenced_skill(env, monkeypatch, capsys):
    """`hermes curator prune` honors the same reference set as the auto walk."""
    d = _write_skill(env.skills, "cron-skill")
    _age(env.usage, "cron-skill", agent=True)
    monkeypatch.setattr(env.curator, "_cron_referenced_skills", lambda: {"cron-skill"})

    assert _prune_cli() == 0

    assert d.exists()


def test_open_card_skill_names_spans_boards_and_strips_slash(env):
    kb = env.kb
    _card(env, ["/lead-slash", "plain"])
    kb.create_board("other")
    with kb.connect_closing(board="other") as conn:
        kb.create_task(conn, title="t", assignee="w", skills=["other-board"],
                       detached=True)

    assert {"lead-slash", "plain", "other-board"} <= kb.open_card_skill_names()


def test_candidate_list_marks_card_referenced_skills(env):
    _write_skill(env.skills, "card-skill")
    _age(env.usage, "card-skill", agent=True)
    _card(env, ["card-skill"])

    rendered = env.curator._render_candidate_list()

    assert "card-skill" in rendered
    line = next(l for l in rendered.splitlines() if "card-skill" in l)
    assert "card=yes" in line
