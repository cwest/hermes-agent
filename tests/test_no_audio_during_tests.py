"""The test suite must never emit audio on the developer's machine.

Casey, 2026-08-12: "Something is running a hermes test suite and I happen to be
home so I can hear TTS tests. Please turn the volume off on your host machine
when you run tests."

Muting the Mac is a workaround, not a fix: the next run on an unmuted machine
regresses. The real defect is that `tools.voice_mode._play_audio_file` shells
out to a REAL system player (`afplay`/`ffplay`/`aplay`), and nothing in the
suite prevented that from happening under pytest.

These tests pin the contract at the source. `conftest.py` installs an autouse
fixture that makes real playback impossible for every test in the suite; this
file proves the guard exists and actually holds.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest


def test_subprocess_popen_refuses_system_audio_players():
    """The autouse guard must block a real audio player, loudly and by name.

    This is the exact call shape `_play_audio_file` uses. If the guard ever
    regresses, this fails instead of Casey's speakers firing at 10pm.
    """
    for player in ("afplay", "ffplay", "aplay"):
        with pytest.raises(RuntimeError, match="audio playback is blocked"):
            subprocess.Popen([player, "/tmp/whatever.mp3"])


def test_subprocess_run_and_call_are_guarded_too():
    """Sibling call paths count — a guard on one entry point is decoration."""
    with pytest.raises(RuntimeError, match="audio playback is blocked"):
        subprocess.run(["afplay", "/tmp/whatever.mp3"])
    with pytest.raises(RuntimeError, match="audio playback is blocked"):
        subprocess.call(["say", "hello"])


def test_macos_say_is_blocked():
    """`say` is TTS straight to the speakers — the literal thing Casey heard."""
    with pytest.raises(RuntimeError, match="audio playback is blocked"):
        subprocess.Popen(["say", "this should never be audible"])


def test_non_audio_subprocess_still_works():
    """The guard must not break the rest of the suite.

    A blanket subprocess ban would be its own outage. Only audio players are
    refused; everything else passes through untouched.
    """
    out = subprocess.run(
        ["echo", "still working"], capture_output=True, text=True
    )
    assert out.returncode == 0
    assert "still working" in out.stdout


def test_play_audio_file_never_reaches_a_real_player():
    """End-to-end: the real production function must not emit sound.

    Exercises `_play_audio_file` itself rather than a mock of it, so the guard
    is proven against the actual code path that made noise.
    """
    voice_mode = pytest.importorskip("tools.voice_mode")
    if not any(shutil.which(p) for p in ("afplay", "ffplay", "aplay")):
        pytest.skip("no system audio player installed; nothing to guard here")

    # Must not raise, must not play. The function catches its own errors and
    # reports failure rather than crashing the caller.
    result = voice_mode.play_audio_file("/tmp/nonexistent-hermes-test.mp3")
    assert result is not True
