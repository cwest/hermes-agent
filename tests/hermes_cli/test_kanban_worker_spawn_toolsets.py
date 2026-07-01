from __future__ import annotations

import subprocess


def _make_task(kb, *, assignee: str):
    return kb.Task(
        id="t_spawn_tools",
        title="spawn tools",
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


def test_default_spawn_pins_assignee_profile_cli_toolsets(monkeypatch, tmp_path):
    """Manual profile assignment should keep that profile's CLI tools.

    Regression guard for dispatcher-spawned workers that boot with
    HERMES_KANBAN_TASK: the worker must not collapse to only kanban lifecycle
    tools when the assigned profile's top-level ``toolsets`` is the default
    composite. The spawned CLI gets an explicit --toolsets pin resolved from
    platform_toolsets.cli; model_tools appends task-scoped kanban tools later.
    """
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - clarify
    - code_execution
    - delegation
    - file
    - memory
    - session_search
    - skills
    - terminal
    - web
toolsets:
  - hermes-cli
agent:
  disabled_toolsets: []
""".lstrip(),
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pid = kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))

    assert pid == 4242
    assert captured["env"]["HERMES_HOME"] == str(profile)
    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_spawn_tools"
    assert "--toolsets" in captured["cmd"]
    pinned = captured["cmd"][captured["cmd"].index("--toolsets") + 1].split(",")
    for required in ("terminal", "web", "file", "skills", "code_execution", "delegation"):
        assert required in pinned


def test_resolve_worker_cli_toolsets_uses_profile_home_not_parent_config(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("platform_toolsets:\n  cli:\n    - kanban\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(str(profile))

    assert resolved is not None
    assert "terminal" in resolved
    assert "web" in resolved
    assert "kanban" in resolved  # recovered worker lifecycle surface
    assert resolved != ["kanban"]


def test_default_spawn_raises_when_toolset_resolution_degenerate(monkeypatch, tmp_path):
    """A worker must NEVER spawn with a degenerate (None/empty) toolset.

    Core regression guard for the 2026-06-26 burst incident: under a
    stuck->mass-spawn recovery, ``_resolve_worker_cli_toolsets`` came up empty
    and the spawn path silently launched workers with only ``kanban_*`` tools,
    which then self-blocked ("only kanban_* coordination tools"). The spawn
    must instead FAIL loudly so ``dispatch_once`` reclaims the card for a clean
    retry rather than burning an LLM cycle on a crippled worker.
    """
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "salton"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "toolsets:\n  - hermes-cli\n  - terminal\n  - web\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    # Simulate the burst-race failure mode: resolution comes up degenerate.
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda home: None)

    def fail_popen(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("Popen must not run when toolset resolution fails")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    import pytest

    with pytest.raises(RuntimeError, match="toolset"):
        kb._default_spawn(_make_task(kb, assignee="salton"), str(workspace))


def test_default_spawn_raises_when_toolset_resolution_empty_list(monkeypatch, tmp_path):
    """An empty resolved toolset is just as degenerate as None — fail the spawn."""
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "salton"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "toolsets:\n  - hermes-cli\n  - terminal\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda home: [])

    def fail_popen(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("Popen must not run when toolset resolution is empty")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    import pytest

    with pytest.raises(RuntimeError, match="toolset"):
        kb._default_spawn(_make_task(kb, assignee="salton"), str(workspace))
