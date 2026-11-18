"""Client-provided session setup over ACP: ``systemPrompt`` and ``additionalDirectories``.

ACP v1 lets a client hand the agent system-level instructions
(``systemPrompt``, append mode) and extra workspace roots
(``additionalDirectories``) on ``session/new``, ``session/load`` and
``session/resume`` — but only when the agent advertises the matching
``sessionCapabilities``. These tests pin the contract end to end:

* ``initialize`` advertises both capabilities on the wire;
* a raw JSON-RPC request carrying the fields reaches the handler (the stock
  SDK request models would silently drop ``systemPrompt``);
* the system prompt lands in the model's ``system`` message, never in a user
  turn, and survives load/resume and a model switch;
* extra roots are visible to the model, to skill discovery, and to the
  workspace edit-approval boundary;
* unsupported or malformed values fail with ``-32602`` without creating the
  session, as the spec requires;
* a session created without either field behaves exactly as before.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import acp
from acp.exceptions import RequestError

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager


APPEND = {"mode": "append", "content": [{"type": "text", "text": "Focus on security review."}]}


def _agent_stub():
    return SimpleNamespace(
        ephemeral_system_prompt=None,
        model="test-model",
        provider="openrouter",
        base_url="",
        api_mode="chat_completions",
        enabled_toolsets=["hermes-acp"],
        disabled_toolsets=None,
        tools=[],
        valid_tool_names=set(),
    )


@pytest.fixture()
def manager(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    return SessionManager(agent_factory=_agent_stub, db=db)


@pytest.fixture()
def agent(manager):
    return HermesACPAgent(session_manager=manager)


def _skill(root: Path, name: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A test skill for ACP roots.\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


# ---------------------------------------------------------------------------
# A real JSON-RPC client over in-memory pipes, driving acp.run_agent exactly
# the way ``hermes acp`` does — so routing/validation is the real SDK path.
# ---------------------------------------------------------------------------


class _PipeTransport(asyncio.Transport):
    def __init__(self, reader: asyncio.StreamReader):
        super().__init__()
        self._reader = reader
        self._closing = False

    def write(self, data):
        self._reader.feed_data(data)

    def is_closing(self):
        return self._closing

    def close(self):
        self._closing = True
        self._reader.feed_eof()

    def get_extra_info(self, name, default=None):
        return default


class _WireClient:
    def __init__(self, hermes_agent: HermesACPAgent):
        self._agent = hermes_agent
        self._to_agent = asyncio.StreamReader()
        self._from_agent = asyncio.StreamReader()
        self._next_id = 0
        self._task = None

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
        writer = asyncio.StreamWriter(_PipeTransport(self._from_agent), protocol, None, loop)
        self._task = asyncio.create_task(
            acp.run_agent(self._agent, writer, self._to_agent, use_unstable_protocol=True)
        )
        return self

    async def __aexit__(self, *exc):
        self._to_agent.feed_eof()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass

    async def call(self, method: str, params: dict) -> dict:
        request_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self._to_agent.feed_data((json.dumps(payload) + "\n").encode())
        while True:
            line = await asyncio.wait_for(self._from_agent.readline(), timeout=10)
            message = json.loads(line)
            if message.get("id") == request_id and "method" not in message:
                return message


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


class TestCapabilityAdvertisement:
    @pytest.mark.asyncio
    async def test_initialize_advertises_system_prompt_and_additional_directories(self, agent):
        async with _WireClient(agent) as client:
            reply = await client.call("initialize", {"protocolVersion": 1})

        caps = reply["result"]["agentCapabilities"]["sessionCapabilities"]
        assert caps["systemPrompt"] == {}
        assert caps["additionalDirectories"] == {}
        # Append-only: override would drop Hermes' own tool and safety scaffolding.
        assert "mode" not in caps["systemPrompt"]
        # The previously advertised session capabilities are still there.
        assert {"fork", "list", "resume"} <= set(caps)
        assert reply["result"]["protocolVersion"] == acp.PROTOCOL_VERSION

    @pytest.mark.asyncio
    async def test_system_prompt_is_not_advertised_when_it_cannot_be_routed(self, agent):
        # A connection whose router cannot be re-registered would drop the
        # field on the floor; advertising it then would be a lie to the client.
        agent.on_connect(SimpleNamespace())
        resp = await agent.initialize(protocol_version=1)
        caps = resp.agent_capabilities.session_capabilities
        assert caps.system_prompt is None
        assert caps.additional_directories is not None


# ---------------------------------------------------------------------------
# systemPrompt
# ---------------------------------------------------------------------------


class TestSystemPrompt:
    @pytest.mark.asyncio
    async def test_wire_session_new_delivers_system_prompt_to_the_session(self, agent):
        async with _WireClient(agent) as client:
            reply = await client.call(
                "session/new", {"cwd": "/tmp", "mcpServers": [], "systemPrompt": APPEND}
            )

        state = agent.session_manager.get_session(reply["result"]["sessionId"])
        assert state.agent.ephemeral_system_prompt == "Focus on security review."

    @pytest.mark.asyncio
    async def test_multiple_text_blocks_join_in_order(self, agent):
        prompt = {
            "mode": "append",
            "content": [{"type": "text", "text": "First."}, {"type": "text", "text": "Second."}],
        }
        resp = await agent.new_session(cwd="/tmp", system_prompt=prompt)
        state = agent.session_manager.get_session(resp.session_id)
        assert state.agent.ephemeral_system_prompt == "First.\n\nSecond."

    @pytest.mark.asyncio
    async def test_load_and_resume_reapply_a_supplied_prompt(self, agent):
        resp = await agent.new_session(cwd="/tmp", system_prompt=APPEND)
        sid = resp.session_id
        new_prompt = {"mode": "append", "content": [{"type": "text", "text": "Now docs."}]}

        await agent.load_session(cwd="/tmp", session_id=sid, system_prompt=new_prompt)
        assert agent.session_manager.get_session(sid).agent.ephemeral_system_prompt == "Now docs."

        await agent.resume_session(cwd="/tmp", session_id=sid, system_prompt=APPEND)
        assert (
            agent.session_manager.get_session(sid).agent.ephemeral_system_prompt
            == "Focus on security review."
        )

    @pytest.mark.asyncio
    async def test_load_without_prompt_keeps_the_stored_one(self, agent):
        resp = await agent.new_session(cwd="/tmp", system_prompt=APPEND)
        await agent.load_session(cwd="/tmp", session_id=resp.session_id)
        state = agent.session_manager.get_session(resp.session_id)
        assert state.agent.ephemeral_system_prompt == "Focus on security review."

    @pytest.mark.asyncio
    async def test_prompt_survives_a_process_restart(self, agent, manager):
        resp = await agent.new_session(cwd="/tmp", system_prompt=APPEND)
        sid = resp.session_id
        manager.save_session(sid)

        fresh = SessionManager(agent_factory=_agent_stub, db=manager._get_db())
        restarted = HermesACPAgent(session_manager=fresh)
        await restarted.resume_session(cwd="/tmp", session_id=sid)
        assert fresh.get_session(sid).agent.ephemeral_system_prompt == "Focus on security review."

    @pytest.mark.asyncio
    async def test_model_switch_keeps_the_prompt(self, agent):
        resp = await agent.new_session(cwd="/tmp", system_prompt=APPEND)
        await agent.set_session_model(model_id="other-model", session_id=resp.session_id)
        state = agent.session_manager.get_session(resp.session_id)
        assert state.agent.ephemeral_system_prompt == "Focus on security review."

    @pytest.mark.asyncio
    async def test_fork_inherits_the_prompt(self, agent):
        resp = await agent.new_session(cwd="/tmp", system_prompt=APPEND)
        forked = await agent.fork_session(cwd="/tmp", session_id=resp.session_id)
        state = agent.session_manager.get_session(forked.session_id)
        assert state.agent.ephemeral_system_prompt == "Focus on security review."

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param({"mode": "override", "content": [{"type": "text", "text": "x"}]}, id="override"),
            pytest.param({"mode": "_custom", "content": [{"type": "text", "text": "x"}]}, id="unknown-mode"),
            pytest.param({"mode": "append", "content": []}, id="empty-content"),
            pytest.param(
                {"mode": "append", "content": [{"type": "image", "data": "AA==", "mimeType": "image/png"}]},
                id="non-text-block",
            ),
            pytest.param({"content": [{"type": "text", "text": "x"}]}, id="missing-mode"),
            pytest.param("plain string", id="not-an-object"),
        ],
    )
    @pytest.mark.asyncio
    async def test_unsupported_prompt_is_rejected_without_creating_a_session(self, agent, bad):
        before = {s["session_id"] for s in agent.session_manager.list_sessions()}
        with pytest.raises(RequestError) as exc:
            await agent.new_session(cwd="/tmp", system_prompt=bad)
        assert exc.value.code == -32602
        assert not agent.session_manager._sessions
        assert {s["session_id"] for s in agent.session_manager.list_sessions()} == before

    @pytest.mark.asyncio
    async def test_unsupported_prompt_on_load_leaves_the_session_untouched(self, agent):
        resp = await agent.new_session(cwd="/tmp", system_prompt=APPEND)
        with pytest.raises(RequestError) as exc:
            await agent.load_session(
                cwd="/tmp",
                session_id=resp.session_id,
                system_prompt={"mode": "override", "content": [{"type": "text", "text": "x"}]},
            )
        assert exc.value.code == -32602
        state = agent.session_manager.get_session(resp.session_id)
        assert state.agent.ephemeral_system_prompt == "Focus on security review."

    @pytest.mark.asyncio
    async def test_override_over_the_wire_returns_invalid_params(self, agent):
        async with _WireClient(agent) as client:
            reply = await client.call(
                "session/new",
                {
                    "cwd": "/tmp",
                    "mcpServers": [],
                    "systemPrompt": {"mode": "override", "content": [{"type": "text", "text": "x"}]},
                },
            )
        assert reply["error"]["code"] == -32602
        assert not agent.session_manager._sessions


# ---------------------------------------------------------------------------
# additionalDirectories
# ---------------------------------------------------------------------------


class TestAdditionalDirectories:
    @pytest.mark.asyncio
    async def test_wire_session_new_records_the_extra_roots(self, agent, tmp_path):
        extra = tmp_path / "shared-lib"
        extra.mkdir()
        async with _WireClient(agent) as client:
            reply = await client.call(
                "session/new",
                {"cwd": "/tmp", "mcpServers": [], "additionalDirectories": [str(extra)]},
            )
        state = agent.session_manager.get_session(reply["result"]["sessionId"])
        assert state.additional_directories == [str(extra)]

    @pytest.mark.asyncio
    async def test_roots_are_named_to_the_model_at_system_level(self, agent, tmp_path):
        extra = tmp_path / "product-docs"
        extra.mkdir()
        resp = await agent.new_session(cwd="/tmp", additional_directories=[str(extra)])
        prompt = agent.session_manager.get_session(resp.session_id).agent.ephemeral_system_prompt
        assert str(extra) in prompt
        assert "/tmp" in prompt

    @pytest.mark.asyncio
    async def test_roots_and_prompt_combine(self, agent, tmp_path):
        extra = tmp_path / "lib"
        extra.mkdir()
        resp = await agent.new_session(
            cwd="/tmp", additional_directories=[str(extra)], system_prompt=APPEND
        )
        prompt = agent.session_manager.get_session(resp.session_id).agent.ephemeral_system_prompt
        assert "Focus on security review." in prompt
        assert str(extra) in prompt

    @pytest.mark.asyncio
    async def test_skills_under_a_root_are_discoverable_in_the_session(self, agent, tmp_path):
        from agent.skill_utils import get_all_skills_dirs, session_skill_dirs_scope

        root = tmp_path / "client-skills"
        _skill(root, "client-provided-skill")
        resp = await agent.new_session(cwd="/tmp", additional_directories=[str(root)])
        state = agent.session_manager.get_session(resp.session_id)

        with session_skill_dirs_scope(state.skill_dirs):
            assert root.resolve() in get_all_skills_dirs()
        # Outside the session's scope the root is not a skills dir.
        assert root.resolve() not in get_all_skills_dirs()

    @pytest.mark.asyncio
    async def test_conventional_skill_subdirs_of_a_root_are_discovered(self, agent, tmp_path):
        workspace = tmp_path / "other-repo"
        _skill(workspace / ".agents" / "skills", "repo-skill")
        resp = await agent.new_session(cwd="/tmp", additional_directories=[str(workspace)])
        state = agent.session_manager.get_session(resp.session_id)
        assert (workspace / ".agents" / "skills").resolve() in state.skill_dirs

    @pytest.mark.parametrize("subdir", [".hermes/skills", ".agents/skills", ".claude/skills", "skills"])
    def test_categorized_skill_subdirs_of_a_root_are_discovered(self, tmp_path, subdir):
        """``<skills>/<category>/<name>/SKILL.md`` is the standard layout."""
        from acp_adapter.session import skill_dirs_for_roots

        workspace = tmp_path / "other-repo"
        _skill(workspace / subdir / "devops", "categorized-skill")
        assert skill_dirs_for_roots([str(workspace)]) == [(workspace / subdir).resolve()]

    def test_skill_md_only_in_excluded_dirs_does_not_count(self, tmp_path):
        from acp_adapter.session import skill_dirs_for_roots

        workspace = tmp_path / "other-repo"
        _skill(workspace / "skills" / "node_modules" / "pkg", "vendored")
        _skill(workspace / "skills" / ".git" / "x", "vcs")
        assert skill_dirs_for_roots([str(workspace)]) == []

    def test_bare_root_needs_skills_one_level_down(self, tmp_path):
        """A bare root is not walked recursively: a code checkout with a stray
        nested SKILL.md must not turn the whole repo into a skills dir."""
        from acp_adapter.session import skill_dirs_for_roots

        flat = tmp_path / "flat"
        _skill(flat, "x")
        deep = tmp_path / "code-repo"
        _skill(deep / "docs" / "examples", "x")
        assert skill_dirs_for_roots([str(flat), str(deep)]) == [flat.resolve()]

    @pytest.mark.asyncio
    async def test_root_skills_reach_the_skills_index_inside_a_turn(self, agent, tmp_path):
        """The skills index the model sees is built inside the turn's context."""
        from agent.prompt_builder import build_skills_system_prompt

        root = tmp_path / "turn-skills"
        _skill(root, "only-in-acp-root")
        resp = await agent.new_session(cwd="/tmp", additional_directories=[str(root)])
        state = agent.session_manager.get_session(resp.session_id)

        seen = {}

        def _run_conversation(**_kwargs):
            seen["index"] = build_skills_system_prompt()
            return {"final_response": "ok", "messages": []}

        state.agent.run_conversation = _run_conversation
        state.agent.interrupt = lambda: None
        await agent.prompt(
            prompt=[acp.schema.TextContentBlock(type="text", text="hi")],
            session_id=resp.session_id,
        )
        assert "only-in-acp-root" in seen["index"]
        assert "only-in-acp-root" not in build_skills_system_prompt()

    @pytest.mark.asyncio
    async def test_categorized_root_skills_reach_the_skills_index(self, agent, tmp_path):
        from agent.prompt_builder import build_skills_system_prompt

        workspace = tmp_path / "categorized-repo"
        _skill(workspace / "skills" / "devops", "categorized-acp-skill")
        resp = await agent.new_session(cwd="/tmp", additional_directories=[str(workspace)])
        state = agent.session_manager.get_session(resp.session_id)

        seen = {}

        def _run_conversation(**_kwargs):
            seen["index"] = build_skills_system_prompt()
            return {"final_response": "ok", "messages": []}

        state.agent.run_conversation = _run_conversation
        state.agent.interrupt = lambda: None
        await agent.prompt(
            prompt=[acp.schema.TextContentBlock(type="text", text="hi")],
            session_id=resp.session_id,
        )
        assert "categorized-acp-skill" in seen["index"]

    @pytest.mark.asyncio
    async def test_extra_roots_widen_the_workspace_edit_boundary(self, agent, tmp_path, monkeypatch):
        from acp_adapter import edit_approval
        from acp_adapter.edit_approval import AUTO_APPROVE_WORKSPACE, EditProposal, should_auto_approve_edit

        # tmp_path sits under the system temp dir, which the workspace policy
        # always allows; point that allowance elsewhere so the roots decide.
        unrelated_tmp = tmp_path / "system-tmp"
        unrelated_tmp.mkdir()
        monkeypatch.setattr(edit_approval.tempfile, "gettempdir", lambda: str(unrelated_tmp))
        cwd = tmp_path / "project"
        extra = tmp_path / "shared"
        outside = tmp_path / "elsewhere"
        for d in (cwd, extra, outside):
            d.mkdir()
        resp = await agent.new_session(cwd=str(cwd), additional_directories=[str(extra)])
        state = agent.session_manager.get_session(resp.session_id)
        policy_roots = agent._edit_approval_policy_for_state(state)[1]

        def approved(path):
            proposal = EditProposal(
                tool_name="write_file", path=str(path), old_text="", new_text="x", arguments={}
            )
            return should_auto_approve_edit(proposal, AUTO_APPROVE_WORKSPACE, policy_roots)

        assert approved(cwd / "a.py")
        assert approved(extra / "b.py")
        assert not approved(outside / "c.py")

    @pytest.mark.asyncio
    async def test_load_replaces_roots_and_omission_clears_them(self, agent, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        resp = await agent.new_session(cwd="/tmp", additional_directories=[str(a)])
        sid = resp.session_id

        await agent.load_session(cwd="/tmp", session_id=sid, additional_directories=[str(b)])
        assert agent.session_manager.get_session(sid).additional_directories == [str(b)]

        # Spec: omitting the field on load/resume activates no additional roots.
        await agent.resume_session(cwd="/tmp", session_id=sid)
        state = agent.session_manager.get_session(sid)
        assert state.additional_directories == []
        assert str(b) not in (state.agent.ephemeral_system_prompt or "")

    @pytest.mark.asyncio
    async def test_list_sessions_reports_the_roots(self, agent, tmp_path):
        extra = tmp_path / "x"
        extra.mkdir()
        resp = await agent.new_session(cwd="/tmp", additional_directories=[str(extra)])
        state = agent.session_manager.get_session(resp.session_id)
        state.history.append({"role": "user", "content": "hello"})
        listing = await agent.list_sessions()
        info = next(s for s in listing.sessions if s.session_id == resp.session_id)
        assert info.additional_directories == [str(extra)]

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param(["relative/path"], id="relative"),
            pytest.param("/not/a/list", id="not-a-list"),
            pytest.param([""], id="empty-entry"),
        ],
    )
    @pytest.mark.asyncio
    async def test_malformed_roots_are_rejected_without_creating_a_session(self, agent, bad):
        with pytest.raises(RequestError) as exc:
            await agent.new_session(cwd="/tmp", additional_directories=bad)
        assert exc.value.code == -32602
        assert not agent.session_manager._sessions


# ---------------------------------------------------------------------------
# No regression for clients that send neither field
# ---------------------------------------------------------------------------


class TestDefaultsUnchanged:
    @pytest.mark.asyncio
    async def test_plain_session_has_no_ephemeral_prompt_and_no_extra_roots(self, agent):
        resp = await agent.new_session(cwd="/tmp")
        state = agent.session_manager.get_session(resp.session_id)
        assert state.agent.ephemeral_system_prompt is None
        assert state.additional_directories == []
        assert state.skill_dirs == []
        assert agent._edit_approval_policy_for_state(state)[1] == state.cwd

    @pytest.mark.asyncio
    async def test_plain_wire_session_and_load(self, agent):
        async with _WireClient(agent) as client:
            new = await client.call("session/new", {"cwd": "/tmp", "mcpServers": []})
            sid = new["result"]["sessionId"]
            loaded = await client.call(
                "session/load", {"cwd": "/tmp", "sessionId": sid, "mcpServers": []}
            )
        assert "error" not in new and "error" not in loaded
        state = agent.session_manager.get_session(sid)
        assert state.agent.ephemeral_system_prompt is None

    @pytest.mark.asyncio
    async def test_null_system_prompt_is_the_default(self, agent):
        resp = await agent.new_session(cwd="/tmp", system_prompt=None)
        state = agent.session_manager.get_session(resp.session_id)
        assert state.agent.ephemeral_system_prompt is None

    def test_skill_dirs_without_a_scope_are_unchanged(self):
        from agent.skill_utils import get_all_skills_dirs, get_external_skills_dirs, get_skills_dir

        assert get_all_skills_dirs() == [get_skills_dir(), *get_external_skills_dirs()]

    def test_skill_scope_is_isolated_between_concurrent_contexts(self, tmp_path):
        import contextvars
        import threading

        from agent.skill_utils import get_external_skills_dirs, session_skill_dirs_scope

        a, b = tmp_path / "a", tmp_path / "b"
        _skill(a, "skill-a")
        _skill(b, "skill-b")
        barrier = threading.Barrier(2)
        seen = {}

        def turn(name, root):
            with session_skill_dirs_scope([root]):
                barrier.wait(timeout=5)
                seen[name] = get_external_skills_dirs()

        threads = [
            threading.Thread(target=contextvars.copy_context().run, args=(turn, "a", a)),
            threading.Thread(target=contextvars.copy_context().run, args=(turn, "b", b)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert a.resolve() in seen["a"] and b.resolve() not in seen["a"]
        assert b.resolve() in seen["b"] and a.resolve() not in seen["b"]


# ---------------------------------------------------------------------------
# The client prompt reaches the model as a system message (real AIAgent)
# ---------------------------------------------------------------------------


def _response(content="done"):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


def test_client_prompt_is_in_the_system_message_not_the_user_turn(tmp_path, monkeypatch):
    from acp_adapter.session import _client_setup_prompt
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        real = AIAgent(
            session_id="acp-system-prompt",
            api_key="k",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="test/model",
            max_iterations=2,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    real._cached_system_prompt = "HERMES CORE PROMPT"
    real._session_db = None
    real._session_json_enabled = False
    real.save_trajectories = False
    real.compression_enabled = False
    real._cleanup_task_resources = lambda *_a, **_kw: None
    real._save_trajectory = lambda *_a, **_kw: None

    real.ephemeral_system_prompt = _client_setup_prompt("Focus on security review.", [], "/tmp")

    captured = []

    def model_call(api_kwargs):
        captured.append(api_kwargs["messages"])
        return _response()

    real._interruptible_api_call = model_call
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        result = real.run_conversation("review this diff")

    messages = captured[0]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].startswith("HERMES CORE PROMPT")
    assert "Focus on security review." in messages[0]["content"]
    user_turns = [m for m in messages if m["role"] == "user"]
    assert user_turns and all("Focus on security review." not in str(m["content"]) for m in user_turns)
    # Ephemeral: never persisted into the transcript.
    assert all("Focus on security review." not in str(m.get("content")) for m in result["messages"])
