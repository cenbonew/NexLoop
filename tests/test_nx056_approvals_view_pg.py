"""NX-056 A4 (docs/design/unified-approvals.md v5 §8; 0324): the workbench approvals view over HTTP, end to end.

Production create_app, members applied by the real configurator (test_nx056_approval_gate_pg's `gated`: owner, operator, a second
owner as approver; policy "opening real dispatch needs one other owner"). The view's three columns per caller, never another person's
body, and the execute round trip: the inputs the view returns, resubmitted as they are, carry the same digest and consume the approval.
Synthetic data only.
"""
import pytest
from fastapi.testclient import TestClient

from nexloop_eios.http_api import create_app
from test_browser_http import ORIGIN
from test_nx056_approval_gate_pg import PLAN, gated, status, tenant_switch  # noqa: F401
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_real_dispatch_switch_pg import switching  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login

COLUMNS = {'to_decide', 'mine', 'to_execute'}
ROW = {'approval_id', 'capability', 'operation', 'action', 'requested_by', 'requested_at', 'due_by', 'status', 'digest', 'policy_version', 'quorum',
       'approvals', 'summary'}


def view(c, member):
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        workbench_login(client, member)
        response = client.get('/api/v1/workbench/approvals', headers={'Origin': ORIGIN})
        assert response.status_code == 200, response.text
        return response.json()


@PLAN
def test_each_member_sees_their_own_columns_and_never_anothers_body(gated, admin):
    c = gated
    approval = c['request']().json()['approval_id']
    mine = view(c, c['owner_member'])
    assert set(mine) == COLUMNS and mine['to_decide'] == [] and mine['to_execute'] == []
    row = mine['mine'][0]
    assert set(row) == ROW | {'result'} and row['approval_id'] == approval and row['status'] == 'waiting_approval' and row['result'] is None
    assert row['summary'] == {'scope_kind': 'tenant', 'enabled': True} and len(row['digest']) == 12 and (row['approvals'], row['quorum']) == (0, 1)
    theirs = view(c, c['second'])
    assert [r['approval_id'] for r in theirs['to_decide']] == [approval] and set(theirs['to_decide'][0]) == ROW  # no inputs, no body
    assert 'synthetic opening' not in str(theirs)  # the request's free text (reason) is not a summary field
    assert view(c, c['operator_member'])['to_decide'] == []  # not an approver role
    c['decide'](approval)
    assert view(c, c['second'])['to_decide'] == []  # decided


@PLAN
def test_the_inputs_shown_to_the_requester_execute_the_approved_request(gated, admin):
    c = gated
    approval = c['request']().json()['approval_id']
    c['decide'](approval)
    ready = view(c, c['owner_member'])['to_execute']
    assert [r['approval_id'] for r in ready] == [approval] and ready[0]['status'] == 'approved'
    inputs, operation = ready[0]['inputs'], ready[0]['operation']
    assert 'request_id' not in inputs and 'operation' not in inputs and operation == 'set_real_dispatch'
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        executed = act(client, csrf, operation, inputs)
    assert executed.status_code == 200, (inputs, executed.text)
    assert executed.json()['approval'] == 'consumed' and tenant_switch(admin) is True
    after = view(c, c['owner_member'])
    assert after['to_execute'] == [] and after['mine'][0]['result'] == 'executed'


@PLAN
def test_the_view_needs_a_session(gated):
    c = gated
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        assert client.get('/api/v1/workbench/approvals', headers={'Origin': ORIGIN}).status_code in (401, 403)
        workbench_login(client, c['owner_member'])
        assert client.get('/api/v1/workbench/approvals?x=1', headers={'Origin': ORIGIN}).status_code == 422
