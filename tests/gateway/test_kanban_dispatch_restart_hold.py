"""While a restart is pending, the embedded kanban dispatcher spawns no new workers.

A restart waits for idle; on a busy board the dispatcher kept spawning workers (each one live work)
for as long as the restart waited, so idle never came. Held ticks leave ready cards ready.
"""

import asyncio

import pytest

from gateway.run import GatewayRunner


def _run_watcher(monkeypatch, *, restart_requested: bool, ticks: int = 3) -> list[str]:
    import gateway.kanban_watchers as kw
    import hermes_cli.config as _cfg_mod

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._draining = False
    runner._restart_requested = restart_requested
    calls: list[str] = []
    passes = {"n": 0}

    class _FakeDispatcher:
        def __init__(self, *_a, **_k):
            pass

        def auto_decompose_tick(self, *_a):
            calls.append("auto_decompose")

        def tick_once(self):
            calls.append("tick_once")  # this is what spawns workers
            return []

        def ready_nonempty(self):
            return True

    async def _to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def _no_sleep(_delay):
        return None

    async def _between(_interval):
        passes["n"] += 1
        if passes["n"] >= ticks:
            runner._running = False

    monkeypatch.setattr(_cfg_mod, "load_config", lambda: {"kanban": {"dispatch_in_gateway": True}})
    monkeypatch.setattr(kw, "_acquire_singleton_lock", lambda _p: (None, "unavailable"))
    monkeypatch.setattr(kw, "_KanbanDispatcher", _FakeDispatcher)
    monkeypatch.setattr(kw, "_to_thread_process_service", _to_thread)
    monkeypatch.setattr(kw, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(kw, "_resolve_auto_decompose_settings", lambda _lc: (True, 1))
    monkeypatch.setattr("gateway.run.asyncio.sleep", _no_sleep)
    runner._sleep_between_ticks = _between
    asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=5))
    return calls


@pytest.mark.parametrize("restart_requested", [False, True])
def test_dispatch_spawns_only_when_no_restart_is_pending(monkeypatch, restart_requested):
    calls = _run_watcher(monkeypatch, restart_requested=restart_requested)
    if restart_requested:
        assert calls == []  # nothing spawned, nothing auto-decomposed: the board can go idle
    else:
        assert calls.count("tick_once") == 3


def test_a_draining_gateway_spawns_no_workers_either(monkeypatch):
    import gateway.kanban_watchers as kw

    runner = object.__new__(GatewayRunner)
    runner._restart_requested, runner._draining = False, True
    assert kw.GatewayKanbanWatchersMixin._kanban_dispatch_held_for_restart(runner) is True
    runner._draining = False
    assert kw.GatewayKanbanWatchersMixin._kanban_dispatch_held_for_restart(runner) is False
