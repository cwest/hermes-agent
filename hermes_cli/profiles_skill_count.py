"""Per-profile skill counting for ``hermes profile list``/``info`` and the dashboard.

A profile's skills are its local ``skills/`` tree plus its ``skills.external_dirs`` grant, read
from THAT profile's ``config.yaml`` (the listing enumerates other profiles, so the active-home
``agent.skill_utils.get_external_skills_dirs`` is the wrong config). With a grant, entries resolve
through ``agent.skill_utils.resolve_skill_catalog`` — the same local-over-external precedence and
same-name dedup ``hermes -p <profile> skills list`` applies — so the two counts agree.
"""

import logging
import os
from pathlib import Path

from agent.skill_utils import (
    TIER_EXTERNAL,
    TIER_LOCAL,
    _config_str_list,
    _expand_path,
    iter_skill_index_files,
    parse_frontmatter,
    resolve_skill_catalog,
)
from hermes_cli.config_backend import config_cache_version_or_none, config_exists
from hermes_cli.profiles_cache import _cached_profile_read

logger = logging.getLogger(__name__)


def profile_external_skill_dirs(profile_dir: Path) -> list[Path]:
    """Existing, deduplicated ``skills.external_dirs`` of the profile at *profile_dir*. Relative
    entries anchor at that profile (its HERMES_HOME); its own ``skills/`` is skipped. A missing or
    malformed config yields ``[]`` — one bad profile must not break the listing."""
    config_path = profile_dir / "config.yaml"

    def _entries() -> tuple[str, ...]:
        try:
            # read_config()/load_config() target the ACTIVE home; read THIS profile's file raw.
            from hermes_cli.config import read_user_config_raw
            skills_cfg = read_user_config_raw(config_path).get("skills")
        except Exception:  # one profile's unreadable config must not fail the whole listing
            logger.debug("Unreadable skills.external_dirs in %s", config_path, exc_info=True)
            return ()
        return tuple(_config_str_list(skills_cfg.get("external_dirs"))) if isinstance(skills_cfg, dict) else ()

    if not config_exists(config_path):
        return []
    local = (profile_dir / "skills").resolve()
    result: list[Path] = []
    # Only the raw entries are cached (keyed on the config version): existence is re-checked per call.
    for entry in _cached_profile_read(config_path, "skills-external-dirs", _entries, config_cache_version_or_none):
        p = _expand_path(entry)
        p = (p if p.is_absolute() else profile_dir / p).resolve()
        if p != local and p not in result and p.is_dir():
            result.append(p)
    return result


def _declared_name(skill_md: Path) -> str | None:
    """Frontmatter ``name`` (else the dir name); None when the file vanished or is unreadable."""
    try:
        with open(skill_md, encoding="utf-8-sig", errors="replace") as fh:
            head = fh.read(4000)
    except OSError:
        return None
    return str(parse_frontmatter(head)[0].get("name") or skill_md.parent.name)


def count_profile_skills(profile_dir: Path) -> int:
    """Skills ``hermes -p <profile> skills list`` would show, disabled ones included."""
    local = profile_dir / "skills"
    external = profile_external_skill_dirs(profile_dir)
    if not external:  # no grant: every local SKILL.md, no frontmatter reads
        return sum(1 for _ in iter_skill_index_files(local, "SKILL.md"))
    scanned = []
    for tier, root in [(TIER_LOCAL, local), *((TIER_EXTERNAL, d) for d in external)]:
        for skill_md in iter_skill_index_files(root, "SKILL.md"):
            name = _declared_name(skill_md)
            if name is not None:
                scanned.append({"name": name, "tier": tier, "root": root, "path": skill_md})
    return sum(1 for e in resolve_skill_catalog(scanned) if e["load_name"])


def _tree_signature(root: Path) -> float:
    """Max mtime of *root* and its immediate subdirs (one scandir; a category add/remove bumps the
    root, a skill add bumps its category)."""
    try:
        sig = root.stat().st_mtime
    except OSError:
        return 0.0
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if entry.is_dir():
                        sig = max(sig, entry.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return sig


def profile_skills_signature(profile_dir: Path) -> tuple:
    """Cache signature for :func:`count_profile_skills`: the local tree, the config that names the
    grant, and each granted tree — so editing the grant or adding a shared skill invalidates it."""
    return (
        _tree_signature(profile_dir / "skills"),
        config_cache_version_or_none(profile_dir / "config.yaml"),
        *(_tree_signature(d) for d in profile_external_skill_dirs(profile_dir)),
    )
