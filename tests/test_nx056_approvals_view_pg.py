"""NX-056 A4 (docs/design/unified-approvals.md v5 §8; 0329): the workbench approvals view over HTTP, end to end.

Production create_app, members applied by the real configurator (test_nx056_approval_gate_pg's `gated`: owner, operator, a second
owner as approver; policy "opening real dispatch needs one other owner"). The view's three columns per caller, never another person's
body, and the execute round trip: the inputs the view returns, resubmitted as they are, carry the same digest and consume the approval.
Synthetic data only.
"""
import pytest
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from nexloop_eios.http_api import create_app
from test_browser_http import ORIGIN
from test_nx056_approval_gate_pg import PLAN, gated, status, tenant_switch  # noqa: F401
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_real_dispatch_switch_pg import switching  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login

COLUMNS = {'to_decide', 'mine', 'to_execute', 'solo_mode'}
ROW = {'approval_id', 'capability', 'operation', 'action', 'requested_by', 'requested_at', 'due_by', 'status', 'digest', 'policy_version', 'quorum',
       'approvals', 'summary', 'target_member'}


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
    assert mine['solo_mode'] == {'enabled': False, 'others_eligible': True}  # the banner's facts (A7)
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


@pytest.fixture
def first_batch(gated, admin, tmp_path, pg):
    """The policies of approval-policies.v1.json (the gate fixture's own real_dispatch.set policy is version 1 already), the two
    other Actions published and a Consumer hold to release."""
    from nexloop_eios.approval_policies import load_policies
    from psycopg.types.json import Jsonb
    from commercial_fixture import publish_registry_actions
    from test_nx056_first_policies_pg import POLICIES
    c = gated
    publish_registry_actions(admin, ('nexloop.consumer.erase', 'nexloop.retention.hold', 'nexloop.retention.release_hold'))
    for p in load_policies(POLICIES)['policies']:
        if p['capability'] != 'real_dispatch.set':
            admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
                summary_fields,published_by) values(%s,%s,%s,null,%s,%s,%s,%s,'synthetic-config')""",
                          (p['capability'], p['version'], p['action_version'], p['quorum'], p['approver_roles'], p['expires_after_seconds'], p['summary_fields']))
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    admin.execute("""insert into control.nexloop_retention_holds(tenant_id,world,hold_id,scope_kind,scope_ref,basis,held_by)
        values(%s,'real','hold-roundtrip','item_class','message_text','synthetic basis','synthetic')""", (TENANT,))
    return c


def round_trip(c, operation, body):
    """Request (waits), approval by the second owner, then the requester executes the view's inputs as they are."""
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        first = act(client, csrf, operation, body)
    assert first.status_code == 200 and first.json()['status'] == 'waiting_approval', first.text
    approval = first.json()['approval_id']
    assert c['decide'](approval).json()['status'] == 'approved'
    [ready] = [r for r in view(c, c['owner_member'])['to_execute'] if r['approval_id'] == approval]
    assert ready['operation'] == operation and set(ready['inputs']) == set(body)  # every workbench field, absent ones as null
    with TestClient(create_app(c['config']), base_url=ORIGIN) as client:
        csrf = workbench_login(client, c['owner_member']).json()['csrf_token']
        executed = act(client, csrf, operation, ready['inputs'])
    assert executed.status_code == 200 and executed.json()['approval'] == 'consumed' and executed.json()['approval_id'] == approval, executed.text
    return executed.json()


@PLAN
def test_each_first_batch_capability_executes_from_the_views_inputs(first_batch, admin):
    c = first_batch
    # real_dispatch.set: action_type absent from the frozen body, null in the inputs, left out again on resubmission (same digest).
    round_trip(c, 'set_real_dispatch', {'scope_kind': 'tenant', 'enabled': True, 'data_mode': 'real', 'action_type': None, 'reason': 'synthetic opening'})
    assert tenant_switch(admin) is True
    erased = round_trip(c, 'erase_consumer', {'consumer_id': c['consumer'], 'reason': 'synthetic erasure'})
    assert erased['state'] == 'settling'
    assert admin.execute("select count(*) from control.nexloop_approval_consumptions where result='executed'").fetchone() == (2,)
    # A hold release is not a workbench operation: its request (from the governed API client) is listed, never offered here.
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    opened = admin.execute("select control.nexloop_approval_open(%s,'real','retention.release_hold','eios:action:nexloop.retention.release_hold:1',%s,%s)", (
        TENANT, c['owner_member']['principal_id'], Jsonb({'operation': 'release_retention_hold', 'request_id': 'api-1', 'hold_id': 'hold-roundtrip',
                                                          'reason': 'synthetic release'}))).fetchone()[0]
    c['decide'](opened['approval_id'])
    [ready] = [r for r in view(c, c['owner_member'])['to_execute'] if r['approval_id'] == opened['approval_id']]
    assert ready['inputs'] is None and ready['summary'] == {}  # a hold id is never shown (it may carry a Consumer id)


@PLAN
def test_a_frozen_body_with_keys_the_workbench_never_sends_is_not_offered_for_execution(first_batch, admin):
    c = first_batch
    # Another client's body: erase_consumer with confirm_request (the handler accepts the key; the workbench never sends it).
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    opened = admin.execute("select control.nexloop_approval_open(%s,'real','consumer.erase','eios:action:nexloop.consumer.erase:1',%s,%s)", (
        TENANT, c['owner_member']['principal_id'], Jsonb({'operation': 'erase_consumer', 'request_id': 'other-client-1', 'consumer_id': c['consumer'],
                                                          'reason': 'x', 'confirm_request': True}))).fetchone()[0]
    c['decide'](opened['approval_id'])
    [ready] = [r for r in view(c, c['owner_member'])['to_execute'] if r['approval_id'] == opened['approval_id']]
    assert ready['inputs'] is None  # no execution offered (never a 422 loop); the approval stays approved until its due_by
