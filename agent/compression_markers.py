"""Shared sentinel for context-compression elision of tool-call arguments.

Context compression rewrites *historical* assistant tool calls to shrink
their large string arguments (a 50 KB ``write_file`` payload, a long
``patch`` body). An earlier implementation kept the first ~200 chars of
the string and appended the literal ``...[truncated]`` **inside the string
value**, so the model saw its own prior write shaped like::

    {"path": "notes.md", "content": "# real head text\\n...[truncated]"}

That is a *valid-looking, copyable* payload. The model then reproduces the
shape in NEW tool calls — new ``write_file`` / ``patch`` / kanban comment
payloads come out cut off at ~200 chars with the marker appended, silently
writing corrupted files and superseded comments (see the card for evidence).

The fix has two halves, both anchored on the sentinel defined here:

1. **Root cause** — the compressor replaces the *entire* over-long string
   with :data:`ELIDED_ARG_SENTINEL` wrapped in an elision note. There is no
   longer a real content prefix for the model to copy, and the value reads
   unmistakably as compression metadata rather than as content.

2. **Hard gate** — ``write_file``, ``patch`` and the kanban
   comment/create tool paths refuse any payload that still contains the
   sentinel via :func:`contains_compression_elision`, telling the model to
   re-send the full content. This catches the case where a model copies a
   compressed value verbatim before the root-cause change fully drains from
   its context window.

The sentinel uses a Unicode Private-Use-Area codepoint so it can never
collide with real prose. Crucially it is NOT the natural-language string
``...[truncated]`` — that substring legitimately appears in source, docs
and this very codebase, and gating on it would produce false refusals.
"""

from __future__ import annotations

# U+E011 is in the Unicode Private Use Area — it carries no meaning in any
# real document, so its presence in a tool payload is proof the payload was
# copied out of a compression-elided history rather than authored.
_PUA = "\ue011"

#: The exact substring the compressor stamps into an elided argument and the
#: tool gate refuses. Kept as a single module-level constant so the writer
#: (the compressor) and the readers (the tool gate) can never drift apart.
ELIDED_ARG_SENTINEL = f"{_PUA}[content elided by context compression]{_PUA}"


def elided_arg_placeholder(original_len: int) -> str:
    """Build the structural placeholder that replaces an over-long string arg.

    The whole original string value is discarded — no head slice is kept —
    so nothing in the elided form reads as copyable content. The character
    count is included purely as a human/debug hint; it is inert data, not a
    prefix of the original text.
    """
    return f"{ELIDED_ARG_SENTINEL} (original was {int(original_len)} chars)"


def contains_compression_elision(text: object) -> bool:
    """True if ``text`` carries the compression-elision sentinel.

    Used by the write_file / patch / kanban comment+create gates to refuse a
    payload the model copied out of an elided history. Non-string input is
    treated as not containing the sentinel (never raises).
    """
    if not isinstance(text, str):
        return False
    return ELIDED_ARG_SENTINEL in text
