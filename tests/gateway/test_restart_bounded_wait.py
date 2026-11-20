"""A requested restart is bounded: on a board that never goes idle it is forced after
``agent.restart_max_wait`` (``0`` keeps the unbounded wait), and every unit cut is logged."""

import asyncio
import logging

from gateway.restart_when_idle import await_idle
from gateway.run import GatewayRunner
from tests.gateway.restart_test_helpers import make_restart_runner


def _forever_busy_runner(max_wait: float):
    runner, _adapter = make_restart_runner()
    runner._restart_after_turn_timeout = 0.0
    runner._restart_max_wait = max_wait
    runner._awaitable_work_count = lambda: 1
    runner._describe_active_work = lambda: [{"kind": "process", "process_id": "proc_poll", "pid": 4242}]
    runner._scale_to_zero_status = lambda *_a, **_k: None
    return runner


def test_a_restart_on_a_board_that_never_idles_is_forced_after_max_wait(caplog):
    runner = _forever_busy_runner(max_wait=0.3)
    caplog.set_level(logging.WARNING, logger="gateway.run")

    idle = asyncio.run(asyncio.wait_for(await_idle(runner), timeout=5))

    assert idle is False  # forced, not idle: the caller runs force_stop_budgets
    assert runner._restart_force_event.is_set()
    forced = [r.getMessage() for r in caplog.records if "restart_max_wait" in r.getMessage()]
    assert forced and "process(process_id=proc_poll, pid=4242)" in forced[0]


def test_restart_max_wait_zero_keeps_the_unbounded_wait():
    runner = _forever_busy_runner(max_wait=0.0)

    async def _run():
        task = asyncio.create_task(await_idle(runner))
        done, _ = await asyncio.wait({task}, timeout=0.5)
        assert not done  # still waiting: no bound
        runner._restart_force_event.set()
        return await asyncio.wait_for(task, timeout=2)

    assert asyncio.run(_run()) is False


def test_restart_max_wait_loads_from_config_and_defaults_to_600(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text("agent:\n  restart_max_wait: 45\n", encoding="utf-8")
    assert GatewayRunner._load_restart_max_wait() == 45.0
    config.write_text("agent:\n  restart_max_wait: 0\n", encoding="utf-8")
    assert GatewayRunner._load_restart_max_wait() == 0.0
    config.write_text("agent: {}\n", encoding="utf-8")
    assert GatewayRunner._load_restart_max_wait() == 600.0
