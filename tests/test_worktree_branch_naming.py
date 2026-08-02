"""Worktree branch names must be human-meaningful, never a bare task id.

Regression cover for the opaque ``t-39521e0e`` branch: a card that is not
project-linked used to fall through to ``wt/<task-id>``, which is unreadable in
a branch list or a preview dashboard. The name must always carry the card's
title when one exists.
"""

from hermes_cli.kanban_db import _derive_worktree_branch_name


def test_title_is_slugged_into_the_branch_name():
    name = _derive_worktree_branch_name(
        "t_39521e0e", "Add the unified design system"
    )
    assert name == "wt/t_39521e0e-add-the-unified-design-system"


def test_branch_name_is_never_the_bare_task_id_when_a_title_exists():
    name = _derive_worktree_branch_name("t_39521e0e", "Fix the login redirect")
    assert name != "wt/t_39521e0e"
    assert "fix-the-login-redirect" in name


def test_punctuation_and_case_are_normalized_to_a_safe_ref():
    name = _derive_worktree_branch_name(
        "t_abc123", "fix(kanban): Give Repos A *Real* Path!"
    )
    # Git refs: no spaces, parens, colons, asterisks, or uppercase.
    assert name == "wt/t_abc123-fix-kanban-give-repos-a-real-path"


def test_long_titles_are_truncated_without_a_trailing_separator():
    long_title = "a" * 200
    name = _derive_worktree_branch_name("t_abc123", long_title)
    slug = name.split("t_abc123-", 1)[1]
    assert len(slug) <= 40
    assert not name.endswith("-")


def test_empty_or_unsluggable_title_falls_back_to_the_task_id():
    # Nothing meaningful to say — the bare id is correct here, not a dangling
    # separator.
    assert _derive_worktree_branch_name("t_abc123", "") == "wt/t_abc123"
    assert _derive_worktree_branch_name("t_abc123", None) == "wt/t_abc123"
    assert _derive_worktree_branch_name("t_abc123", "!!!") == "wt/t_abc123"
