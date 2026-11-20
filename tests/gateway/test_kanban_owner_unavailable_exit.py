"""A managed Kanban turn that failed because its OWNER stopped answering books the
owner-unavailable exit, not a plain failure.

A ``worker.persist`` that times out waiting for the owner's receipt says nothing about the
card: the gateway loop was stalled. Booked as exit 1, three such runs in one sweep trip the
systemic-crash rule and park every card on the board at once.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.turn_exit import KANBAN_OWNER_UNAVAILABLE_EXIT_CODE


def _bound_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    path = kb.kanban_db_path()
    with connect_closing(path) as conn:
        task_id = kb.create_task(conn, title="private fixture", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="private-host:owner")
        context = {"db": str(path), "task_id": task_id,
                   "run_id": task.current_run_id, "claim_lock": task.claim_lock}
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "worker_bound",
                             {"pid": os.getpid(), "claim_lock": task.claim_lock},
                             run_id=task.current_run_id)
    return path, context


def _store(tmp_path, error):
    """A real worker store whose owner RPC fails with ``error`` on the next write."""
    def rpc(method, **_params):
        raise error

    store = RuntimeSessionStore(rpc, {"session_id": "s1", "execution_id": "e1", "epoch": 1, "generation": 1},
                                tmp_path / "outbox")
    with pytest.raises(type(error)):
        store.try_acquire_session_turn_lease("s1", "holder")
    return store


def _run(path, context, monkeypatch, store, turn):
    monkeypatch.setattr(session_kanban, "_run_task_turns", turn)
    frame = {"policy": {"kanban_json": json.dumps(context)}}
    agent = SimpleNamespace(_session_db=store)
    try:
        session_kanban.run_worker_turns(agent, frame, [])
    except WorkerPersistenceError:
        pass
    return session_kanban.worker_exit_code(path, context)


def _raise_sticky(*_args):
    raise WorkerPersistenceError("worker_receipt_timeout")


def _persistence_failed_result(*_args):
    return {"failed": True, "failure_reason": "session_persistence_failed:unknown", "final_response": ""}


@pytest.mark.parametrize("turn", [_raise_sticky, _persistence_failed_result], ids=["raised", "failed-result"])
def test_owner_receipt_timeout_books_the_owner_unavailable_exit(tmp_path, monkeypatch, turn):
    path, context = _bound_claim(tmp_path, monkeypatch)
    store = _store(tmp_path, TimeoutError("worker_receipt_timeout"))
    assert _run(path, context, monkeypatch, store, turn) == KANBAN_OWNER_UNAVAILABLE_EXIT_CODE


def test_a_storage_failure_of_the_worker_itself_still_books_exit_1(tmp_path, monkeypatch):
    path, context = _bound_claim(tmp_path, monkeypatch)
    store = _store(tmp_path, WorkerPersistenceError("permission_denied"))
    assert _run(path, context, monkeypatch, store, _persistence_failed_result) == 1


def test_a_failed_turn_with_a_healthy_owner_still_books_exit_1(tmp_path, monkeypatch):
    path, context = _bound_claim(tmp_path, monkeypatch)
    store = RuntimeSessionStore(lambda method, **_p: {}, {"session_id": "s1"}, tmp_path / "outbox")
    assert _run(path, context, monkeypatch, store, lambda *_a: {"failed": True}) == 1
