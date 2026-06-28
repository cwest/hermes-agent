"""Worker-spawn git identity pinning.

A dispatched kanban worker clones/branches a repo and commits its work. The
canonical author for ALL such commits is the repo maintainer; a worker must
never author commits under a per-profile identity. When a per-profile identity
leaks into a branch commit, a squash-merge auto-appends a ``Co-authored-by:``
trailer for that distinct author into the merge commit body -- which is exactly
the metadata leak this pin exists to prevent.

These tests assert that ``_default_spawn`` pins ``GIT_AUTHOR_*`` /
``GIT_COMMITTER_*`` into the worker subprocess env, resolved from the host git
config, so every worker commit is authored as the maintainer regardless of what
identity the worker's runtime would otherwise synthesize. Env-level git
identity overrides repo-local and global config, so the worker cannot author
under any other name.
"""

from __future__ import annotations

import subprocess


def _make_task(kb, *, assignee: str):
    return kb.Task(
        id="t_git_identity",
        title="git identity",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )


def _spawn_and_capture(kb, monkeypatch, tmp_path, *, assignee="elias"):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / assignee
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "toolsets:\n  - hermes-cli\n", encoding="utf-8"
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pid = kb._default_spawn(_make_task(kb, assignee=assignee), str(workspace))
    assert pid == 4242
    return captured


def test_default_spawn_pins_git_identity_env(monkeypatch, tmp_path):
    """The worker env carries GIT_AUTHOR_* / GIT_COMMITTER_* resolved from the
    host git identity, so every worker commit is authored as the maintainer."""
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(
        kb,
        "_resolve_worker_git_identity",
        lambda: ("Casey West", "casey@geeknest.com"),
    )

    captured = _spawn_and_capture(kb, monkeypatch, tmp_path)
    env = captured["env"]

    assert env["GIT_AUTHOR_NAME"] == "Casey West"
    assert env["GIT_AUTHOR_EMAIL"] == "casey@geeknest.com"
    assert env["GIT_COMMITTER_NAME"] == "Casey West"
    assert env["GIT_COMMITTER_EMAIL"] == "casey@geeknest.com"


def test_default_spawn_omits_git_identity_when_unresolvable(monkeypatch, tmp_path):
    """If the host git identity can't be resolved, the spawn must NOT crash and
    must NOT inject partial/empty identity env vars (which would author commits
    as an empty name/email). It simply omits them and lets git's own config
    chain apply."""
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_worker_git_identity", lambda: None)

    captured = _spawn_and_capture(kb, monkeypatch, tmp_path)
    env = captured["env"]

    assert "GIT_AUTHOR_NAME" not in env
    assert "GIT_AUTHOR_EMAIL" not in env
    assert "GIT_COMMITTER_NAME" not in env
    assert "GIT_COMMITTER_EMAIL" not in env


def test_resolve_worker_git_identity_reads_git_config(monkeypatch, tmp_path):
    """_resolve_worker_git_identity returns (name, email) from `git config`
    when both are present."""
    from hermes_cli import kanban_db as kb

    def fake_run(args, *a, **k):
        class R:
            returncode = 0
            stdout = (
                "Casey West\n" if args[-1] == "user.name" else "casey@geeknest.com\n"
            )

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)

    ident = kb._resolve_worker_git_identity()
    assert ident == ("Casey West", "casey@geeknest.com")


def test_resolve_worker_git_identity_none_when_missing(monkeypatch, tmp_path):
    """When git config has no user.name/user.email (empty output), resolution
    returns None rather than empty strings."""
    from hermes_cli import kanban_db as kb

    def fake_run(args, *a, **k):
        class R:
            returncode = 0
            stdout = "\n"

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert kb._resolve_worker_git_identity() is None
