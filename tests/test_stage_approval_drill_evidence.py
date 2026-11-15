"""NX-056 drill evidence consistency, synthetic memory objects only.
No login, credentials, HTTP transport, database, or stage operations.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('approval_drill_evidence', Path(__file__).resolve().parents[1] / 'scripts/stage/approval_drill.py')
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)
APPROVAL = '0f0e0d0c-0b0a-4908-8706-050403020100'


class MemoryWorkbench:
    def __init__(self, status):
        self.inputs = {'reason': 'synthetic-frozen-reason', 'value': 7}
        self.row = {'approval_id': APPROVAL, 'operation': 'synthetic_operation', 'inputs': self.inputs}
        if status is not None:
            self.row['status'] = status
        self.sent = []

    def approvals(self):
        return {'to_execute': [self.row]}

    def act(self, operation, inputs):
        self.sent.append((operation, inputs))
        return 200, {'approval': 'consumed'}


@pytest.mark.parametrize('status', ['expired', 'rejected', 'cancelled', 'waiting_approval', None])
def test_contradictory_to_execute_row_never_sends_an_execution(status):
    wb = MemoryWorkbench(status)
    record, ok = D.run_step(SimpleNamespace(step='execute', approval_id=APPROVAL), wb)
    assert ok is False
    assert record == {'http': None, 'approval_id': APPROVAL, 'error': 'not_executable_from_workbench'}
    assert wb.sent == []


def test_approved_row_still_submits_the_same_frozen_inputs_once():
    wb = MemoryWorkbench('approved')
    record, ok = D.run_step(SimpleNamespace(step='execute', approval_id=APPROVAL), wb)
    assert ok is True and record['approval'] == 'consumed' and record['approval_id'] == APPROVAL
    assert wb.sent == [('synthetic_operation', wb.inputs)]
    assert wb.sent[0][1] is wb.inputs
