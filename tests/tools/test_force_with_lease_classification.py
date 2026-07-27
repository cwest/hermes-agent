"""Force-push classification splits the lease-guarded form from unconditional force.

``git push --force`` rewrites remote history and can silently clobber another
party's push. ``git push --force-with-lease`` (and ``--force-if-includes``)
abort when the remote ref moved since the last fetch, so they cannot overwrite
work someone else advanced — a materially different risk profile.

A single ``--forc[a-z]*`` pattern conflated the two, tagging a lease-guarded
push with the history-rewriting description. These tests assert the two are
classified as distinct categories:

  - unconditional ``--force`` / ``-f`` / abbreviations  → history-rewriting key
  - ``--force-with-lease`` / ``--force-if-includes``    → a distinct, non-
    history-rewriting key

Both remain dangerous (they require an approval decision), but the description
key — which the permanent-allowlist keys off — no longer conflates them, so a
profile can allowlist the lease-guarded form without also allowlisting an
unconditional history rewrite.
"""

from tools.approval import detect_dangerous_command


# The description string used by the unconditional history-rewriting patterns.
_HISTORY_REWRITE_KEY = "git force push (rewrites remote history)"
_HISTORY_REWRITE_SHORT_KEY = "git force push short flag (rewrites remote history)"
_HISTORY_REWRITE_KEYS = {_HISTORY_REWRITE_KEY, _HISTORY_REWRITE_SHORT_KEY}


class TestLeaseGuardedNotHistoryRewriting:
    """--force-with-lease / --force-if-includes must not carry the history key."""

    def test_force_with_lease_not_classified_as_history_rewriting(self):
        dangerous, key, desc = detect_dangerous_command(
            "git push --force-with-lease origin topic/x"
        )
        assert dangerous is True
        assert key not in _HISTORY_REWRITE_KEYS, (
            "lease-guarded push must not carry a history-rewriting classification, "
            f"got {key!r}"
        )
        assert "lease" in desc.lower() or "guard" in desc.lower()

    def test_force_if_includes_treated_like_force_with_lease(self):
        dangerous, key, _ = detect_dangerous_command(
            "git push --force-if-includes origin topic/x"
        )
        assert dangerous is True
        assert key not in _HISTORY_REWRITE_KEYS, (
            "--force-if-includes must be classified like --force-with-lease, "
            f"got {key!r}"
        )

    def test_force_with_lease_and_if_includes_share_a_category(self):
        _, lease_key, _ = detect_dangerous_command(
            "git push --force-with-lease origin topic/x"
        )
        _, includes_key, _ = detect_dangerous_command(
            "git push --force-if-includes origin topic/x"
        )
        assert lease_key == includes_key

    def test_force_with_lease_with_ref_value_not_history_rewriting(self):
        """`--force-with-lease=<ref>` (explicit lease value) is still lease-guarded."""
        dangerous, key, _ = detect_dangerous_command(
            "git push --force-with-lease=main origin topic/x"
        )
        assert dangerous is True
        assert key not in _HISTORY_REWRITE_KEYS


class TestUnconditionalForceStillHistoryRewriting:
    """The unconditional-force gate must not be weakened."""

    def test_force_full_still_history_rewriting(self):
        dangerous, key, _ = detect_dangerous_command(
            "git push --force origin main"
        )
        assert dangerous is True
        assert key == _HISTORY_REWRITE_KEY

    def test_short_f_still_history_rewriting(self):
        dangerous, key, _ = detect_dangerous_command("git push -f origin main")
        assert dangerous is True
        assert key == _HISTORY_REWRITE_SHORT_KEY

    def test_forc_abbreviation_still_history_rewriting(self):
        """git resolves the unambiguous prefix `--forc` to `--force`."""
        dangerous, key, _ = detect_dangerous_command(
            "git push --forc origin main"
        )
        assert dangerous is True
        assert key == _HISTORY_REWRITE_KEY

    def test_forced_variant_still_history_rewriting(self):
        dangerous, key, _ = detect_dangerous_command(
            "git push --forced origin main"
        )
        assert dangerous is True
        assert key == _HISTORY_REWRITE_KEY

    def test_no_force_not_flagged(self):
        dangerous, _, _ = detect_dangerous_command("git push origin main")
        assert dangerous is False

    def test_set_upstream_not_flagged(self):
        dangerous, _, _ = detect_dangerous_command(
            "git push --set-upstream origin feature"
        )
        assert dangerous is False
