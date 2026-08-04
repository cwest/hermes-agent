"""The ``agent:end`` hook must be able to BLOCK or REWRITE a reply.

Before this change the gateway fired ``agent:end`` via ``emit()``, which
discards handler return values, and passed only the first 500 chars of the
reply.  A handler could record a violation but never stop the reply, and a
violation buried past char 500 was invisible.

These tests pin the decision-processing seam that the ``agent:end`` call site
uses to honor handler decisions, mirroring the proven ``command:*`` pattern:

  * ``decision == "deny"``    -> original reply suppressed, handler ``message``
                                surfaced back into the loop so the agent revises.
  * ``decision == "rewrite"`` -> handler ``response`` substituted.
  * anything else / None / garbage / a raised handler -> NO-OP; the original
    reply passes through untouched (a broken predicate must never silence the
    agent).

The processing operates on the FULL reply, so a violation anywhere in a long
reply is caught — not only in the last 500 chars.
"""

import pytest

from gateway.run import (
    _agent_end_hook_context,
    _apply_agent_end_hook_decisions,
)


class TestResponseFullContext:
    """Requirement 1: ``response_full`` carries the untruncated reply while
    ``response`` stays capped at 500 chars (other consumers may rely on it)."""

    def test_response_stays_capped_response_full_is_untruncated(self):
        long_reply = "x" * 1200
        ctx = _agent_end_hook_context({"session_id": "s1", "platform": "test"}, long_reply)

        assert ctx["response"] == long_reply[:500]
        assert len(ctx["response"]) == 500
        assert ctx["response_full"] == long_reply
        assert len(ctx["response_full"]) == 1200
        # Base context is preserved.
        assert ctx["session_id"] == "s1"
        assert ctx["platform"] == "test"

    def test_none_response_yields_empty_strings(self):
        ctx = _agent_end_hook_context({"session_id": "s1"}, None)
        assert ctx["response"] == ""
        assert ctx["response_full"] == ""

    def test_short_reply_identical_in_both_fields(self):
        short = "all done"
        ctx = _agent_end_hook_context({}, short)
        assert ctx["response"] == short
        assert ctx["response_full"] == short


class TestDenyBlocksReply:
    def test_deny_suppresses_original_and_surfaces_message(self):
        original = "Sure — say the word and I'll file it."
        results = [{"decision": "deny", "message": "Do not ask permission; do the work."}]

        final, decision = _apply_agent_end_hook_decisions(original, results)

        assert decision == "deny"
        # The original reply is NOT what gets sent.
        assert final != original
        # The handler's message is surfaced back into the loop.
        assert final == "Do not ask permission; do the work."

    def test_deny_without_message_still_blocks(self):
        original = "Want me to?"
        results = [{"decision": "deny"}]

        final, decision = _apply_agent_end_hook_decisions(original, results)

        assert decision == "deny"
        # Blocked: the original permission-ask must not be what is returned.
        assert final != original
        assert isinstance(final, str) and final.strip()


class TestRewriteSubstitutes:
    def test_rewrite_replaces_response(self):
        original = "Should I go ahead?"
        results = [{"decision": "rewrite", "response": "Done. Filed as #123."}]

        final, decision = _apply_agent_end_hook_decisions(original, results)

        assert decision == "rewrite"
        assert final == "Done. Filed as #123."

    def test_rewrite_without_response_is_noop(self):
        original = "the real reply"
        results = [{"decision": "rewrite"}]  # no replacement text

        final, decision = _apply_agent_end_hook_decisions(original, results)

        # Missing replacement text must not blank the reply.
        assert decision is None
        assert final == original


class TestNoOpDecisionsPassThrough:
    def test_no_results_passes_through(self):
        original = "a perfectly good reply"
        final, decision = _apply_agent_end_hook_decisions(original, [])
        assert final == original
        assert decision is None

    def test_none_return_passes_through(self):
        # A record-only handler returns None; emit_collect drops it, so results
        # is empty — but be defensive if a None ever leaks in.
        original = "a perfectly good reply"
        final, decision = _apply_agent_end_hook_decisions(original, [None])
        assert final == original
        assert decision is None

    def test_allow_decision_passes_through(self):
        original = "a perfectly good reply"
        results = [{"decision": "allow"}]
        final, decision = _apply_agent_end_hook_decisions(original, results)
        assert final == original
        assert decision is None

    def test_empty_decision_passes_through(self):
        original = "a perfectly good reply"
        results = [{"foo": "bar"}]  # no decision key
        final, decision = _apply_agent_end_hook_decisions(original, results)
        assert final == original
        assert decision is None


class TestWedgeSafety:
    """A broken predicate must NEVER be able to silence the agent."""

    def test_garbage_result_types_pass_through(self):
        original = "a perfectly good reply"
        results = ["not a dict", 42, None, ["nested"], object()]
        final, decision = _apply_agent_end_hook_decisions(original, results)
        assert final == original
        assert decision is None

    def test_first_actionable_decision_wins_over_later_garbage(self):
        original = "say the word"
        results = [
            "garbage",
            {"decision": "deny", "message": "just do it"},
            {"decision": "rewrite", "response": "should never reach here"},
        ]
        final, decision = _apply_agent_end_hook_decisions(original, results)
        assert decision == "deny"
        assert final == "just do it"


class TestFullReplyIsWhatIsInspected:
    """The planted-positive backtest: a permission-ask buried mid-reply is caught
    when the decision is derived from the FULL reply, and would be MISSED if only
    the last 500 chars were seen.

    This test operates at the seam: it proves the helper acts on whatever text a
    handler was given.  The call-site wiring (passing response_full, not the
    truncated response) is covered by test_agent_end_response_full below.
    """

    def test_deny_derived_from_full_reply(self):
        # Simulate a handler that flags a buried permission-ask and denies.
        buried = "ok " * 400 + "say the word and I'll file it " + "done " * 400
        results = [{"decision": "deny", "message": "buried permission-ask found"}]
        final, decision = _apply_agent_end_hook_decisions(buried, results)
        assert decision == "deny"
        assert final == "buried permission-ask found"


class TestEmitCollectIntegration:
    """Prove the call-site pieces compose: a real HookRegistry.emit_collect
    feeding _apply_agent_end_hook_decisions, including the wedge-safety layer
    (a raising handler is swallowed by emit_collect and never denies)."""

    @staticmethod
    async def _run(handler_code, response):
        from gateway.hooks import HookRegistry

        reg = HookRegistry()
        # Register a single agent:end handler from source, mirroring how a
        # real hook module exposes ``handle``.
        ns: dict = {}
        exec(handler_code, ns)
        reg._handlers["agent:end"] = [ns["handle"]]

        ctx = _agent_end_hook_context({"session_id": "s"}, response)
        results = await reg.emit_collect("agent:end", ctx)
        return _apply_agent_end_hook_decisions(ctx["response_full"], results)

    @pytest.mark.asyncio
    async def test_deny_handler_blocks_and_surfaces_message(self):
        code = (
            "def handle(event_type, context):\n"
            "    if 'say the word' in context.get('response_full', ''):\n"
            "        return {'decision': 'deny', 'message': 'JUST DO THE WORK.'}\n"
            "    return None\n"
        )
        final, decision = await self._run(code, "Sure — say the word and I'll do it.")
        assert decision == "deny"
        assert final == "JUST DO THE WORK."

    @pytest.mark.asyncio
    async def test_buried_permission_ask_caught_via_response_full_missed_via_response(self):
        # A permission-ask past char 500. A handler reading response_full catches
        # it; the same handler reading the truncated response would miss it.
        buried = "x" * 800 + " say the word " + "y" * 40
        code_full = (
            "def handle(event_type, context):\n"
            "    if 'say the word' in context.get('response_full', ''):\n"
            "        return {'decision': 'deny', 'message': 'blocked'}\n"
            "    return None\n"
        )
        final, decision = await self._run(code_full, buried)
        assert decision == "deny", "response_full must catch the buried ask"

        # Same reply, handler reading only the (truncated) response field: MISS.
        code_trunc = (
            "def handle(event_type, context):\n"
            "    if 'say the word' in context.get('response', ''):\n"
            "        return {'decision': 'deny', 'message': 'blocked'}\n"
            "    return None\n"
        )
        final2, decision2 = await self._run(code_trunc, buried)
        assert decision2 is None, "truncated response misses a buried ask"
        assert final2 == buried

    @pytest.mark.asyncio
    async def test_raising_handler_does_not_wedge(self):
        code = (
            "def handle(event_type, context):\n"
            "    raise RuntimeError('boom')\n"
        )
        final, decision = await self._run(code, "a perfectly good reply")
        assert decision is None
        assert final == "a perfectly good reply"

    @pytest.mark.asyncio
    async def test_record_only_handler_unaffected(self):
        # A record-only handler returns None; the reply passes through and the
        # handler still ran (side effect observable via a module global).
        code = (
            "SEEN = []\n"
            "def handle(event_type, context):\n"
            "    SEEN.append(context.get('response_full'))\n"
            "    return None\n"
        )
        final, decision = await self._run(code, "recorded but not blocked")
        assert decision is None
        assert final == "recorded but not blocked"

    @pytest.mark.asyncio
    async def test_rewrite_handler_substitutes(self):
        code = (
            "def handle(event_type, context):\n"
            "    return {'decision': 'rewrite', 'response': 'cleaned reply'}\n"
        )
        final, decision = await self._run(code, "original reply")
        assert decision == "rewrite"
        assert final == "cleaned reply"
