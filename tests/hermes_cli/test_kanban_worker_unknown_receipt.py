"""An ``unknown`` receipt whose attempt booked the owner-unavailable exit ends the submitter with it.

The owner settles a lost managed worker's admission ``unknown``. When that worker recorded
``KANBAN_OWNER_UNAVAILABLE_EXIT_CODE`` (its owner stopped answering), the submitter exits with
the same code, so its log trailer and the authority's ``worker_result`` give one verdict. Any
other unknown receipt still fails the submitter as before.
"""

import pytest

from gateway import session_kanban
from hermes_cli import kanban_worker_client
from hermes_cli.gateway_client import GatewayClientError
from hermes_cli.turn_exit import KANBAN_OWNER_UNAVAILABLE_EXIT_CODE

PARAMS = {'board': 'b', 'task_id': 't1', 'run_id': 1, 'claim_lock': 'lock'}


def test_unknown_receipt_after_an_owner_side_failure_exits_owner_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(session_kanban, 'worker_result', lambda db, params: {
        'exit_code': KANBAN_OWNER_UNAVAILABLE_EXIT_CODE, 'last_output': ''})
    code = kanban_worker_client._exit_code({'status': 'unknown'}, PARAMS, str(tmp_path / 'kanban.db'))
    assert code == KANBAN_OWNER_UNAVAILABLE_EXIT_CODE


@pytest.mark.parametrize('result', [{}, {'exit_code': 1, 'last_output': ''}])
def test_any_other_unknown_receipt_still_fails_the_submitter(monkeypatch, tmp_path, result):
    monkeypatch.setattr(session_kanban, 'worker_result', lambda db, params: result)
    with pytest.raises(GatewayClientError, match='execution unknown'):
        kanban_worker_client._exit_code({'status': 'unknown'}, PARAMS, str(tmp_path / 'kanban.db'))
