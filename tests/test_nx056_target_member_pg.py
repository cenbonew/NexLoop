"""NX-056 B1 (ADR-039 §6, draft revision R1): the approval view's target_member block on a clean catalog.

A test policy names the member a request acts on (target_subject_field target_principal, owners only - the NX-058 slice D shape);
the requester, owners and an operator read the view through runtime.nexloop_approval_view as the workbench observe read calls it.
The block is read-time only, follows the fixed order of §6.1 (null, hidden, invite_owner, erased, unresolved, member) and never
returns the frozen body's value. Admin seeds members, accounts and configuration. Synthetic ids only.
"""
import hashlib
import json
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_nx056_approvals_store_pg import ACTION, CAP, decide, store  # noqa: F401

TARGET_KEYS = {'kind', 'label', 'username', 'account_status', 'member_status', 'role'}


def target_policy(admin, version=2):
    admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
        summary_fields,target_subject_field,published_by) values(%s,%s,1,null,1,'{owner}',86400,'{}','target_principal','synthetic-config')""", (CAP, version))


def open_for(f, who, target, request_id=None):
    body = {'operation': 'set_real_dispatch', 'request_id': request_id or uuid.uuid4().hex, 'scope_kind': 'tenant', 'enabled': True,
            'reason': 'synthetic', 'target_principal': target}
    return f['admin'].execute('select control.nexloop_approval_open(%s,%s,%s,%s,%s,%s)',
                              (TENANT_A, 'real', CAP, ACTION, f[who]['principal_id'], Jsonb(body))).fetchone()[0]['approval_id']


def view(f, who):
    return f['admin'].execute("select runtime.nexloop_approval_view(%s,'real',%s)", (TENANT_A, f[who]['principal_id'])).fetchone()[0]


def row(rows, approval):
    return next(r for r in rows if r['approval_id'] == approval)


def label(principal):
    return 'p-' + hashlib.sha256(principal.encode()).hexdigest()[:8]


def test_an_owner_approver_sees_the_target_as_it_is_now(store):
    f = store
    target_policy(f['admin'])
    approval = open_for(f, 'requester', f['operator']['principal_id'])
    block = row(view(f, 'approver')['to_decide'], approval)['target_member']
    assert set(block) == TARGET_KEYS and block == {'kind': 'member', 'label': label(f['operator']['principal_id']), 'username': f['operator']['username'],
                                                    'account_status': 'active', 'member_status': 'active', 'role': 'operator'}
    assert f['operator']['principal_id'] not in str(block)  # the principal only as its label
    # The requester (an owner) sees the same block in their own columns.
    assert row(view(f, 'requester')['mine'], approval)['target_member'] == block
    # The target as it is now: a later role change shows at the next read.
    f['admin'].execute("""insert into control.nexloop_workbench_configurations(tenant_id,manifest_version,roles_version,application_id,roles,members,roles_digest,recorded_by)
        select tenant_id,2,2,application_id,roles,members||jsonb_build_object(%s::text,'reviewer'),roles_digest,recorded_by
        from control.nexloop_workbench_configurations where tenant_id=%s and manifest_version=1""", (f['operator']['principal_id'], TENANT_A))
    assert row(view(f, 'approver')['to_decide'], approval)['target_member']['role'] == 'reviewer'


def test_the_bodys_own_principal_is_shown_and_a_subject_id_maps_to_its_first_membership(store):
    f = store
    target_policy(f['admin'])
    by_alias = open_for(f, 'approver', f['alias']['principal_id'])  # the requester's second principal, as the body names it
    assert row(view(f, 'second')['to_decide'], by_alias)['target_member']['label'] == label(f['alias']['principal_id'])
    by_subject = open_for(f, 'approver', f['alias']['subject_id'])  # a subject id: first membership by status, then principal
    expected = min(f['requester']['principal_id'], f['alias']['principal_id'])
    assert row(view(f, 'second')['to_decide'], by_subject)['target_member']['label'] == label(expected)


def test_a_non_owner_caller_gets_hidden_without_any_resolution(store):
    f = store
    target_policy(f['admin'])
    approval = open_for(f, 'operator', f['approver']['principal_id'])  # an operator requester names an owner
    assert row(view(f, 'operator')['mine'], approval)['target_member'] == {'kind': 'hidden'}
    gone = open_for(f, 'operator', f['second']['principal_id'])
    f['admin'].execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    f['admin'].execute('delete from control.nexloop_browser_memberships where principal_id=%s', (f['second']['principal_id'],))
    # A target that exists and one that no longer does look the same to a non-owner (no enumeration through the difference).
    assert row(view(f, 'operator')['mine'], gone)['target_member'] == {'kind': 'hidden'}


def test_unresolved_and_erased_never_return_the_bodys_value(store):
    f = store
    admin = f['admin']
    target_policy(admin)
    gone = open_for(f, 'requester', f['second']['principal_id'])
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    admin.execute('delete from control.nexloop_browser_memberships where principal_id=%s', (f['second']['principal_id'],))
    block = row(view(f, 'approver')['to_decide'], gone)['target_member']
    assert block == {'kind': 'unresolved'} and f['second']['principal_id'] not in str(view(f, 'approver'))
    pseudonym = 'pseudonym:' + 'a' * 32
    payload = admin.execute('select payload from control.nexloop_browser_memberships where principal_id=%s', (f['operator']['principal_id'],)).fetchone()[0]
    admin.execute('insert into control.nexloop_browser_memberships values(%s,%s,%s,%s)',
                  (TENANT_A, pseudonym, f['operator']['subject_id'], Jsonb({**payload, 'principal_id': pseudonym})))
    erased = open_for(f, 'requester', pseudonym)
    assert row(view(f, 'approver')['to_decide'], erased)['target_member'] == {'kind': 'erased'}


def invite_capability(admin):
    """A registered human capability named as NX-058 slice D registers it, and its policy (the view reads the request's capability)."""
    admin.execute("""insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task)
        values('workbench_member.invite_owner','invite_workbench_owner','human','control.nexloop_governed_approval_cancel(text,text,text,text,text,jsonb)','NX-058')
        on conflict do nothing""")
    admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,condition,quorum,approver_roles,expires_after_seconds,
        summary_fields,solo_mode,published_by) values('workbench_member.invite_owner',1,1,null,1,'{owner}',86400,'{}','not_applicable','synthetic-config')""")


def invite(f, who, request_id, username):
    return f['admin'].execute('select control.nexloop_approval_open(%s,%s,%s,%s,%s,%s)', (
        TENANT_A, 'real', 'workbench_member.invite_owner', 'eios:action:nexloop.workbench_member.invite_owner:1', f[who]['principal_id'],
        Jsonb({'operation': 'invite_workbench_owner', 'request_id': request_id, 'username': username, 'expires_in_hours': 24}))).fetchone()[0]['approval_id']


def test_no_target_field_is_null_and_invite_owner_shows_the_invitee_to_owners_only(store):
    f = store
    approval = f['admin'].execute('select control.nexloop_approval_open(%s,%s,%s,%s,%s,%s)', (
        TENANT_A, 'real', CAP, ACTION, f['requester']['principal_id'],
        Jsonb({'operation': 'set_real_dispatch', 'request_id': 'plain', 'scope_kind': 'tenant', 'enabled': True, 'reason': 'x'}))).fetchone()[0]['approval_id']
    assert row(view(f, 'approver')['to_decide'], approval)['target_member'] is None  # the key is always there
    invite_capability(f['admin'])
    invited = invite(f, 'requester', 'invite', 'new-owner')
    # D7 (owner 2026-10-25: A): owners see the invitee's username and whether an account already has exactly that name.
    assert row(view(f, 'approver')['to_decide'], invited)['target_member'] == {'kind': 'invitee', 'username': 'new-owner', 'same_username_exists': False}
    clash = invite(f, 'requester', 'invite-2', f['operator']['username'])  # a request that bypassed the HTTP refusal
    assert row(view(f, 'approver')['to_decide'], clash)['target_member']['same_username_exists'] is True
    not_text = invite(f, 'requester', 'invite-4', 42)  # a username that is not a string: unresolved, never the value
    assert row(view(f, 'approver')['to_decide'], not_text)['target_member'] == {'kind': 'unresolved'}
    by_operator = invite(f, 'operator', 'invite-3', 'another-owner')
    mine = row(view(f, 'operator')['mine'], by_operator)
    assert mine['target_member'] == {'kind': 'hidden'} and 'another-owner' not in str(mine)  # non-owners: hidden, no username


def test_a_body_claiming_to_be_an_invitation_never_hides_the_real_target(store):
    """L6 review of fdc06ef2: the request's capability decides, never the operation the requester wrote into the body."""
    f = store
    target_policy(f['admin'])
    body = {'operation': 'invite_workbench_owner', 'request_id': 'forged', 'username': 'decoy', 'scope_kind': 'tenant', 'enabled': True,
            'target_principal': f['operator']['principal_id']}
    # NX-058 slice D2: approval_open refuses a body whose operation is not the capability's own, so such a request never opens ...
    with pytest.raises(psycopg.Error, match='operation_mismatch'):
        f['admin'].execute('select control.nexloop_approval_open(%s,%s,%s,%s,%s,%s)', (TENANT_A, 'real', CAP, ACTION, f['requester']['principal_id'], Jsonb(body)))
    # ... and a row that holds one anyway (written directly here) still shows the real target: the capability decides.
    f['admin'].execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    forged = f['admin'].execute("""insert into control.nexloop_approval_requests(tenant_id,world,capability,action_resource,policy_version,request_digest,request_id,body,
        requester_subject,requester_principal,due_by) select %s,'real',%s,%s,max(version),repeat('d',64),'forged',%s,%s,%s,clock_timestamp()+interval '1 hour'
        from control.nexloop_approval_policies where capability=%s returning approval_id""",
        (TENANT_A, CAP, ACTION, Jsonb(body), f['requester']['subject_id'], f['requester']['principal_id'], CAP)).fetchone()[0]
    block = row(view(f, 'approver')['to_decide'], str(forged))['target_member']
    assert block['kind'] == 'member' and block['username'] == f['operator']['username'] and 'decoy' not in str(block)


def test_a_target_policy_is_approved_by_owners_only_and_first_votes_are_counted(store):
    f = store
    admin = f['admin']
    with pytest.raises(psycopg.Error, match='owners only'), admin.transaction():
        admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,quorum,approver_roles,expires_after_seconds,
            target_subject_field,published_by) values(%s,5,1,1,'{owner,operator}',86400,'target_principal','x')""", (CAP,))
    approval = admin.execute('select control.nexloop_approval_open(%s,%s,%s,%s,%s,%s)', (
        TENANT_A, 'real', CAP, ACTION, f['requester']['principal_id'],
        Jsonb({'operation': 'set_real_dispatch', 'request_id': 'vote', 'scope_kind': 'tenant', 'enabled': True, 'reason': 'x'}))).fetchone()[0]['approval_id']
    decide(f, 'approver', approval)
    metrics = admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'approvals'", (TENANT_A,)).fetchone()[0]
    assert (metrics['first_time_approvers_24h'], metrics['same_person_flags_24h']) == (1, 0)


def test_rules_v16_are_v15_plus_the_first_vote_and_same_person_rules():
    # Batch 27: NX-057 R1/R2 published v15 (v14 plus the remediation rules); B1's two rules follow as v16.
    from nexloop_eios.observability import load_rules, validate_rules
    root = Path(__file__).resolve().parents[1] / 'deploy/configuration'
    v15, v16 = json.loads((root / 'alert-rules.v15.json').read_text()), load_rules(root / 'alert-rules.v16.json')
    assert v16['version'] == 16 and v16['rules'][:len(v15['rules'])] == v15['rules']
    assert {r['rule_id']: r['metric'] for r in v16['rules'][len(v15['rules']):]} == {
        'approval_first_time_approver': 'approvals.first_time_approvers_24h', 'approval_same_person_flag': 'approvals.same_person_flags_24h'}
    validate_rules(v16)
