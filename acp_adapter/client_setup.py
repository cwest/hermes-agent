"""Client-provided session setup: ACP ``systemPrompt`` and ``additionalDirectories``.

ACP v1 lets a client send system-level instructions (``systemPrompt``) and
extra workspace roots (``additionalDirectories``) on ``session/new``,
``session/load`` and ``session/resume`` once the agent advertises the matching
``sessionCapabilities``. ``additionalDirectories`` is in the installed SDK's
schema; ``systemPrompt`` was stabilized in the protocol after the SDK release
this adapter pins, so its wire types live here. They mirror the canonical
schema definitions (``SystemPrompt``, ``SessionSystemPromptCapabilities``) and
drop out once the SDK ships them.

Hermes supports ``append`` mode only. ``override`` would replace Hermes' own
instructions — the tool guidance and safety scaffolding the rest of the agent
depends on — so it is neither advertised nor accepted.
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Optional

from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    BaseModel,
    InitializeResponse,
    LoadSessionRequest,
    NewSessionRequest,
    ResumeSessionRequest,
    SessionCapabilities,
)
from pydantic import Field

SYSTEM_PROMPT_APPEND = "append"


class SessionSystemPromptCapabilities(BaseModel):
    """``sessionCapabilities.systemPrompt``; ``{}`` means append mode only."""

    field_meta: Annotated[Optional[Dict[str, Any]], Field(alias="_meta")] = None


class HermesSessionCapabilities(SessionCapabilities):
    system_prompt: Annotated[
        Optional[SessionSystemPromptCapabilities], Field(alias="systemPrompt")
    ] = None


class HermesAgentCapabilities(AgentCapabilities):
    session_capabilities: Annotated[  # type: ignore[assignment]
        Optional[HermesSessionCapabilities], Field(alias="sessionCapabilities")
    ] = None


class HermesInitializeResponse(InitializeResponse):
    agent_capabilities: Annotated[  # type: ignore[assignment]
        Optional[HermesAgentCapabilities], Field(alias="agentCapabilities")
    ] = None


# Request models with the ``systemPrompt`` field. Kept as raw JSON (``Any``) so
# an unsupported value reaches the handler and is rejected with -32602 as the
# spec requires, instead of being dropped by model validation.
class HermesNewSessionRequest(NewSessionRequest):
    system_prompt: Annotated[Optional[Any], Field(alias="systemPrompt")] = None


class HermesLoadSessionRequest(LoadSessionRequest):
    system_prompt: Annotated[Optional[Any], Field(alias="systemPrompt")] = None


class HermesResumeSessionRequest(ResumeSessionRequest):
    system_prompt: Annotated[Optional[Any], Field(alias="systemPrompt")] = None


def _invalid(field: str, message: str) -> RequestError:
    return RequestError.invalid_params({"field": field, "message": message})


def parse_system_prompt(value: Any) -> Optional[str]:
    """Validate a client ``systemPrompt`` and return its append text.

    ``None`` (omitted or null) returns ``None``. Anything Hermes cannot apply
    raises ``RequestError(-32602)`` so the caller can refuse the request
    before creating, loading or resuming the session.
    """
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump(by_alias=True)
    if not isinstance(value, dict):
        raise _invalid("systemPrompt", "must be an object with mode and content")
    mode = value.get("mode")
    if mode != SYSTEM_PROMPT_APPEND:
        raise _invalid("systemPrompt.mode", f"unsupported mode {mode!r}; only 'append' is supported")
    content = value.get("content")
    if not isinstance(content, list) or not content:
        raise _invalid("systemPrompt.content", "must be a non-empty array of content blocks")
    texts: List[str] = []
    for block in content:
        if hasattr(block, "model_dump"):
            block = block.model_dump(by_alias=True)
        if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
            raise _invalid("systemPrompt.content", "only text content blocks are supported")
        texts.append(block["text"])
    return "\n\n".join(t.strip() for t in texts if t.strip())


def parse_additional_directories(value: Any) -> Optional[List[str]]:
    """Validate ``additionalDirectories`` and return the absolute paths.

    ``None`` (omitted) returns ``None``; an empty list returns ``[]``. Entries
    must be absolute paths; duplicates are dropped, order is kept.
    """
    import os

    if value is None:
        return None
    if not isinstance(value, list):
        raise _invalid("additionalDirectories", "must be an array of absolute paths")
    roots: List[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip() or not os.path.isabs(entry):
            raise _invalid("additionalDirectories", f"entry {entry!r} is not an absolute path")
        if entry not in roots:
            roots.append(entry)
    return roots
