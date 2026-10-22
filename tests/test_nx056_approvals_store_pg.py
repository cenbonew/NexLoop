"""NX-056 A1 (docs/design/unified-approvals.md v5): the unified Approval store and its two governed human Actions, on a clean catalog.

Members are written as the identity store writes them; the workbench configuration and a policy as trusted configuration does (admin
seeds), and the governed handlers are called as the governed entry calls them (tenant, world, principal and subject kind server side).
Synthetic ids only.
"""
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A
from test_workbench_read_pg import add_human

CAP = 'real_dispatch.set'
ACTION = 'eios:action:nexloop.real_dispatch.set:1'


@pytest.fixture
def store(admin):
    bootstrap(admin)
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT_A,))
    people = {k: add_human(admin, k) for k in ('requester', 'approver', 'second', 'operator')}
    # The requester's second principal: the same person (subject) under another membership (L6 C5).
    alias = dict(people['requester'], principal_id='synthetic-staff-requester-alias')
    payload = admin.execute('select payload from control.nexloop_browser_memberships where principal_id=%s', (people['requester']['principal_id'],)).fetchone()[0]
    admin.execute('insert into control.nexloop_browser_memberships values(%s,%s,%s,%s)',
                  (TENANT_A, alias['principal_id'], alias['subject_id'], Jsonb({**payload, 'principal_id': alias['principal_id']})))
    people['alias'] = alias
    members = {p['principal_id']: role for p, role in ((people['requester'], 'owner'), (alias, 'owner'), (people['approver'], 'owner'),
                                                       (people['second'], 'owner'), (people['operator'], 'operator'))}
    admin.execute("""insert into control.nexloop_workbench_configurations(tenant_id,manifest_version,roles_version,application_id,roles,members,roles_digest,recorded_by)
        values(%s,1,1,'synthetic-workbench-app',%s,%s,%s,'synthetic-configurator')""",
                  (TENANT_A, Jsonb({'owner': [ACTION, 'eios:action:nexloop.approvals.decide:1'], 'operator': []}), Jsonb(members), 'c' * 64))
    policy(admin, 1)
    return {'admin': admin, **people}


def policy(admin, version, quorum=1, roles=('owner',), expires=86400):
    admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
        summary_fields,published_by) values(%s,%s,1,%s,%s,%s,%s,%s,'synthetic-config')""",
                  (CAP, version, Jsonb({'field': 'enabled', 'equals': True}), quorum, list(roles), expires, ['scope_kind', 'enabled']))


def open_request(f, request_id='r-1', **body):
    body = {'operation': 'set_real_dispatch', 'request_id': request_id, 'scope_kind': 'tenant', 'enabled': True, 'reason': '打开', **body}
    return f['admin'].execute('select control.nexloop_approval_open(%s,%s,%s,%s,%s,%s)',
                              (TENANT_A, 'real', CAP, ACTION, f['requester']['principal_id'], Jsonb(body))).fetchone()[0]


def decide(f, who, approval, decision='approve', subject='human'):
    principal = f[who]['principal_id'] if isinstance(who, str) else who
    return f['admin'].execute('select control.nexloop_governed_approval_decide(%s,%s,%s,%s,%s,%s)',
                              (TENANT_A, 'real', principal, subject, 'intent-' + uuid.uuid4().hex[:12],
                               Jsonb({'operation': 'decide_approval', 'request_id': uuid.uuid4().hex, 'approval_id': approval, 'decision': decision,
                                      'reason': '核对过'}))).fetchone()[0]


def cancel(f, who, approval):
    return f['admin'].execute('select control.nexloop_governed_approval_cancel(%s,%s,%s,%s,%s,%s)',
                              (TENANT_A, 'real', f[who]['principal_id'], 'human', 'intent-cancel',
                               Jsonb({'operation': 'cancel_approval', 'request_id': uuid.uuid4().hex, 'approval_id': approval, 'reason': '不需要了'}))).fetchone()[0]


def refused(code, fn, *args, **kw):
    with pytest.raises(psycopg.Error, match=code):
        fn(*args, **kw)


def row(f, approval):
    f['admin'].execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    return f['admin'].execute('select status,revision,due_by-requested_at from control.nexloop_approval_requests where approval_id=%s', (approval,)).fetchone()


def test_a_request_is_frozen_once_and_never_flooded(store):
    f = store
    opened = open_request(f)
    approval = opened['approval_id']
    assert opened['status'] == 'waiting_approval' and opened['replayed'] is False and row(f, approval)[2] == timedelta(hours=24)
    assert open_request(f) == {**opened, 'replayed': True}  # the same request id
    refused('request_reused', open_request, f, enabled=True, scope_kind='action_type')  # the same id, another body
    assert open_request(f, request_id='r-2')['approval_id'] == approval  # the same digest while one waits
    assert f['admin'].execute("select count(*) from runtime.audit_events where action='approval.requested'").fetchone() == (1,)


def test_the_requester_never_approves_by_any_principal(store):
    f = store
    approval = open_request(f)['approval_id']
    refused('self_approval', decide, f, 'requester', approval)
    refused('self_approval', decide, f, 'alias', approval)  # the same subject under another principal
    refused('approver_unauthorized', decide, f, 'operator', approval)  # not an approver role
    refused('approver_unauthorized', decide, f, 'approver', approval, subject='agent')  # never an agent
    assert decide(f, 'approver', approval)['status'] == 'approved' and row(f, approval)[0] == 'approved'


def test_a_quorum_counts_distinct_people_and_one_rejection_ends_it(store):
    f = store
    policy(f['admin'], 2, quorum=2)
    approval = open_request(f)['approval_id']
    assert decide(f, 'approver', approval)['status'] == 'waiting_approval'
    assert decide(f, 'approver', approval)['replayed'] is True  # the same decision by the same person replays, it does not count twice
    assert decide(f, 'second', approval) == {'approval_id': approval, 'status': 'approved', 'approvals': 2, 'quorum': 2, 'replayed': False}
    other = open_request(f, request_id='r-3', scope_kind='action_type')['approval_id']
    decide(f, 'approver', other)
    assert decide(f, 'second', other, 'reject')['status'] == 'rejected'
    refused('not_waiting', decide, f, 'approver', other, 'reject')


def test_expired_and_cancelled_are_terminal(store):
    f = store
    policy(f['admin'], 2, expires=900)
    approval = open_request(f)['approval_id']
    with f['admin'].transaction():
        f['admin'].execute('set local session_replication_role=replica')  # age this one request (its guard is off for this test only)
        f['admin'].execute("update control.nexloop_approval_requests set requested_at=requested_at-interval '2 hours',due_by=due_by-interval '1 hour' where approval_id=%s",
                           (approval,))
    assert decide(f, 'approver', approval)['status'] == 'expired' and row(f, approval)[0] == 'expired'
    other = open_request(f, request_id='r-4', scope_kind='action_type')['approval_id']
    refused('not_requester', cancel, f, 'approver', other)
    assert cancel(f, 'alias', other)['status'] == 'cancelled'  # the requester, under either principal
    refused('not_waiting', decide, f, 'approver', other)


def test_the_records_are_append_only_and_policies_only_human_capabilities(store):
    f = store
    admin = f['admin']
    approval = open_request(f)['approval_id']
    decide(f, 'approver', approval)
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    for sql in ('update control.nexloop_approval_decisions set reason=reason', 'delete from control.nexloop_approval_decisions',
                'truncate control.nexloop_approval_decisions', 'update control.nexloop_approval_policies set quorum=1',
                'truncate control.nexloop_approval_consumptions', 'delete from control.nexloop_approval_requests'):
        with pytest.raises(psycopg.Error), admin.transaction():
            admin.execute(sql)
    with pytest.raises(psycopg.Error, match='immutable'), admin.transaction():
        admin.execute('update control.nexloop_approval_requests set body=body||\'{"enabled":false}\',revision=revision+1 where approval_id=%s', (approval,))
    with pytest.raises(psycopg.Error), admin.transaction():  # no zero-person approval (L6 C4)
        admin.execute("insert into control.nexloop_approval_policies(capability,version,action_version,quorum,approver_roles,expires_after_seconds,published_by) "
                      "values(%s,9,1,0,'{owner}',86400,'x')", (CAP,))
    for capability in ('approvals.decide', 'no.such.capability'):
        with pytest.raises(psycopg.Error, match='human governed capability'), admin.transaction():
            admin.execute("insert into control.nexloop_approval_policies(capability,version,action_version,quorum,approver_roles,expires_after_seconds,published_by) "
                          "values(%s,1,1,1,'{owner}',86400,'x')", (capability,))
    for role in ('nexloop_api', 'nexloop_domain_worker'):
        assert not admin.execute("select has_table_privilege(%s,'control.nexloop_approval_requests','select')", (role,)).fetchone()[0]


def test_outreach_approval_refuses_the_requesters_other_principal(store):
    """0240 compared principals only (L6 C5): the requester's second principal is refused as self-approval too."""
    f = store
    admin = f['admin']
    intent = 'outreach:' + 'a' * 64
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into runtime.nexloop_outreach_sends(tenant_id,world,intent_id,consumer_id,purpose_id,purpose_version,topic,frequency_class,channel,
            origin_ref,body,body_digest,initiator,principal_id,request_intent,control_snapshot,state) values(%s,'real',%s,%s,'service_followup',1,'service.followup',
            'service','native.webchat','owner:x','跟进',%s,'human',%s,'intent-x','{}','pending_approval')""",
                      (TENANT_A, intent, 'b' * 64, 'e' * 64, f['requester']['principal_id']))
        admin.execute("""insert into runtime.nexloop_outreach_approvals(tenant_id,world,intent_id,requested_by,expires_at) values(%s,'real',%s,%s,%s)""",
                      (TENANT_A, intent, f['requester']['principal_id'], datetime.now(UTC) + timedelta(hours=24)))
    admin.execute("""insert into control.nexloop_workbench_configurations(tenant_id,manifest_version,roles_version,application_id,roles,members,roles_digest,recorded_by)
        select tenant_id,2,2,application_id,roles||jsonb_build_object('owner',roles->'owner'||'["eios:action:nexloop.outreach.approve:1"]'),members,roles_digest,recorded_by
        from control.nexloop_workbench_configurations where manifest_version=1""")
    with pytest.raises(psycopg.Error, match='self_approval'):
        admin.execute('select control.nexloop_governed_outreach_approve(%s,%s,%s,%s,%s,%s)',
                      (TENANT_A, 'real', f['alias']['principal_id'], 'human', 'intent-y',
                       Jsonb({'operation': 'decide_outreach_approval', 'request_id': 'q', 'intent_id': intent, 'decision': 'approve', 'reason': '可以'})))


def test_a_requester_holds_at_most_three_waiting_and_a_refused_digest_cools_down(store):
    f = store
    for n, kind in enumerate(('a', 'b', 'c')):
        open_request(f, request_id=f'w-{n}', reason=kind)
    refused('too_many_waiting', open_request, f, request_id='w-3', reason='d')
    rejected = open_request(f, request_id='w-0', reason='a')['approval_id']
    decide(f, 'approver', rejected, 'reject')
    refused('cooldown', open_request, f, request_id='w-4', reason='a')  # the same digest within an hour of its rejection
    assert open_request(f, request_id='w-5', reason='e')['status'] == 'waiting_approval'  # another body, a slot freed


def test_the_member_a_request_acts_on_never_approves_it(store):
    """NX-058 Z1: a policy names the body field of the member the request acts on; that person (by subject, under any principal)
    is refused at the decision, whatever the number of owners."""
    f = store
    admin = f['admin']
    admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
        summary_fields,target_subject_field,published_by) values(%s,2,1,null,1,'{owner}',86400,'{}','member','synthetic-config')""", (CAP,))
    by_principal = open_request(f, request_id='t-1', member=f['approver']['principal_id'])['approval_id']
    refused('target_cannot_approve', decide, f, 'approver', by_principal)
    assert decide(f, 'second', by_principal)['status'] == 'approved'
    by_subject = open_request(f, request_id='t-2', member=f['second']['subject_id'])['approval_id']  # a subject id names the same person
    refused('target_cannot_approve', decide, f, 'second', by_subject)
    assert decide(f, 'approver', by_subject)['status'] == 'approved'
    with pytest.raises(psycopg.Error), admin.transaction():
        admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,quorum,approver_roles,expires_after_seconds,
            target_subject_field,published_by) values(%s,3,1,1,'{owner}',86400,'Bad Field','x')""", (CAP,))
