"""The owner's worker claim check runs off the gateway event loop.

``worker_request`` serves every managed worker of every profile the gateway owns. Its claim check reads
the profile's state.db (the producer's original identity is a ``json_extract`` scan over every receipt
of the execution: 100+ ms on a multi-GB store) and probes the worker process. Run on the loop, that
cost is paid by every other connection: replies to concurrent workers miss their receipt wait. Real
SQLite, real processes.
"""
import asyncio
import subprocess
import sys
import threading
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli.process_identity import worker_birth
from tests.fixtures.storage_seed import seed_session
from tests.gateway.fixtures.storage_handles import storage_handle


@pytest.mark.asyncio
async def test_claim_and_verify_do_not_block_the_event_loop(tmp_path, monkeypatch):
    from gateway import session_worker
    from gateway.config import Platform
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionActor, SessionRef

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = storage_handle(tmp_path)
    seed_session(db, 'owned', 'telegram')
    runner = SimpleNamespace(_draining=False, config=SimpleNamespace(multiplex_profiles=False),
                             _adapter_for_source=lambda source: None)
    authority = SessionAuthority(runner, profile_id='p', instance_id='owner',
                                 epoch=db.runtime.begin_epoch(instance_id='owner'), persistence=db)
    authority.sessions['owned'] = LiveSession(SimpleNamespace(platform=Platform.TELEGRAM, user_id='human'), 'route')
    ref = SessionRef('p', 'owned')
    worker = SimpleNamespace(authority=authority, actor=SessionActor('human', 'p', frozenset({'worker:adopt'}), 't'))
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE,  # noqa: ASYNC220 -- Popen returns immediately
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    scope = {'profile_id': 'p', 'session_id': 'owned', 'execution_id': f'compute-{child.pid}', 'generation': 0,
             'pid': child.pid, 'birth': worker_birth(psutil.Process(child.pid)), 'secret': 'w' * 32}
    loop = asyncio.get_running_loop()
    blocked = []

    def needs_the_loop(real):
        # The store read stands in for a slow one: it finishes only once the loop has run another task.
        # On the loop thread that task can never run, so the wait would time out.
        def check(*args, **kwargs):
            done = threading.Event()
            loop.call_soon_threadsafe(done.set)
            if not done.wait(5):
                blocked.append(real.__name__)
            return real(*args, **kwargs)
        return check
    monkeypatch.setattr(session_worker, '_claim', needs_the_loop(session_worker._claim))
    monkeypatch.setattr(session_worker, '_verify', needs_the_loop(session_worker._verify))
    try:
        registered = await session_worker.worker_request(worker, ref, dict(scope, kind='compute'), operation='register')
        assert registered['status'] == 'registered'
        adopted = await session_worker.worker_request(worker, ref, scope, operation='adopt')
        assert adopted['status'] == 'running'
        assert blocked == []
    finally:
        child.stdin.close()
        child.wait(timeout=5)
        db.close()


class _Stop(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['delegation.complete', 'child.reserve'])
@pytest.mark.parametrize('check', ['_claim', '_verify'])
async def test_sibling_persist_targets_check_claims_off_the_event_loop(monkeypatch, operation, check):
    """``delegation.*`` and ``child.reserve`` share the same claim check, so they must offload it too."""
    from gateway import session_worker
    from gateway.session_contract import SessionActor, SessionRef

    loop = asyncio.get_running_loop()
    ran_on_loop = []

    def probe(*_args, **_kwargs):
        done = threading.Event()
        loop.call_soon_threadsafe(done.set)
        ran_on_loop.append(not done.wait(5))
        raise _Stop

    monkeypatch.setattr(session_worker, '_claim', lambda *_a, **_k: object())
    monkeypatch.setattr(session_worker, '_verify', lambda *_a, **_k: None)
    monkeypatch.setattr(session_worker, check, probe)
    worker = SimpleNamespace(authority=SimpleNamespace(),
                             actor=SessionActor('human', 'p', frozenset({'worker:adopt'}), 't'))
    params = {key: 'x' for key in session_worker._SCOPE} | {
        'epoch': 1, 'sequence': 1, 'operation': operation, 'payload': {}}
    from gateway.session_worker_child import worker_child_request
    from gateway.session_worker_delegation import worker_delegation_request
    handler = worker_child_request if operation == 'child.reserve' else worker_delegation_request
    with pytest.raises(_Stop):
        await handler(worker, SessionRef('p', 'owned'), params)
    assert ran_on_loop == [False]
