"""The native-live gateway fixtures never outlive the test process that started them.

A ``stack_dump_daemon.py`` whose test process was killed without cleanup re-parented to init and
kept rewriting ``logs/stacks.txt`` for days. These tests drive the real fixtures through the real
harness: kill the starting process with SIGKILL and the daemon must exit by itself, and a daemon
nobody stops must still exit once its lifetime ceiling passes.
"""
import subprocess
import sys

import psutil
import pytest

from tests.gateway.test_unified_gateway_native_live import FIXTURES, ROOT, control, control_absent, daemon
from tests.waiting import wait_until

pytest_plugins = ('tests.gateway.test_unified_gateway_native_live',)  # the harness and peer fixtures

DAEMONS = ['stack_dump_daemon.py', 'api_recovery_daemon.py']


def _running(proc):
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


# The bypass covers only the failure-path cleanup: an orphan re-parented to init is outside this
# test's process subtree by definition, and the guard would refuse to kill it.
@pytest.mark.live_system_guard_bypass
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('fixture', DAEMONS)
def test_fixture_daemon_dies_with_its_killed_test_process(harness, fixture):
    home, env = harness['home'], harness['env']
    command = [sys.executable, str(FIXTURES / 'fixture_owner.py'), str(home.parent / 'orphan.log'),
               sys.executable, str(FIXTURES / fixture)]
    test_process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert test_process.stdout is not None
    orphan = None
    try:
        orphan = psutil.Process(int(test_process.stdout.readline()))
        # Ready: past start-up, about to serve indefinitely.
        wait_until(lambda: not control_absent(home) and control(home, 'identify').get('state') == 'ready',
                   timeout=120, what=f'{fixture} ready')
        test_process.kill()
        test_process.wait(timeout=30)
        wait_until(lambda: not _running(orphan), timeout=30, what=f'{fixture} exit after its test process died')
    finally:
        test_process.kill()
        test_process.wait(timeout=30)
        if orphan is not None and _running(orphan):
            orphan.kill()


@pytest.mark.parametrize('fixture', DAEMONS)
def test_fixture_daemon_exits_at_its_lifetime_ceiling(harness, fixture):
    home, env = harness['home'], harness['env']
    with daemon(home, {**env, 'UGW_FIXTURE_MAX_SECONDS': '5'}, 'ceiling.log', fixture=fixture,
                expect_ready=False) as (proc, _, log):
        assert proc.wait(timeout=90) != 0
        log.flush(); log.seek(0)
        assert 'UGW_FIXTURE_MAX_SECONDS' in log.read()
