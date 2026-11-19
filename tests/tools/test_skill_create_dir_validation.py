"""``skills.create_dir`` fails closed: a configured creation dir that is missing or not a
directory is a hard error naming the key, and nothing is written (never a silent fallback to
the profile skills dir). Unset keeps the profile-local behaviour; a name already held by a
skill in the shared dir is refused rather than shadowed by a new profile copy."""

import json
from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_hermes_home_cache", None, raising=False)

    from agent import skill_utils as su
    from tools import skills_tool
    import tools.skill_manager_tool as smt
    su._external_dirs_cache_clear()
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", home / "skills")
    # Upstream resolves the skills dir only through skills_tool; older bases also read smt's copy.
    monkeypatch.setattr(smt, "SKILLS_DIR", home / "skills", raising=False)
    yield home
    su._external_dirs_cache_clear()


def _config(home: Path, body: str) -> None:
    (home / "config.yaml").write_text(body, encoding="utf-8")
    from agent import skill_utils as su
    su._raw_config_cache_clear()
    su._external_dirs_cache_clear()


def _skill_md(name: str, body: str = "Body.") -> str:
    return (f"---\nname: {name}\ndescription: Use when testing create_dir validation. "
            f"One-line behavior.\n---\n\n# {name}\n\n{body}\n")


def _create(name: str, **kw) -> dict:
    from tools.skill_manager_tool import skill_manage
    return json.loads(skill_manage("create", name, content=_skill_md(name), **kw))


def _create_batch(name: str) -> dict:
    from tools.skill_manager_tool import skill_manage
    return json.loads(skill_manage("", "", operations=[
        {"action": "create", "name": name, "content": _skill_md(name)}]))


def _skill_mds(root: Path) -> list[Path]:
    return sorted(root.rglob("SKILL.md")) if root.exists() else []


def test_unset_key_creates_in_the_profile_dir(home):
    _config(home, "skills:\n  external_dirs: []\n")
    res = _create("local-one")
    assert res["success"], res
    assert (home / "skills" / "local-one" / "SKILL.md").is_file()


def test_set_key_creates_in_the_shared_dir_not_the_profile(home, tmp_path):
    shared = tmp_path / "shared-skills"
    shared.mkdir()
    _config(home, f"skills:\n  create_dir: {shared}\n  external_dirs:\n    - {shared}\n")
    res = _create("shared-one", category="devops")
    assert res["success"], res
    assert (shared / "devops" / "shared-one" / "SKILL.md").is_file()
    assert _skill_mds(home / "skills") == []


@pytest.mark.parametrize("via", [_create, _create_batch], ids=["single", "batch"])
def test_missing_create_dir_is_an_error_and_nothing_is_written(home, tmp_path, via):
    missing = tmp_path / "not-there"
    _config(home, f"skills:\n  create_dir: {missing}\n")
    res = via("orphan")
    assert res["success"] is False, res
    assert "skills.create_dir" in res["error"]
    assert str(missing) in res["error"]
    assert not missing.exists()
    assert _skill_mds(home / "skills") == []


@pytest.mark.parametrize("via", [_create, _create_batch], ids=["single", "batch"])
def test_create_dir_that_is_a_file_is_an_error(home, tmp_path, via):
    a_file = tmp_path / "skills-file"
    a_file.write_text("not a dir\n", encoding="utf-8")
    _config(home, f"skills:\n  create_dir: {a_file}\n")
    res = via("orphan")
    assert res["success"] is False, res
    assert "skills.create_dir" in res["error"]
    assert a_file.read_text(encoding="utf-8") == "not a dir\n"
    assert _skill_mds(home / "skills") == []


def test_collision_with_a_shared_skill_is_refused_not_shadowed(home, tmp_path):
    shared = tmp_path / "shared-skills"
    (shared / "ops" / "taken").mkdir(parents=True)
    original = _skill_md("taken", "Shared copy.")
    (shared / "ops" / "taken" / "SKILL.md").write_text(original, encoding="utf-8")
    _config(home, f"skills:\n  create_dir: {shared}\n  external_dirs:\n    - {shared}\n")
    res = _create("taken")
    assert res["success"] is False, res
    assert "already exists" in res["error"]
    assert (shared / "ops" / "taken" / "SKILL.md").read_text(encoding="utf-8") == original
    assert _skill_mds(home / "skills") == []
    assert _skill_mds(shared) == [shared / "ops" / "taken" / "SKILL.md"]
