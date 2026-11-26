"""Keep the gateway daemons the native-live tests start from outliving the test process.

A test process that dies without running its ``finally`` (a file timeout, a SIGKILLed worker, a
reaped kanban worker) re-parents every child it started to init, where a gateway daemon serves
forever. Two layers stop that:

* test side (``spawn`` / ``reap``): each child leads its own session and process group and is
  recorded in a module registry; every registered group is SIGKILLed at interpreter exit or on
  SIGTERM, so grandchildren left in the group go with it;
* child side (``bound_to_parent``, called first thing by every fixture daemon): a watchdog thread
  ends the daemon and its group once its parent pid changes, the one parent-death signal macOS
  offers (no ``PR_SET_PDEATHSIG``), and in any case after ``UGW_FIXTURE_MAX_SECONDS`` (default
  900), so even a kill nobody delivered heals itself.
"""
import atexit
import os
import signal
import subprocess
import sys
import threading
import time

CEILING_ENV = 'UGW_FIXTURE_MAX_SECONDS'
DEFAULT_CEILING_SECONDS = 900.0
PARENT_POLL_SECONDS = 0.5

_live: dict[int, subprocess.Popen] = {}
_registry_lock = threading.Lock()
_hooks_installed = False
_armed = False


def spawn(argv, **popen_kwargs):
    """``Popen`` a test child that is reaped with this process (see the module docstring)."""
    if os.name == 'posix':
        popen_kwargs['start_new_session'] = True
    proc = subprocess.Popen(argv, **popen_kwargs)
    with _registry_lock:
        _install_hooks()
        _live[proc.pid] = proc
    return proc


def reap(proc, timeout=20):
    """Kill ``proc`` and, on POSIX, every process left in the group it leads; then forget it.

    The group is signalled only while ``proc`` is unreaped: until then its pid (and so the pgid)
    cannot be recycled, so the signal never reaches a stranger's group."""
    if os.name == 'posix' and proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX branch; spawn made pid the pgid
        except ProcessLookupError:
            pass
    elif proc.poll() is None:
        proc.kill()
    proc.wait(timeout=timeout)
    with _registry_lock:
        _live.pop(proc.pid, None)


def reap_all():
    with _registry_lock:
        procs = list(_live.values())
    for proc in procs:
        try:
            reap(proc, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _install_hooks():
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    atexit.register(reap_all)
    if threading.current_thread() is not threading.main_thread():
        return
    previous = signal.getsignal(signal.SIGTERM)

    def on_sigterm(signum, frame):
        reap_all()
        if callable(previous):
            previous(signum, frame)
            return
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, on_sigterm)


def _exit_with_group(reason):
    sys.stderr.write(f'fixture daemon {os.getpid()} exiting: {reason}\n')
    sys.stderr.flush()
    if os.name == 'posix' and os.getpgrp() == os.getpid():
        os.killpg(os.getpid(), signal.SIGKILL)  # windows-footgun: ok — POSIX only; never a group we do not lead
    os._exit(124)


def bound_to_parent():
    """End this process (and the group it leads) when its parent dies or its lifetime ceiling
    passes. Idempotent. The parent check is POSIX only: a Windows child's recorded parent pid
    never changes when the parent dies, so there only the ceiling applies."""
    global _armed
    if _armed:
        return
    _armed = True
    parent = os.getppid()
    ceiling = float(os.environ.get(CEILING_ENV) or DEFAULT_CEILING_SECONDS)
    deadline = time.monotonic() + ceiling

    def watch():
        tick = threading.Event()
        while True:
            if os.name == 'posix' and (parent == 1 or os.getppid() != parent):
                _exit_with_group(f'parent {parent} is gone')
            if time.monotonic() >= deadline:
                _exit_with_group(f'{CEILING_ENV}={ceiling:g} lifetime ceiling reached')
            tick.wait(PARENT_POLL_SECONDS)

    threading.Thread(target=watch, name='fixture-lifetime', daemon=True).start()
