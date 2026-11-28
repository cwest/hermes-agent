"""A worker whose owner answers late resends the same journaled entry instead of failing the turn.

The owner settles a worker write inside a 60 s transcript write patience; the worker's receipt wait is
20 s. A receipt that missed that wait (the owner's loop stalled, or its write queued behind another
writer) used to make the store's failure sticky, so every later write of the turn failed at once and the
managed worker exited ``owner_unavailable`` mid-turn. The receipt is idempotent per (execution,
sequence, digest): resending the exact pending entry returns the stored receipt.
"""
import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError


class _SlowOwner:
    """Owner whose first ``late`` replies miss the worker's receipt wait, after the write landed."""

    def __init__(self, late, failure=TimeoutError('worker_receipt_timeout')):
        self.late, self.failure, self.sent, self.receipts = late, failure, [], {}

    def __call__(self, method, **params):
        self.sent.append((params['sequence'], params['operation']))
        receipt = self.receipts.setdefault(params['sequence'], {'count': 1, 'annotations': [{'_row_id': params['sequence']}]})
        if self.late:
            self.late -= 1
            raise self.failure
        return receipt


def test_a_receipt_timeout_resends_the_same_entry_and_the_turn_keeps_writing(tmp_path):
    owner = _SlowOwner(late=2)
    store = RuntimeSessionStore(owner, {'session_id': 's'}, tmp_path / 'private')
    try:
        rows = [{'role': 'tool', 'content': 'result'}]
        store.append_messages_batch('s', rows)
        assert rows[0]['_row_id'] == 1
        assert store.failure is None and store.journal['pending'] == []
        store.append_messages_batch('s', [{'role': 'assistant', 'content': 'next'}])
        assert [sequence for sequence, _ in owner.sent] == [1, 1, 1, 2]
    finally:
        store.close()


def test_a_dropped_owner_connection_resends_the_same_entry(tmp_path):
    from websockets.exceptions import ConnectionClosedError
    owner = _SlowOwner(late=1, failure=ConnectionClosedError(None, None))
    store = RuntimeSessionStore(owner, {'session_id': 's'}, tmp_path / 'private')
    try:
        store.queue_token_counts('s', input_tokens=1)
        assert store.failure is None
        assert [sequence for sequence, _ in owner.sent] == [1, 1]
    finally:
        store.close()


def test_an_owner_refusal_is_not_resent(tmp_path):
    owner = _SlowOwner(late=1, failure=WorkerPersistenceError('stale_generation'))
    store = RuntimeSessionStore(owner, {'session_id': 's'}, tmp_path / 'private')
    try:
        with pytest.raises(WorkerPersistenceError, match='stale_generation'):
            store.queue_token_counts('s', input_tokens=1)
        assert store.failure == 'stale_generation'
        assert len(owner.sent) == 1
    finally:
        store._outbox_owner.close()


def test_an_owner_silent_past_the_resend_budget_still_fails_sticky(tmp_path):
    owner = _SlowOwner(late=10 ** 6)
    store = RuntimeSessionStore(owner, {'session_id': 's'}, tmp_path / 'private', resend_budget_s=0.0)
    try:
        with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
            store.queue_token_counts('s', input_tokens=1)
        assert store.failure == 'worker_receipt_timeout'
        assert len(owner.sent) == 1
        assert store.journal['pending'][0]['sequence'] == 1
    finally:
        store._outbox_owner.close()
