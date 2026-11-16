"""Worktree branch names must be human-meaningful, never a bare task id.

Regression cover for the opaque ``t-39521e0e`` branch: a card that is not
project-linked used to fall through to an id-only branch, which is unreadable in
a branch list or a preview dashboard. The name must always carry the card's
title when one exists, and never the raw task id.
"""

from hermes_cli.kanban_db import _derive_worktree_branch_name, _is_auto_derived_branch


def test_title_is_slugged_into_the_branch_name():
    name = _derive_worktree_branch_name(
        "t_39521e0e", "Add the unified design system"
    )
    assert name == "topic/add-the-unified-design-system"


def test_branch_name_never_carries_the_task_id_when_a_title_exists():
    name = _derive_worktree_branch_name("t_39521e0e", "Fix the login redirect")
    assert "t_39521e0e" not in name
    assert "39521e0e" not in name
    assert "fix-the-login-redirect" in name


def test_punctuation_and_case_are_normalized_to_a_safe_ref():
    name = _derive_worktree_branch_name(
        "t_abc123", "fix(kanban): Give Repos A *Real* Path!"
    )
    # Git refs: no spaces, parens, colons, asterisks, or uppercase.
    assert name == "fix/give-repos-a-real-path"


def test_long_titles_are_truncated_without_a_trailing_separator():
    long_title = "a" * 200
    name = _derive_worktree_branch_name("t_abc123", long_title)
    assert len(name) <= 63
    assert not name.endswith("-")


def test_empty_or_unsluggable_title_falls_back_without_an_underscore():
    for title in ("", None, "!!!"):
        name = _derive_worktree_branch_name("t_abc123", title)
        assert name == "topic/work-abc123"
        assert "_" not in name


def test_auto_derived_shapes_are_not_declarations():
    tid, title = "t_abc12345", "fix: the login redirect"
    base = _derive_worktree_branch_name(tid, title)
    # The descriptive base, its own collision suffixes, and the legacy shape
    # persisted on older cards are all dispatcher-generated.
    assert _is_auto_derived_branch(tid, title, base)
    assert _is_auto_derived_branch(tid, title, f"{base}-abc123")
    assert _is_auto_derived_branch(tid, title, f"{base}-abc123-2")
    assert _is_auto_derived_branch(tid, title, f"wt/{tid}")
    assert _is_auto_derived_branch(tid, title, f"wt/{tid}-fix-the-login-redirect")


def test_caller_supplied_branches_are_declarations():
    tid, title = "t_abc12345", "fix: the login redirect"
    assert not _is_auto_derived_branch(tid, title, "topic/obtainability-series")
    assert not _is_auto_derived_branch(tid, title, "fix/some-other-pr")
    # Another task's suffix on the same base is not ours.
    base = _derive_worktree_branch_name(tid, title)
    assert not _is_auto_derived_branch(tid, title, f"{base}-ffff00")
    assert not _is_auto_derived_branch(tid, title, "")
