"""Gate: refuse tool payloads carrying the context-compression elision sentinel.

Context compression rewrites historical tool-call args, and the model
imitates that shape in new calls — producing cut-off ``write_file`` /
``patch`` / kanban payloads. These tests pin the hard gate that refuses any
payload still carrying the compression sentinel, plus the sentinel module's
own contract.

See ``agent/compression_markers.py`` and the card for the full rationale.
"""

import json

import pytest

from agent.compression_markers import (
    ELIDED_ARG_SENTINEL,
    contains_compression_elision,
    elided_arg_placeholder,
)


class TestCompressionSentinel:
    def test_detector_true_when_sentinel_present(self):
        payload = "# My doc\n" + ELIDED_ARG_SENTINEL
        assert contains_compression_elision(payload) is True

    def test_detector_true_when_sentinel_embedded_midstring(self):
        payload = "before " + ELIDED_ARG_SENTINEL + " after"
        assert contains_compression_elision(payload) is True

    def test_detector_false_on_plain_content(self):
        assert contains_compression_elision("# A normal markdown file\n") is False

    def test_detector_does_not_flag_natural_language_truncated(self):
        """The natural-language string ``...[truncated]`` legitimately appears
        in source, docs, and this very codebase — gating on it would produce
        false refusals. Only the PUA sentinel triggers the gate."""
        assert contains_compression_elision("output was ...[truncated] here") is False
        assert contains_compression_elision("line[:80] + '... [truncated]'") is False

    def test_detector_non_string_is_false(self):
        assert contains_compression_elision(None) is False
        assert contains_compression_elision(1234) is False
        assert contains_compression_elision({"a": 1}) is False

    def test_placeholder_carries_sentinel_and_length_hint(self):
        ph = elided_arg_placeholder(4096)
        assert contains_compression_elision(ph) is True
        assert "4096" in ph

    def test_placeholder_retains_no_content_prefix(self):
        """The whole original string is discarded — the placeholder must not
        be a copyable prefix of real content."""
        ph = elided_arg_placeholder(4096)
        # a placeholder is pure metadata; it opens with the (invisible) PUA
        # sentinel, never with document-looking text.
        assert ph.startswith(ELIDED_ARG_SENTINEL)


class TestWriteFileGate:
    def test_write_file_refuses_content_with_sentinel(self):
        """Negative control + fix: a write_file whose content carries the
        compression sentinel must be REFUSED (not written)."""
        from tools.file_tools import _handle_write_file

        poisoned = "# Real head of a doc\n" + elided_arg_placeholder(5000)
        result = json.loads(_handle_write_file({"path": "/tmp/should_not_write.md", "content": poisoned}))
        assert "error" in result
        assert "compression" in result["error"].lower() or "re-send" in result["error"].lower()

    def test_write_file_allows_clean_content(self, tmp_path):
        from tools.file_tools import _handle_write_file

        target = tmp_path / "clean.md"
        result = json.loads(_handle_write_file({"path": str(target), "content": "# Perfectly normal\n"}))
        assert "error" not in result
        assert target.read_text() == "# Perfectly normal\n"

    def test_write_file_allows_natural_language_truncated(self, tmp_path):
        """A doc that legitimately talks about ``...[truncated]`` must write."""
        from tools.file_tools import _handle_write_file

        target = tmp_path / "doc.md"
        body = "The reader shows long lines as `... [truncated]`.\n"
        result = json.loads(_handle_write_file({"path": str(target), "content": body}))
        assert "error" not in result
        assert target.read_text() == body


class TestPatchGate:
    def test_patch_replace_refuses_new_string_with_sentinel(self):
        from tools.file_tools import _handle_patch

        poisoned = "new body\n" + elided_arg_placeholder(3000)
        result = json.loads(_handle_patch({
            "mode": "replace",
            "path": "/tmp/should_not_patch.py",
            "old_string": "old",
            "new_string": poisoned,
        }))
        assert "error" in result
        assert "compression" in result["error"].lower() or "re-send" in result["error"].lower()

    def test_patch_v4a_refuses_patch_body_with_sentinel(self):
        from tools.file_tools import _handle_patch

        poisoned_patch = (
            "*** Begin Patch\n*** Update File: x.py\n@@\n-old\n+new "
            + elided_arg_placeholder(2000)
            + "\n*** End Patch\n"
        )
        result = json.loads(_handle_patch({"mode": "patch", "patch": poisoned_patch}))
        assert "error" in result
        assert "compression" in result["error"].lower() or "re-send" in result["error"].lower()
