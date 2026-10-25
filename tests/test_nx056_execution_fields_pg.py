"""NX-056 B2 (ADR-039 draft revisions R2 §7 and R3 §8): the capability approval settings (execution-time fields, accepted target
statuses), open-time quorum_unreachable, requester_no_longer_eligible and the execution refusal codes, on a clean catalog (the store
fixture of test_nx056_approvals_store_pg: four owners and an operator, votes ripened past the NX-058 slice D cooldown). The handlers and
the gate are called as the governed entry calls them. Synthetic ids only.
"""
import json
from datetime import datetime

import psycopg
import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_nx056_approvals_store_pg import ACTION, CAP, decide, refused, row, store  # noqa: F401
from test_workbench_read_pg import add_human

FIELDS = ['code_hmac', 'issuer_source_hmac']


def register_fields(admin, capability=CAP, fields=FIELDS):
    admin.execute('insert into control.nexloop_approval_capability_settings(capability,execution_fields) values(%s,%s)', (capability, fields))


def body(request_id, **extra):
    return {'operation': 'set_real_dispatch', 'request_id': request_id, 'scope_kind': 'tenant', 'enabled': True, 'reason': '打开', **extra}


def open_as(f, who, b):
    return f['admin'].execute('select control.nexloop_approval_open(%s,%s,%s,%s,%s,%s)',
                              (TENANT_A, 'real', CAP, ACTION, f[who]['principal_id'], Jsonb(b))).fetchone()[0]


def gate(f, who, b):
    return f['admin'].execute('select control.nexloop_approval_gate(%s,%s,%s,%s,%s,%s)',
                              (TENANT_A, 'real', CAP, ACTION, f[who]['principal_id'], Jsonb(b))).fetchone()[0]


def frozen(f, approval):
    f['admin'].execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    return f['admin'].execute('select body from control.nexloop_approval_requests where approval_id=%s', (approval,)).fetchone()[0]


def target_policy(admin, version, quorum=1):
    admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
        summary_fields,target_subject_field,solo_mode,published_by) values(%s,%s,1,null,%s,'{owner}',86400,'{}','target_principal','not_applicable','x')""",
                  (CAP, version, quorum))


def test_execution_time_fields_are_neither_frozen_nor_in_the_digest(store):
    f = store
    register_fields(f['admin'])
    approval = open_as(f, 'requester', body('r-1', code_hmac='a' * 64, issuer_source_hmac='b' * 64))['approval_id']
    assert set(frozen(f, approval)) == {'operation', 'request_id', 'scope_kind', 'enabled', 'reason'}  # no secret's HMAC kept
    assert 'a' * 64 not in json.dumps(frozen(f, approval))
    # The waiting submission may carry them or not: the same request either way.
    assert open_as(f, 'requester', body('r-2'))['approval_id'] == approval
    decide(f, 'approver', approval)
    # The execution's resubmission draws new values (another code, another network): the approval still matches and is used.
    executed = gate(f, 'requester', body('r-3', code_hmac='c' * 64, issuer_source_hmac='d' * 64))
    assert executed['proceed'] is True and executed['approval_id'] == approval
    # R3 §8.2: a present execution-time field is a 64-hex HMAC, nothing else.
    refused('execution_field_invalid', open_as, f, 'requester', body('r-4', reason='x', code_hmac='the code itself'))


def test_settings_are_append_only_hmac_only_registered_first_and_never_a_condition(store):
    f = store
    admin = f['admin']
    register_fields(admin)
    for sql in ('update control.nexloop_approval_capability_settings set execution_fields=execution_fields',
                'delete from control.nexloop_approval_capability_settings'):
        with pytest.raises(psycopg.Error), admin.transaction():
            admin.execute(sql)
    with pytest.raises(psycopg.Error, match='execution-time field'), admin.transaction():
        admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
            summary_fields,published_by) values(%s,9,1,%s,1,'{owner}',86400,'{}','x')""", (CAP, Jsonb({'field': 'code_hmac', 'equals': 'x'})))
    for fields in (['request_id'], ['issuer_source'], ['code_hmac', 'code_hmac']):  # R3 §8.2: *_hmac only, never repeated
        with pytest.raises(psycopg.Error, match='invalid'), admin.transaction():
            register_fields(admin, 'approvals.decide', fields)


def test_settings_come_before_any_request_of_the_capability(store):
    """R3 §8.3: a request frozen and digested without the settings would not match them."""
    f = store
    open_as(f, 'requester', body('s-1'))
    with pytest.raises(psycopg.Error, match='already has approval requests'), f['admin'].transaction():
        register_fields(f['admin'])


def test_a_target_has_one_of_the_capabilitys_declared_statuses(store):
    """R3 §8.1: no fixed status list - a capability without settings accepts an active target only."""
    f = store
    admin = f['admin']
    target_policy(admin, 2)
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    admin.execute("""update control.nexloop_browser_memberships set payload=payload||'{"status":"suspended"}' where principal_id=%s""",
                  (f['operator']['principal_id'],))
    refused('target_unresolved', open_as, f, 'requester', body('t-1', target_principal=f['operator']['principal_id']))
    assert open_as(f, 'requester', body('t-2', target_principal=f['approver']['principal_id']))['status'] == 'waiting_approval'
    statuses = dict(admin.execute('select capability,target_statuses from control.nexloop_approval_capability_settings').fetchall())
    assert statuses.get('workbench_member.resume_owner') == ['suspended'] and statuses.get('workbench_member.revoke_owner') == ['active', 'suspended']


def test_quorum_unreachable_at_open_with_eligible_from_when_only_cooldowns_fall_short(store):
    f = store
    admin = f['admin']
    target_policy(admin, 2, quorum=1)
    # Others than the requester and the target: second - enough for quorum 1.
    assert open_as(f, 'requester', body('q-1', target_principal=f['approver']['principal_id']))['status'] == 'waiting_approval'
    target_policy(admin, 3, quorum=2)
    refused('quorum_unreachable', open_as, f, 'requester', body('q-2', target_principal=f['approver']['principal_id']))  # only second
    # A new owner whose holding began now: a candidate, not yet eligible - the refusal names the quorum-th earliest time.
    newbie = add_human(admin, 'newbie')
    admin.execute("""insert into control.nexloop_workbench_configurations(tenant_id,manifest_version,roles_version,application_id,roles,members,roles_digest,recorded_by)
        select tenant_id,2,2,application_id,roles,members||jsonb_build_object(%s::text,'owner'),roles_digest,recorded_by
        from control.nexloop_workbench_configurations where tenant_id=%s and manifest_version=1""", (newbie['principal_id'], TENANT_A))
    admin.execute("""insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source,recorded_at)
        values(%s,%s,%s,'owner','start','synthetic-now',clock_timestamp())""", (TENANT_A, newbie['subject_id'], newbie['principal_id']))
    with pytest.raises(psycopg.Error, match='quorum_unreachable') as caught:
        open_as(f, 'requester', body('q-3', target_principal=f['approver']['principal_id']))
    detail = json.loads(caught.value.diag.message_detail)
    # S-B1: the quorum-th earliest vote_eligible_from of the others (second is eligible already; newbie's comes 24 hours from now).
    expected = admin.execute("select control.nexloop_workbench_vote_eligible_from(%s,'real',%s,'{owner}')", (TENANT_A, newbie['subject_id'])).fetchone()[0]
    assert set(detail) == {'eligible_from'} and datetime.fromisoformat(detail['eligible_from']) == expected  # as instants, not strings (PG trims trailing zeros)


def test_a_requester_principal_that_lost_standing_has_its_open_requests_ended(store):
    f = store
    admin = f['admin']
    waiting = open_as(f, 'requester', body('e-1'))['approval_id']
    approved = open_as(f, 'requester', body('e-2', reason='另一条'))['approval_id']
    decide(f, 'approver', approved)
    other = open_as(f, 'second', body('e-3'))['approval_id']
    by_alias = open_as(f, 'alias', body('e-4', reason='同一个人的另一个 principal'))['approval_id']
    ended = admin.execute("select control.nexloop_approval_end_for_requester(%s,'real',%s,'synthetic-actor','intent-end')",
                          (TENANT_A, f['requester']['principal_id'])).fetchone()[0]
    assert ended == 2 and row(f, waiting)[0] == 'expired' and row(f, approved)[0] == 'expired' and row(f, other)[0] == 'waiting_approval'
    assert row(f, by_alias)[0] == 'waiting_approval'  # R3 §8.5: by principal - the same person's other principal keeps its request
    reasons = admin.execute("select payload->>'reason' from runtime.audit_events where action='approval.expired' order by 1").fetchall()
    assert reasons == [('requester_no_longer_eligible',), ('requester_no_longer_eligible',)]


def test_an_execution_refusal_records_the_handlers_fixed_code(store):
    """The consumption result carries the code of a '<prefix>: <code>' message (ADR-039 §7.4); the pattern the gate uses."""
    f = store
    admin = f['admin']
    code = "select substring(%s from '^[a-z][a-z _]{0,63}: ([a-z_]{1,64})$')"
    assert admin.execute(code, ('workbench member: target_not_active',)).fetchone()[0] == 'target_not_active'
    assert admin.execute(code, ('value 42 is wrong',)).fetchone()[0] is None  # no fixed code: the sqlstate only


def test_a_requester_who_loses_the_role_has_old_approvals_ended_without_any_explicit_call(store):
    """C-B1 (L6 review of B2): a demotion or revocation (a role's stop in the role history, as the D2 handlers and the configurator
    write it) and a suspension (the membership leaving active) end the principal's open requests; an old approval never executes."""
    f = store
    admin = f['admin']
    approved = open_as(f, 'requester', body('c-1'))['approval_id']
    decide(f, 'approver', approved)
    admin.execute("""insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source)
        values(%s,%s,%s,'owner','stop','synthetic-demotion')""", (TENANT_A, f['requester']['subject_id'], f['requester']['principal_id']))
    assert row(f, approved)[0] == 'expired'
    assert admin.execute("select payload->>'reason' from runtime.audit_events where action='approval.expired'").fetchone() == ('requester_no_longer_eligible',)
    ended = admin.execute("select actor_id,payload->>'ended' from runtime.audit_events where action='approval.requester_ended'").fetchone()
    assert ended == ('system:member_change', '1')
    # The old approval is gone: the resubmission does not proceed (it opens a new request; the entry refuses executing it anyway).
    assert gate(f, 'requester', body('c-2'))['proceed'] is False
    # A suspension (membership leaving active) ends requests too.
    waiting = open_as(f, 'second', body('c-3'))['approval_id']
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    admin.execute("""update control.nexloop_browser_memberships set payload=payload||'{"status":"suspended"}' where principal_id=%s""",
                  (f['second']['principal_id'],))
    assert row(f, waiting)[0] == 'expired'


def test_settings_check_published_policies_and_keep_revoked_alone(store):
    f = store
    admin = f['admin']
    # I-B1: a policy published before the settings already uses the field as its condition: the settings are refused.
    admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
        summary_fields,published_by) values('consumer.erase',1,1,%s,1,'{owner}',86400,'{}','x')""", (Jsonb({'field': 'code_hmac', 'equals': 'x'}),))
    with pytest.raises(psycopg.Error, match='published policy uses an execution-time field'), admin.transaction():
        register_fields(admin, 'consumer.erase', ['code_hmac'])
    # I-B2: revoked never mixes with another status.
    with pytest.raises(psycopg.Error, match='invalid'), admin.transaction():
        admin.execute("insert into control.nexloop_approval_capability_settings(capability,target_statuses) values('retention.release_hold','{active,revoked}')")


def test_the_gate_checks_the_execution_hmacs_before_the_handler(store):
    """I-B3: a malformed execution-time HMAC at the execution refuses the call before the handler and consumes nothing."""
    f = store
    register_fields(f['admin'])
    approval = open_as(f, 'requester', body('g-1'))['approval_id']
    decide(f, 'approver', approval)
    refused('execution_field_invalid', gate, f, 'requester', body('g-2', code_hmac='not-a-hmac'))
    assert row(f, approval)[0] == 'approved'
