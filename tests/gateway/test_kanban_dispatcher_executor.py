"""The embedded dispatcher's thread offload is not queued behind the default executor.

``asyncio.to_thread`` runs on the loop's default executor. Anything else that
saturates that pool (a burst of plain ``to_thread`` callers, a library that
offloads to ``run_in_executor(None, ...)``) would otherwise park every dispatcher
tick behind it, so ready work waits for minutes instead of one interval. The
dispatcher, notifier and zombie reaper offload through their own pool.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading

from gateway.kanban_watchers_common import _to_thread_process_service


def test_process_service_offload_runs_while_the_default_executor_is_saturated():
    release = threading.Event()
    both_busy = threading.Barrier(2)

    def busy():
        both_busy.wait(timeout=10)
        release.wait(timeout=30)

    async def scenario() -> bool:
        loop = asyncio.get_running_loop()
        default_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        loop.set_default_executor(default_pool)
        busy_futs = [loop.run_in_executor(None, busy) for _ in range(2)]
        try:
            ran = await asyncio.wait_for(_to_thread_process_service(lambda: True), timeout=5)
        except TimeoutError:
            ran = False
        finally:
            release.set()
            await asyncio.gather(*busy_futs)
        return ran

    assert asyncio.run(scenario()) is True
