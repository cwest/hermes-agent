"""Run ``python -m <module> <args...>`` bound to the test process that started it.

    python bounded_module.py <module> <args...>

Same entry point as ``python -m``, plus the ``fixture_lifetime`` watchdog: the child ends when
its parent dies or ``UGW_FIXTURE_MAX_SECONDS`` passes.
"""
import runpy
import sys

from tests.gateway.fixtures.fixture_lifetime import bound_to_parent

bound_to_parent()
module = sys.argv[1]
sys.argv = [sys.argv[0], *sys.argv[2:]]
runpy.run_module(module, run_name='__main__', alter_sys=True)
