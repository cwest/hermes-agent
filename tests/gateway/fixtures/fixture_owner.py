"""Stand-in for a test process that dies abnormally: start one fixture through the harness, print its
pid, then block on stdin until killed. The regression test SIGKILLs this process and watches the fixture.

    python fixture_owner.py <log path> <argv...>
"""
import subprocess
import sys

from tests.gateway.fixtures.fixture_lifetime import spawn

with open(sys.argv[1], 'w', encoding='utf-8') as log:
    child = spawn(sys.argv[2:], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
print(child.pid, flush=True)
sys.stdin.read()
