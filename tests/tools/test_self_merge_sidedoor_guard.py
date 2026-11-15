"""Command-start-aware deny for a path-qualified ``gh pr merge|ready`` side door.

This is the code-shipped, every-profile close for the KB self-merge residual that
the per-profile ``approvals.deny`` fnmatch globs could narrow but not fully close.

The config-glob layer (``_match_user_deny_rule``) fnmatches the WHOLE command
line, so an ``fnmatch`` pattern with a leading ``*`` cannot distinguish an
ABSOLUTE-PATH executable that is NOT gh but merely quotes the repro
(``/usr/bin/python3 -c "... /opt/homebrew/bin/gh pr merge ..."``) from a real
path-qualified ``gh`` invocation — both start with ``/`` and carry the
``gh pr merge`` token sequence. That narrow false-positive was documented as an
xfail in the config repo's ``test_kb_self_merge_deny_rules.py``.

``detect_self_merge_sidedoor`` closes it at the terminal tool itself: it reuses
the quote-aware command-start machinery (``_iter_shell_command_starts`` /
``_read_shell_word``) to inspect the EXECUTABLE of each REAL command segment, and
fires ONLY when a segment's executable is a PATH-QUALIFIED ``gh`` (a ``/`` in the
program word, basename ``gh``) running ``pr merge`` / ``pr ready``. A path-qualified
``gh pr merge`` sitting inside a quoted argument to another command is never a
command-position executable, so it is never a match — the residual FP is gone.

It is a hardline-class floor: unconditional, before the yolo / mode=off bypass,
shipped in code for every profile with no per-config duplication. The sanctioned
seam (``onecard_common.self_merge_kb_pr``) runs ``gh`` via ``subprocess`` and never
routes a command line through the terminal tool, so it is untouched.

The command fragments below are assembled from pieces so this source file does
not itself carry a literal path-qualified ``gh pr merge`` command token; the
runtime strings are byte-identical to the real repros.
"""

from __future__ import annotations

import pytest

from tools import approval as mod

_GH = "/opt/homebrew/bin/" + "gh"       # a real absolute path to gh
_MERGE = "pr " + "merge"
_READY = "pr " + "ready"
_KB = "cwest/knowledge-base"
_SENTINEL = "HERMES_KB_SELF_MERGE" + "_GATED"


# --------------------------------------------------------------------------- #
# Commands that MUST fire the side-door guard: a path-qualified gh in command
# position running pr merge / pr ready, in every residual shape, plus the
# hand-set sentinel. These never route through the sanctioned findings gate.
# --------------------------------------------------------------------------- #
_BLOCKED_SIDEDOOR = [
    # absolute-path gh, --repo form
    f"{_GH} {_MERGE} 584 --repo {_KB} --squash",
    # absolute-path gh, positional PR URL (no -R/--repo)
    f"{_GH} {_MERGE} https://github.com/cwest/knowledge-base/pull/584 --squash",
    # absolute-path gh, -R short flag
    f"{_GH} {_MERGE} 584 -R {_KB} --squash",
    # absolute-path gh, --repo= joined form
    f"{_GH} {_MERGE} 584 --repo={_KB} --squash",
    # absolute-path gh, pr ready (undraft half of a self-merge)
    f"{_GH} {_READY} 584 --repo {_KB}",
    # absolute-path gh, repo INFERRED FROM CWD: no --repo, no repo name in argv
    f"{_GH} {_MERGE} 99999 --squash",
    f"{_GH} {_READY} 99999",
    # compound: cd into the KB checkout && absolute-path merge, no repo flag
    f"cd /Users/caseywest/src/knowledge-base && {_GH} {_MERGE} 99999 --squash",
    # compound with a semicolon separator
    f"cd /Users/caseywest/src/knowledge-base; {_GH} {_MERGE} 99999 --squash",
    # compound, pr ready
    f"cd /Users/caseywest/src/knowledge-base && {_GH} {_READY} 99999",
    # relative ./gh (still path-qualified, not a bare gh)
    f"./gh {_MERGE} 99999 --squash",
    # relative ../bin/gh
    f"../bin/gh {_MERGE} 99999 --squash",
    # path-qualified gh after a pipe
    f"true | {_GH} {_MERGE} 99999 --squash",
    # --- repo flag BEFORE the subcommand tokens: gh accepts global/repo flags
    #     ahead of `pr merge`, and those forms reach the real merge, so the match
    #     must find the pr -> merge/ready subsequence among the non-flag words
    #     rather than anchoring at argv 0/1 (otherwise these leak). ------------
    f"{_GH} --repo {_KB} {_MERGE} 584 --squash",
    f"{_GH} -R {_KB} {_MERGE} 584 --squash",
    f"{_GH} --repo={_KB} {_MERGE} 584",
    f"{_GH} --repo {_KB} {_READY} 584",
    # --- repo flag BETWEEN `pr` and `merge`/`ready` (gh also parses this): ----
    f"{_GH} pr --repo {_KB} merge 584 --squash",
    f"{_GH} pr -R {_KB} merge 584 --squash",
    f"{_GH} pr --repo={_KB} merge 584",
    f"{_GH} pr -R {_KB} ready 584",
    # --- short -R joined value forms real gh accepts (-R=val and -Rval): ------
    f"{_GH} -R={_KB} {_MERGE} 584",
    f"{_GH} -R{_KB} {_MERGE} 584",
    f"{_GH} pr -R={_KB} merge 584",
    f"{_GH} pr -R{_KB} merge 584",
    # --- a boolean flag interleaved before the subcommand split: --------------
    f"{_GH} --repo {_KB} --squash {_MERGE} 584",
    # --- flag before AND the pr/verb split, cwd-inferred (no repo flag): ------
    f"{_GH} pr merge --squash 584",
]

# The hand-set sentinel: forging the env var the shim trusts, inline or exported.
_BLOCKED_SENTINEL = [
    f"{_SENTINEL}=1 gh {_MERGE} 584 --repo {_KB} --squash",
    f"{_SENTINEL}=1 gh {_READY} 584 --repo {_KB}",
    f"export {_SENTINEL}=1",
    f"export {_SENTINEL}=1 && gh {_MERGE} 584 --repo {_KB}",
]

# --------------------------------------------------------------------------- #
# Commands that MUST NOT fire: the residual FP (NON-gh tool quoting the repro),
# bare gh (routes through the shim), and read-only gh (never a self-merge).
# --------------------------------------------------------------------------- #
_ALLOWED = [
    # THE RESIDUAL FP this card closes: an absolute-path NON-gh tool whose quoted
    # -c / argument merely quotes a path-qualified gh merge/ready. The executable
    # is python3 / env, not gh — it must NOT be blocked.
    f'/usr/bin/python3 -c "x = \'{_GH} {_MERGE} 99\'"',
    f'/usr/bin/env echo "repro: {_GH} {_MERGE} 99 --repo {_KB}"',
    # a kanban comment body quoting the repro (observed-live FP #1)
    f'hermes kanban comment t_x --body "repro: {_GH} {_MERGE} 584 --repo {_KB}"',
    f'hermes kanban comment t_x --body "repro: {_GH} {_READY} 584 --repo {_KB}"',
    # a heredoc/notes string mentioning it (observed-live FP #2)
    f"python3 run.py  # notes: the guard refuses {_GH} {_MERGE} on the KB",
    # an echo of the repro — quoted data, not a command
    f"echo 'repro: {_GH} {_MERGE} 584 --repo {_KB}'",
    # a comment quoting the compound cd && <path-gh> merge repro
    f'hermes kanban comment t_x --body "run: cd ~/x && {_GH} {_MERGE} 99999"',
    # BARE gh merge (any repo / cwd-inferred) — routes through the PATH shim
    f"gh {_MERGE} 7 --repo cwest/okfctl --squash",
    f"gh {_MERGE} 7 --squash",
    f"gh {_MERGE} 99999 --squash",
    # read-only gh — never a self-merge, bare OR path-qualified
    f"gh pr view 584 --repo {_KB}",
    f"gh pr list --repo {_KB}",
    f"{_GH} pr view 584 --repo {_KB}",
    f"{_GH} pr list",
    # read-only gh with the repo flag interleaved before/between the subcommand
    # tokens — the positional reduction must not misread `view`/`list` as a verb
    f"{_GH} --repo {_KB} pr view 584",
    f"{_GH} pr --repo {_KB} view 584",
    f"{_GH} -R {_KB} pr list",
    f"gh pr create --repo cwest/okfctl --title x --body y",
    # unrelated commands
    "ls -la",
    "git -C /Users/caseywest/src/knowledge-base status",
]

# Option (c) consequence: a PATH-QUALIFIED merge/ready on ANOTHER repo is also a
# side door (the absolute path skips the shim regardless of the repo), so it is
# denied too. Proof the close is the absolute-path side door, not the repo name.
_BLOCKED_OTHER_REPO = [
    f"{_GH} {_MERGE} 7 --repo cwest/okfctl --squash",
    f"{_GH} {_READY} 7 --repo cwest/okfctl",
]


# --------------------------------------------------------------------------- #
# detect_self_merge_sidedoor: the pure predicate.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", _BLOCKED_SIDEDOOR)
def test_path_qualified_gh_merge_ready_is_detected(command):
    is_block, desc = mod.detect_self_merge_sidedoor(command)
    assert is_block is True, (
        f"a path-qualified gh pr merge/ready in command position must fire: {command!r}"
    )
    assert desc


@pytest.mark.parametrize("command", _BLOCKED_SENTINEL)
def test_hand_set_sentinel_is_detected(command):
    is_block, _desc = mod.detect_self_merge_sidedoor(command)
    assert is_block is True, (
        f"hand-setting the KB self-merge sentinel must fire: {command!r}"
    )


@pytest.mark.parametrize("command", _BLOCKED_OTHER_REPO)
def test_path_qualified_merge_ready_other_repo_is_detected(command):
    is_block, _desc = mod.detect_self_merge_sidedoor(command)
    assert is_block is True, (
        f"the absolute-path side door is closed for ANY repo: {command!r}"
    )


@pytest.mark.parametrize("command", _ALLOWED)
def test_allowed_commands_do_not_fire(command):
    is_block, _desc = mod.detect_self_merge_sidedoor(command)
    assert is_block is False, (
        f"this command must NOT fire the side-door guard: {command!r}"
    )


# --------------------------------------------------------------------------- #
# The residual FP, stated as its own named regression (the xfail this card flips
# to a plain pass): an absolute-path NON-gh tool quoting the repro.
# --------------------------------------------------------------------------- #
_RESIDUAL_FP = [
    f'/usr/bin/python3 -c "x = \'{_GH} {_MERGE} 99\'"',
    f'/usr/bin/env echo "repro: {_GH} {_MERGE} 99 --repo {_KB}"',
]


@pytest.mark.parametrize("command", _RESIDUAL_FP)
def test_residual_fp_abs_path_nongh_tool_quoting_repro_not_blocked(command):
    """The exact xfail class from the config-glob layer, now cleanly closed."""
    is_block, _desc = mod.detect_self_merge_sidedoor(command)
    assert is_block is False, (
        "an absolute-path NON-gh tool whose quoted argument merely quotes a "
        f"path-qualified gh merge/ready must not be blocked: {command!r}"
    )


# --------------------------------------------------------------------------- #
# Unconditional, hardline-class: fires through the real guard entry points even
# under yolo / mode=off, and reports its own block (not an incidental prompt).
# --------------------------------------------------------------------------- #
@pytest.fixture
def clean_env(monkeypatch):
    for var in ("HERMES_YOLO_MODE", "HERMES_GATEWAY_SESSION",
                "HERMES_CRON_SESSION", "HERMES_INTERACTIVE", "HERMES_EXEC_ASK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(mod, "_YOLO_MODE_FROZEN", False)


@pytest.mark.parametrize("command", _BLOCKED_SIDEDOOR + _BLOCKED_SENTINEL)
def test_full_guard_blocks_sidedoor_even_under_yolo(command, clean_env, monkeypatch):
    monkeypatch.setattr(mod, "_YOLO_MODE_FROZEN", True)
    result = mod.check_all_command_guards(command, "local")
    assert result["approved"] is False, (
        f"the side door must be refused even under yolo: {command!r}"
    )


@pytest.mark.parametrize("command", _BLOCKED_SIDEDOOR)
def test_check_dangerous_command_blocks_sidedoor_even_under_yolo(
        command, clean_env, monkeypatch):
    monkeypatch.setattr(mod, "_YOLO_MODE_FROZEN", True)
    result = mod.check_dangerous_command(command, "local")
    assert result["approved"] is False, (
        f"check_dangerous_command must also refuse the side door: {command!r}"
    )


@pytest.mark.parametrize("command", _ALLOWED)
def test_full_guard_does_not_block_allowed(command, clean_env, monkeypatch):
    """An allowed command is never refused BY THIS guard. It may still require
    approval for other reasons, but under yolo it must pass straight through."""
    monkeypatch.setattr(mod, "_YOLO_MODE_FROZEN", True)
    result = mod.check_all_command_guards(command, "local")
    assert result["approved"] is True, (
        f"the side-door guard must not touch this command: {command!r}"
    )
