"""``/restart`` while one is already waiting says what it waits on and how to force it;
``/restart now`` forces through ``force_restart`` (the ``hermes update --force`` path)."""

import time
from unittest.mock import MagicMock

import pytest

import gateway.restart_when_idle as rwi
import gateway.run as gateway_run
from gateway.platforms.base import EphemeralReply
from gateway.platforms.event import MessageEvent, MessageType
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _event(text: str) -> MessageEvent:
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=make_restart_source(), message_id="m1")


def _text(reply) -> str:
    return reply.text if isinstance(reply, EphemeralReply) else str(reply)


@pytest.mark.asyncio
async def test_a_second_restart_names_what_the_wait_holds_and_how_to_force(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, _adapter = make_restart_runner()
    runner._restart_requested = runner._restart_task_started = True
    runner._restart_wait_started_at = time.time() - 23401
    runner._describe_active_work = lambda: [
        {"kind": "process", "process_id": "p1"}, {"kind": "process", "process_id": "p2"},
        {"kind": "chat", "session": "s1"}]

    reply = _text(await runner._handle_restart_command(_event("/restart")))

    assert "3" in reply and "2 process" in reply and "1 chat" in reply
    assert "6h30m" in reply  # how long it has waited
    assert "/restart now" in reply


@pytest.mark.asyncio
async def test_restart_now_escalates_a_waiting_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, _adapter = make_restart_runner()
    runner._restart_requested = runner._restart_task_started = True
    runner._restart_detached, runner._restart_via_service = True, False
    forced = MagicMock(return_value=True)
    monkeypatch.setattr(rwi, "force_restart", forced)

    await runner._handle_restart_command(_event("/restart now"))

    forced.assert_called_once_with(runner, detached=True, via_service=False)


@pytest.mark.asyncio
async def test_restart_now_with_live_work_forces_instead_of_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, _adapter = make_restart_runner()
    runner._running_agent_count = lambda: 2  # busy: a plain /restart would wait for idle
    runner.request_restart = MagicMock()
    forced = MagicMock(return_value=True)
    monkeypatch.setattr(rwi, "force_restart", forced)

    await runner._handle_restart_command(_event("/restart now"))

    forced.assert_called_once()
    runner.request_restart.assert_not_called()
    assert (tmp_path / ".restart_notify.json").exists()  # the requester still hears back


@pytest.mark.asyncio
async def test_a_plain_restart_still_waits_for_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, _adapter = make_restart_runner()
    runner._running_agent_count = lambda: 2
    runner.request_restart = MagicMock()
    forced = MagicMock()
    monkeypatch.setattr(rwi, "force_restart", forced)

    await runner._handle_restart_command(_event("/restart"))

    runner.request_restart.assert_called_once()
    forced.assert_not_called()
