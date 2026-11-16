"""Per-profile skill counts include the profile's ``skills.external_dirs`` grant.

``hermes profile list`` / ``profile info`` / the dashboard count must match what
``hermes -p <profile> skills list`` shows: a profile that sources its skills from a shared
external grant (no local ``skills/``) reports them, not 0, and an external copy shadowed by a
local skill of the same name is counted once.
"""

import pytest

from hermes_cli import profiles


def _skill(root, name, *, declared=None):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {declared or name}\ndescription: {name}.\n---\nbody\n", encoding="utf-8")


def _grant(profile_dir, *entries):
    profile_dir.mkdir(parents=True, exist_ok=True)
    lines = "".join(f"    - {e}\n" for e in entries)
    (profile_dir / "config.yaml").write_text(f"skills:\n  external_dirs:\n{lines}", encoding="utf-8")


@pytest.fixture(autouse=True)
def _cold_cache(monkeypatch):
    monkeypatch.setattr(profiles, "_SKILL_COUNT_CACHE", {})
    monkeypatch.setattr(profiles, "_SKILL_COUNT_SCAN_LOCKS", {})


def test_external_only_profile_counts_its_grant(tmp_path):
    shared = tmp_path / "shared"
    for n in ("alpha", "beta", "gamma"):
        _skill(shared, n)
    profile = tmp_path / "profiles" / "worker"
    _grant(profile, shared)

    assert profiles._count_skills(profile) == 3


def test_local_skill_shadows_external_copy_of_same_name(tmp_path):
    shared = tmp_path / "shared"
    _skill(shared, "dup")
    _skill(shared, "ext-only")
    profile = tmp_path / "profiles" / "worker"
    _skill(profile / "skills", "dup-local-dir", declared="dup")
    _skill(profile / "skills", "local-only")
    _grant(profile, shared)

    assert profiles._count_skills(profile) == 3  # dup (once) + ext-only + local-only


def test_relative_grant_resolves_against_the_profile_not_the_active_home(tmp_path):
    profile = tmp_path / "profiles" / "worker"
    _skill(profile / "grant", "rel")
    _grant(profile, "grant")

    assert profiles._count_skills(profile) == 1


def test_missing_or_unusable_grant_counts_local_only(tmp_path):
    profile = tmp_path / "profiles" / "worker"
    _skill(profile / "skills", "a")
    _grant(profile, tmp_path / "does-not-exist", profile / "skills")
    assert profiles._count_skills(profile) == 1

    (profile / "config.yaml").write_text("skills: [not, a, mapping]\n", encoding="utf-8")
    profiles._SKILL_COUNT_CACHE.clear()
    assert profiles._count_skills(profile) == 1

    (profile / "config.yaml").write_text("skills: {external_dirs: [unclosed\n", encoding="utf-8")
    profiles._SKILL_COUNT_CACHE.clear()
    assert profiles._count_skills(profile) == 1

    bare = tmp_path / "profiles" / "bare"
    bare.mkdir(parents=True)
    assert profiles._count_skills(bare) == 0


def test_skill_added_to_the_grant_refreshes_a_cached_count(tmp_path):
    shared = tmp_path / "shared"
    _skill(shared / "cat", "one")
    profile = tmp_path / "profiles" / "worker"
    _grant(profile, shared)
    assert profiles._count_skills(profile) == 1

    _skill(shared / "cat", "two")
    assert profiles._count_skills(profile) == 2
