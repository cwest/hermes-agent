"""Descriptive kebab-case branch + worktree-directory naming.

The dispatcher used to name every worktree task's branch ``wt/<task-id>`` (or
``wt/<task-id>-<title-slug>`` truncated at 40 chars, mid-word) and its on-disk
worktree directory the bare task id. Both violate Casey's descriptive-kebab-case
rule and, because the blog preview supervisor uses the branch's last ref segment
as a ``<label>.blog.office.caseywest.com`` DNS label, both leak the opaque
``t_<id>`` prefix and mid-word truncation into every preview hostname.

These tests pin the new contract: a branch derived from the card title, a
Conventional-type namespace, word-boundary truncation under the 63-char DNS
label cap, no underscore / raw task id in the human-facing portion, and a
descriptive worktree directory name.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# RFC-1123 single label: lowercase alnum, internal hyphens, 1..63 chars.
_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_DNS_LABEL_MAX = 63


def _last_segment(branch: str) -> str:
    """The subdomain label the blog supervisor derives (``rsplit('/', 1)[-1]``)."""
    return branch.rsplit("/", 1)[-1]


def _assert_valid_dns_label(label: str) -> None:
    assert label, "empty label is not routable"
    assert len(label) <= _DNS_LABEL_MAX, f"label too long: {label!r}"
    assert _DNS_LABEL_RE.match(label), f"not an RFC-1123 label: {label!r}"


# --------------------------------------------------------------------------
# _derive_worktree_branch_name — pure derivation
# --------------------------------------------------------------------------

def test_conventional_type_becomes_branch_namespace():
    b = kb._derive_worktree_branch_name(
        "t_28dc7ee2", "fix(post): cut the false claim"
    )
    assert b.split("/", 1)[0] == "fix"
    # No wt/ prefix, no raw task id, no underscore anywhere.
    assert not b.startswith("wt/")
    assert "t_28dc7ee2" not in b
    assert "_" not in b
    # The descriptive tail is present and readable.
    assert "cut-the-false-claim" in b


@pytest.mark.parametrize(
    "title,expected_ns",
    [
        ("fix: a bug", "fix"),
        ("feat: a feature", "feat"),
        ("feat(blog): scoped feature", "feat"),
        ("content: a post", "content"),
        ("chore: housekeeping", "chore"),
        ("docs: write it up", "docs"),
        ("refactor: tidy", "refactor"),
        # No recognizable type → the topic/ namespace (matches hand-authored
        # branches like topic/unlisted-post-flag).
        ("just do the thing", "topic"),
        ("WIP something odd", "topic"),
    ],
)
def test_namespace_mapping(title, expected_ns):
    b = kb._derive_worktree_branch_name("t_00000001", title)
    assert b.split("/", 1)[0] == expected_ns


def test_no_underscore_or_raw_id_in_human_portion():
    b = kb._derive_worktree_branch_name(
        "t_64416edf", "feat(blog): build the accelerator schedule"
    )
    assert "_" not in b
    assert "t_64416edf" not in b
    assert "64416edf" not in b


def test_result_is_a_valid_dns_label_end_to_end():
    # Whole branch stays under the cap AND the last segment is a valid label.
    b = kb._derive_worktree_branch_name(
        "t_a627ab05",
        "content(blog): add the hiring manager voice to the pipeline post",
    )
    assert len(b) <= _DNS_LABEL_MAX
    _assert_valid_dns_label(_last_segment(b))


def test_long_title_truncates_on_word_boundary_not_mid_word():
    title = (
        "fix: correct the extremely verbose and rambling description that keeps "
        "going well past any reasonable single label length limit"
    )
    b = kb._derive_worktree_branch_name("t_deadbeef", title)
    assert len(b) <= _DNS_LABEL_MAX
    tail = _last_segment(b)
    _assert_valid_dns_label(tail)
    # Word-boundary cut: every hyphen-separated token must be a whole word from
    # the (slugified) title — no token is a truncated fragment of a title word.
    title_words = set(re.sub(r"[^a-z0-9]+", " ", title.lower()).split())
    for token in tail.split("-"):
        if token in {"fix"}:  # namespace strays only into the prefix, not tail
            continue
        assert token in title_words, f"mid-word fragment in tail: {token!r}"


def test_empty_slug_falls_back_without_underscore_or_bare_id():
    # A title that slugs away to nothing (punctuation only) still yields a valid,
    # underscore-free, DNS-label-safe branch — the descriptive replacement for
    # the old bare-id fallback.
    b = kb._derive_worktree_branch_name("t_7fd7f2a2", "!!! ??? ...")
    assert "_" not in b
    assert not b.startswith("wt/")
    _assert_valid_dns_label(_last_segment(b))


def test_scope_only_title_still_produces_readable_tail():
    # "feat(blog):" with an empty description after the colon.
    b = kb._derive_worktree_branch_name("t_11112222", "feat(blog):")
    assert b.split("/", 1)[0] == "feat"
    _assert_valid_dns_label(_last_segment(b))


# --------------------------------------------------------------------------
# Collision handling — only disambiguate when the branch already exists
# --------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(r)], check=True, capture_output=True, text=True)
    (r / "README.md").write_text("base\n", encoding="utf-8")
    _git(r, "add", "README.md")
    _git(r, "commit", "-m", "init")
    return r


def test_no_collision_keeps_clean_name(repo):
    b = kb._derive_worktree_branch_name(
        "t_aaaa1111", "fix: post cut the false claim", repo_root=repo
    )
    # Nothing named that yet → clean, no disambiguating suffix.
    assert b == "fix/post-cut-the-false-claim"


def test_collision_disambiguates_only_on_conflict(repo):
    # Pre-create the clean branch so the derivation must disambiguate.
    _git(repo, "branch", "fix/post-cut-the-false-claim")
    b = kb._derive_worktree_branch_name(
        "t_bbbb2222", "fix: post cut the false claim", repo_root=repo
    )
    assert b != "fix/post-cut-the-false-claim"
    assert b.startswith("fix/post-cut-the-false-claim")
    _assert_valid_dns_label(_last_segment(b))
    assert len(b) <= _DNS_LABEL_MAX
    # The disambiguated branch itself does not exist yet.
    assert not kb._git_branch_exists(repo, b)


def test_derivation_is_deterministic():
    a = kb._derive_worktree_branch_name("t_abc", "feat: a stable feature")
    b = kb._derive_worktree_branch_name("t_abc", "feat: a stable feature")
    assert a == b


# --------------------------------------------------------------------------
# Worktree DIRECTORY naming — descriptive, not the bare task id
# --------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_worktree_directory_is_descriptive_not_bare_id(kanban_home, repo, monkeypatch):
    monkeypatch.chdir(repo)
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="fix: post cut the false claim",
            workspace_kind="worktree",
        )
        # Anchor the worktree on the repo (board default_workdir path).
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(repo), tid),
        )
        conn.commit()
        task = kb.get_task(conn, tid)
        # Give it the descriptive branch the dispatcher would assign.
        branch = kb._derive_worktree_branch_name(tid, task.title, repo_root=repo)
        task.branch_name = branch

        target, resolved_branch = kb._resolve_worktree_workspace(task)

    leaf = Path(target).name
    # The directory reads as prose, not the opaque task id.
    assert leaf != tid
    assert tid not in leaf
    assert "_" not in leaf
    assert leaf == _last_segment(resolved_branch)
    # And the checkout actually exists on the right branch.
    assert Path(target).is_dir()
    cur = subprocess.run(
        ["git", "-C", str(target), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert cur == resolved_branch


def test_two_tasks_same_title_get_distinct_directories(kanban_home, repo, monkeypatch):
    monkeypatch.chdir(repo)
    targets = []
    with kb.connect() as conn:
        for _ in range(2):
            tid = kb.create_task(
                conn, title="fix: same exact title", workspace_kind="worktree"
            )
            conn.execute(
                "UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(repo), tid)
            )
            conn.commit()
            task = kb.get_task(conn, tid)
            task.branch_name = kb._derive_worktree_branch_name(
                tid, task.title, repo_root=repo
            )
            target, _b = kb._resolve_worktree_workspace(task)
            targets.append(Path(target).name)
    # Distinct on-disk directories even though the titles match.
    assert targets[0] != targets[1]


# --------------------------------------------------------------------------
# Card <-> PR resolution is independent of the branch shape
# --------------------------------------------------------------------------

def test_card_pr_resolution_survives_the_rename():
    """PR->card resolution uses the ``<!-- card:t_... -->`` body marker, not the
    branch name. A descriptive branch with no ``wt/t_<id>`` prefix must still be
    resolvable back to its card through that marker path."""
    from hermes_cli import kanban_db as _kb  # noqa: F401  (import-safety)

    tid = "t_28dc7ee2"
    branch = kb._derive_worktree_branch_name(tid, "fix(post): cut the false claim")
    # The branch deliberately does NOT contain the task id...
    assert tid not in branch
    # ...but the PR body marker does, and that is what resolution reads.
    marker = f"<!-- card:{tid} -->"
    pr_body = f"Some description.\n\n{marker}\n"
    m = re.search(r"<!--\s*card:(t_[0-9a-f]+)\s*-->", pr_body)
    assert m is not None
    assert m.group(1) == tid
