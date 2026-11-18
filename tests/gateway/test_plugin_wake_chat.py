"""``ctx.wake_chat(origin=..., wake_id=...)``: a plugin's producer wake to a known chat rides the same
internal-event admission the Kanban notifier's wake uses (``gateway.wake.deliver_wake``), so it
reaches a chat that holds no committed human turn yet, unlike ``inject_message(origin=...)``.
Profile ownership and current user authorization still apply, the answer is the real admission
receipt, and a repeated ``wake_id`` for the same chat is not delivered twice. Real GatewayRunner,
DiscordAdapter and SessionAuthority.
"""
import asyncio

import pytest
import hermes_yaml as yaml

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from tests.waiting import async_wait_until

pytestmark = pytest.mark.usefixtures("authority_cwd_in_tmp")

PLUGIN = "desk"


def _context(home, *, allow=True) -> tuple[PluginContext, PluginManager]:
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"entries": {PLUGIN: {"allow_gateway_injection": allow}}},
    }), encoding="utf-8")
    manager = PluginManager(scope_key=str(home))
    return PluginContext(PluginManifest(name=PLUGIN, key=PLUGIN, source="user"), manager), manager


async def _gateway(monkeypatch):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_results import execution_result
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fixture-token", typing_indicator=False))

    async def send(chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="sent")
    monkeypatch.setattr(adapter, "send", send)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="owner")
    runner._wire_adapter_handlers(adapter)
    executed = []

    async def turn(event, source, quick_key, run_generation):
        executed.append(event.text)
        captured = execution_result.get()
        if captured is not None:
            captured["result"] = {"final_response": "reply", "messages": []}
        return "reply"
    monkeypatch.setattr(runner, "_handle_message_with_agent", turn)
    runner._gateway_loop = asyncio.get_running_loop()
    runner._running = True
    return runner, adapter, authority, executed


ORIGIN = {"platform": "discord", "chat_id": "T1", "chat_type": "thread", "thread_id": "T1",
          "parent_chat_id": "P1", "user_id": "42"}


@pytest.mark.asyncio
async def test_a_wake_reaches_a_chat_with_no_human_turn_admitted(monkeypatch):
    from hermes_constants import get_hermes_home
    from gateway.config import Platform
    from gateway.session import SessionSource
    runner, _adapter, authority, executed = await _gateway(monkeypatch)
    ctx, manager = _context(get_hermes_home())
    manager.set_gateway_message_injector(runner, runner._schedule_plugin_message_injection)
    source = SessionSource(platform=Platform.DISCORD, chat_id="T1", chat_type="thread", thread_id="T1",
                           parent_chat_id="P1", user_id="42")
    # The cutover condition: the thread has a session, and no human message was ever admitted.
    entry = runner.session_store.get_or_create_session(source)
    from gateway.session_automation import committed_route_envelope
    assert committed_route_envelope(authority, entry) is None

    # inject_message still refuses it (a plugin can continue a chat, not open one) ...
    assert await asyncio.to_thread(ctx.inject_message, "chat", origin=ORIGIN) is False
    # ... while a wake is admitted the way the Kanban notifier's own wake is.
    assert await asyncio.to_thread(ctx.wake_chat, "READY FOR YOU", origin=ORIGIN, wake_id="evt-1") is True
    await async_wait_until(lambda: "READY FOR YOU" in executed, timeout=10, what="the wake turn")


@pytest.mark.asyncio
async def test_a_wake_is_idempotent_on_its_wake_id(monkeypatch):
    from hermes_constants import get_hermes_home
    runner, _adapter, _authority, executed = await _gateway(monkeypatch)
    ctx, manager = _context(get_hermes_home())
    manager.set_gateway_message_injector(runner, runner._schedule_plugin_message_injection)

    assert await asyncio.to_thread(ctx.wake_chat, "once", origin=ORIGIN, wake_id="evt-7") is True
    await async_wait_until(lambda: "once" in executed, timeout=10, what="the wake turn")
    # The same wake again (a retry after a lost receipt) answers True and is not delivered again.
    assert await asyncio.to_thread(ctx.wake_chat, "once", origin=ORIGIN, wake_id="evt-7") is True
    await asyncio.sleep(0.3)
    assert executed.count("once") == 1
    # A different wake id is a different wake.
    assert await asyncio.to_thread(ctx.wake_chat, "once", origin=ORIGIN, wake_id="evt-8") is True
    await async_wait_until(lambda: executed.count("once") == 2, timeout=10, what="the second wake")


def _counting_deliver(monkeypatch, *, delay=0.0, hold=None, refuse=0):
    """Wrap the real ``gateway.wake.deliver_wake``: count calls, slow them by *delay*, refuse the
    first *refuse* calls. A call whose text is in *hold* (text -> asyncio.Event) parks until
    released and then counts as admitted without a real admission (a second wake into a session
    whose turn is still running may be refused as busy, which is not what that test measures)."""
    import gateway.wake as wake
    real = wake.deliver_wake
    calls = []

    async def deliver(adapter, *, text, **kwargs):
        calls.append(text)
        if len(calls) <= refuse:
            raise wake.WakeNotAccepted("refused by the fixture")
        if hold and text in hold:
            await hold[text].wait()
            return None
        if delay:
            await asyncio.sleep(delay)
        return await real(adapter, text=text, **kwargs)
    monkeypatch.setattr(wake, "deliver_wake", deliver)
    return calls


@pytest.mark.asyncio
async def test_concurrent_dispatches_of_one_wake_id_deliver_once(monkeypatch):
    from hermes_constants import get_hermes_home
    runner, _adapter, _authority, executed = await _gateway(monkeypatch)
    ctx, manager = _context(get_hermes_home())
    manager.set_gateway_message_injector(runner, runner._schedule_plugin_message_injection)
    calls = _counting_deliver(monkeypatch, delay=0.5)

    results = await asyncio.gather(*(asyncio.to_thread(ctx.wake_chat, "race", origin=ORIGIN, wake_id="evt-9")
                                     for _ in range(2)))
    assert results == [True, True]
    await async_wait_until(lambda: "race" in executed, timeout=10, what="the wake turn")
    await asyncio.sleep(0.3)
    assert calls == ["race"]
    assert executed.count("race") == 1


@pytest.mark.asyncio
async def test_a_refused_wake_clears_its_id_so_a_retry_delivers(monkeypatch):
    from hermes_constants import get_hermes_home
    runner, _adapter, _authority, executed = await _gateway(monkeypatch)
    ctx, manager = _context(get_hermes_home())
    manager.set_gateway_message_injector(runner, runner._schedule_plugin_message_injection)
    calls = _counting_deliver(monkeypatch, refuse=1)

    assert await asyncio.to_thread(ctx.wake_chat, "again", origin=ORIGIN, wake_id="evt-3") is False
    assert executed == []
    assert await asyncio.to_thread(ctx.wake_chat, "again", origin=ORIGIN, wake_id="evt-3") is True
    await async_wait_until(lambda: "again" in executed, timeout=10, what="the retried wake turn")
    assert calls == ["again", "again"]
    assert executed.count("again") == 1


@pytest.mark.asyncio
async def test_a_retry_after_a_receipt_timeout_does_not_deliver_again(monkeypatch):
    import gateway.run_plugin_injection as injection
    from hermes_constants import get_hermes_home
    runner, _adapter, _authority, executed = await _gateway(monkeypatch)
    ctx, manager = _context(get_hermes_home())
    manager.set_gateway_message_injector(runner, runner._schedule_plugin_message_injection)
    monkeypatch.setattr(injection, "_PLUGIN_WAKE_RECEIPT_TIMEOUT", 0.2)
    calls = _counting_deliver(monkeypatch, delay=1.0)

    # The receipt is lost; the delivery is left running on the loop.
    assert await asyncio.to_thread(ctx.wake_chat, "late", origin=ORIGIN, wake_id="evt-5") is False
    # The plugin retries while it is still in flight: no second delivery starts.
    assert await asyncio.to_thread(ctx.wake_chat, "late", origin=ORIGIN, wake_id="evt-5") is False
    await async_wait_until(lambda: "late" in executed, timeout=10, what="the late wake turn")
    # Once it is admitted, a retry answers True from memory.
    assert await asyncio.to_thread(ctx.wake_chat, "late", origin=ORIGIN, wake_id="evt-5") is True
    await asyncio.sleep(0.3)
    assert calls == ["late"]
    assert executed.count("late") == 1


@pytest.mark.asyncio
async def test_different_wake_ids_do_not_wait_on_each_other(monkeypatch):
    from hermes_constants import get_hermes_home
    runner, _adapter, _authority, executed = await _gateway(monkeypatch)
    ctx, manager = _context(get_hermes_home())
    manager.set_gateway_message_injector(runner, runner._schedule_plugin_message_injection)
    release = asyncio.Event()
    calls = _counting_deliver(monkeypatch, hold={"first": release})

    first = asyncio.ensure_future(asyncio.to_thread(ctx.wake_chat, "first", origin=ORIGIN, wake_id="evt-A"))
    await async_wait_until(lambda: "first" in calls, timeout=10, what="the first delivery to start")
    # evt-A is parked in its delivery; evt-B is admitted without waiting for it.
    assert await asyncio.wait_for(
        asyncio.to_thread(ctx.wake_chat, "second", origin=ORIGIN, wake_id="evt-B"), timeout=5) is True
    assert not first.done()
    release.set()
    assert await first is True
    assert sorted(calls) == ["first", "second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [
    {k: v for k, v in ORIGIN.items() if k != "user_id"},   # no user: nothing to authorize
    {**ORIGIN, "user_id": "666"},                          # a user the gateway does not allow
])
async def test_a_wake_for_an_unauthorized_or_userless_origin_is_refused(monkeypatch, caplog, origin):
    import logging
    from hermes_constants import get_hermes_home
    caplog.set_level(logging.WARNING, logger="gateway.run")
    runner, _adapter, _authority, executed = await _gateway(monkeypatch)
    ctx, manager = _context(get_hermes_home())
    manager.set_gateway_message_injector(runner, runner._schedule_plugin_message_injection)

    assert await asyncio.to_thread(ctx.wake_chat, "nope", origin=origin, wake_id="evt-1") is False
    await asyncio.sleep(0.3)
    assert executed == []
    why = "names no user_id" if "user_id" not in origin else "denied by current gateway authorization"
    assert why in caplog.text, caplog.text


@pytest.mark.asyncio
async def test_a_wake_needs_the_injection_grant_an_origin_and_a_wake_id(monkeypatch):
    from hermes_constants import get_hermes_home
    runner, _adapter, _authority, executed = await _gateway(monkeypatch)
    ctx, manager = _context(get_hermes_home(), allow=False)
    manager.set_gateway_message_injector(runner, runner._schedule_plugin_message_injection)
    assert await asyncio.to_thread(ctx.wake_chat, "x", origin=ORIGIN, wake_id="evt-1") is False

    ctx, manager = _context(get_hermes_home())
    manager.set_gateway_message_injector(runner, runner._schedule_plugin_message_injection)
    assert await asyncio.to_thread(ctx.wake_chat, "x", origin=ORIGIN, wake_id="") is False
    assert await asyncio.to_thread(ctx.wake_chat, "x", origin="discord/T1", wake_id="evt-1") is False
    # Another profile's chat is never woken.
    assert await asyncio.to_thread(ctx.wake_chat, "x", origin={**ORIGIN, "profile": "other"},
                                   wake_id="evt-1") is False
    await asyncio.sleep(0.3)
    assert executed == []


@pytest.mark.asyncio
async def test_a_wake_called_on_the_gateway_loop_is_refused_instead_of_deadlocking(monkeypatch):
    from hermes_constants import get_hermes_home
    runner, _adapter, _authority, executed = await _gateway(monkeypatch)
    ctx, manager = _context(get_hermes_home())
    manager.set_gateway_message_injector(runner, runner._schedule_plugin_message_injection)
    # The receipt is awaited on the loop, so a caller ON the loop could only block it forever.
    assert ctx.wake_chat("x", origin=ORIGIN, wake_id="evt-1") is False
    await asyncio.sleep(0.3)
    assert executed == []


def test_no_live_gateway_refuses_a_wake(tmp_path):
    ctx, _manager = _context(tmp_path)
    assert ctx.wake_chat("x", origin=ORIGIN, wake_id="evt-1") is False
