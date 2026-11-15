"""Cross-session HERMES_SESSION_ID leak via the shared bash snapshot.

Regression coverage for the bug where a single long-lived backend serves many
sessions through ONE ``_active_environments["default"]`` LocalEnvironment (the
messaging gateway, TUI, and desktop/web dashboard all collapse the terminal to
"default"). That environment persists a bash *session snapshot* file and
``source``s it before every command. ``export -p`` dumped the FIRST session's
``HERMES_SESSION_ID`` into the snapshot, so every LATER session ``source``d that
stale value and its ``echo $HERMES_SESSION_ID`` reported a FOREIGN session's id
— overriding the correct per-command Popen env injected by
``_inject_session_context_env``.

The fix strips the per-session bridged vars (HERMES_SESSION_* / UI /
CRON_AUTO_DELIVER_) from the snapshot at both dump sites in
``tools/environments/base.py``; they are re-injected fresh on every command.
"""

import os
import re
import sys

import pytest

from tools.environments.base import (
    _SNAPSHOT_EXCLUDED_ENV_REGEX,
    _export_dump_excluding_session_vars,
)


# ---------------------------------------------------------------------------
# Unit: the exclusion regex matches exactly the bridged vars, nothing else.
# ---------------------------------------------------------------------------

def test_regex_matches_bridged_session_vars():
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    # Every var the gateway bridges must be excluded.
    from gateway.session_context import _VAR_MAP

    for name in _VAR_MAP:
        line = f'declare -x {name}="whatever"'
        assert rx.search(line), f"{name} should be excluded from the snapshot"


def test_regex_preserves_user_env():
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    for line in (
        'declare -x PATH="/usr/bin:/bin"',
        'declare -x HOME="/home/user"',
        'declare -x HERMES_HOME="/home/user/.hermes"',  # NOT a session var
        'declare -x HERMESX="x"',
        'declare -x MY_HERMES_SESSION_ID="x"',  # prefix must anchor after "declare -x "
    ):
        assert not rx.search(line), f"{line!r} must be preserved in the snapshot"


def _lineage_marker_names():
    from agent.delegation_context import (
        DELEGATED_CHILD_ENV_MARKER,
        KANBAN_ENV_KEYS,
        KANBAN_ORIGIN_ENV_KEY,
    )

    return (DELEGATED_CHILD_ENV_MARKER, KANBAN_ORIGIN_ENV_KEY, *KANBAN_ENV_KEYS)


def test_regex_matches_lineage_and_kanban_markers():
    """Per-turn / per-lineage markers must never persist in the snapshot.

    A delegate_task child's command exports HERMES_DELEGATED_CHILD_CONTEXT=1;
    a gateway turn mirrors HERMES_KANBAN_ORIGIN. If either lands in the shared
    snapshot, every later command of every later session sources it back.
    """
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    for name in _lineage_marker_names():
        for line in (f'declare -x {name}="v"', f"declare -x {name}"):
            assert rx.search(line), f"{line!r} should be excluded from the snapshot"


def test_regex_lineage_names_are_exact_not_prefixes():
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    for name in _lineage_marker_names():
        line = f'declare -x {name}_SUFFIX="v"'
        assert not rx.search(line), f"{line!r} must be preserved (exact-name match)"


def test_session_context_origin_key_is_the_shared_constant():
    from agent.delegation_context import KANBAN_ORIGIN_ENV_KEY
    from gateway.session_context import _KANBAN_ORIGIN_ENV

    assert _KANBAN_ORIGIN_ENV == KANBAN_ORIGIN_ENV_KEY


def test_export_snippet_shape():
    snippet = _export_dump_excluding_session_vars("/tmp/snap.tmp.$BASHPID")
    assert "export -p" in snippet
    assert "grep -vE" in snippet
    assert "/tmp/snap.tmp.$BASHPID" in snippet
    # The redirection must be attached to a brace group wrapping the pipeline,
    # NOT to the grep segment: a redirect on grep expands $BASHPID inside
    # grep's pipeline subshell (a different PID than the parent shell that
    # expands the follow-up ``mv`` operand), silently orphaning the dump and
    # breaking snapshot env persistence entirely.
    assert snippet.lstrip().startswith("{ ")
    assert "|| true; }" in snippet
    assert snippet.rstrip().endswith("> /tmp/snap.tmp.$BASHPID")


# ---------------------------------------------------------------------------
# Integration: real LocalEnvironment, two sessions, no cross-contamination.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_shared_snapshot_no_cross_session_leak(tmp_path):
    import threading

    from gateway.session_context import _VAR_MAP, _UNSET, set_session_vars
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    env.init_session()
    try:
        def run_as(sid):
            out = {}

            def worker():
                for v in _VAR_MAP.values():
                    v.set(_UNSET)
                set_session_vars(session_key="k" + sid, session_id=sid, source="desktop")
                out["r"] = env.execute('echo "[$HERMES_SESSION_ID]"')

            t = threading.Thread(target=worker)
            t.start()
            t.join()
            return out["r"].get("output", "")

        out_a = run_as("SIDAAA")
        out_b = run_as("SIDBBB")

        assert "SIDAAA" in out_a, f"session A saw {out_a!r}"
        # The core assertion: B must see its OWN id, not A's leaked via snapshot.
        assert "SIDBBB" in out_b, f"session B saw {out_b!r}"
        assert "SIDAAA" not in out_b, f"session B leaked A's id: {out_b!r}"

        # And the snapshot file must not carry the session id at all.
        snap = env._snapshot_path
        if os.path.exists(snap):
            with open(snap) as f:
                assert "HERMES_SESSION_ID" not in f.read()
    finally:
        env.cleanup()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_shared_snapshot_drops_lineage_markers(tmp_path, monkeypatch):
    """A command that exports lineage/kanban markers must not poison later ones.

    Reproduces the orchestrator wake that inherited a delegate child's
    HERMES_DELEGATED_CHILD_CONTEXT=1 (every ``hermes kanban`` call then refused
    to run) and a stale HERMES_KANBAN_ORIGIN from an earlier turn.
    """
    import threading

    from tools.environments.local import LocalEnvironment

    names = _lineage_marker_names()
    for name in names:
        monkeypatch.delenv(name, raising=False)

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    env.init_session()
    try:
        exports = " ".join(f"{n}=stale-{n}" for n in names)
        first = env.execute(f"export {exports}; echo ok")
        assert "ok" in first.get("output", "")

        # Ordinary user exports still persist (the snapshot keeps working).
        env.execute("export T5991_USER_VAR=kept")

        probe = " ".join(f'"{n}=[${{{n}-unset}}]"' for n in names)
        out = {}

        def later_session():
            out["r"] = env.execute(f'printf "%s\\n" {probe} "U=[$T5991_USER_VAR]"')

        t = threading.Thread(target=later_session)
        t.start()
        t.join()
        text = out["r"].get("output", "")

        assert "U=[kept]" in text, text
        for name in names:
            assert f"{name}=[unset]" in text, f"{name} leaked via snapshot: {text!r}"

        with open(env._snapshot_path) as f:
            snap = f.read()
        for name in names:
            assert f"stale-{name}" not in snap, f"{name} persisted in the snapshot"
    finally:
        env.cleanup()
