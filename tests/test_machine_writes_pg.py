"""NX-062 M5b (docs/implementation/NX-062-M5-design.md §3, §4, §9 R2-R5) on real PostgreSQL through the API's own module
(nexloop_eios.machine_writes, nexloop_api pool, the real authority signing key).

prepare / commit / intent query of a write capability registered by the fixture only (test.machine.write_probe, whose handler records
each call); the 0124 entry renamed and wrapped (machine core); every C5 check re-run at commit, including after an approval; the intent
answers first, then expiry (R4); the business intent id carries the tenant (R3); the signed command binds client, intent, prepare digest
and world (R5). Keys are generated at run time."""
import hashlib
import json
import re

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from nexloop_eios import machine_writes as W
from support.tenants import TENANT_A as TENANT
from system_catalog_fixture import insert_active_actions
from support.vote_cooldown import ripen_approvers
from test_machine_authority_pg import PROBE, grant_row, machine, register_probe  # noqa: F401
from test_machine_lifecycle_pg import desk, owner  # noqa: F401
from test_machine_reads_pg import commitment, hexid, put
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

BASE_ENTRY_SHA = '3158bf4dfd1fdd86a31c90986515617ee09fde4d174ace9128e2d2eadc966d47'  # 0348's entry, as 0442 checks before renaming it


def probe_definition(admin):
    """Fixture only: the probe's Action definition, copied from a published lifecycle Action and re-bound to the probe capability."""
    d, cap = admin.execute("""select definition,capability from control.nexloop_action_definitions where tenant_id=%s and world='real' and active
        and definition->'capability_binding'->>'capability_name'='machine_client.register' limit 1""", (TENANT,)).fetchone()
    d = {**d, 'stable_name': PROBE, 'version': 1, 'capability_binding': {**d['capability_binding'], 'capability_name': PROBE}}
    cap = {**cap, 'capability_name': PROBE} if isinstance(cap, dict) else cap
    insert_active_actions(admin, TENANT, [{'definition': d, 'capability': cap}])


@pytest.fixture
def writes(machine, pg):
    m = machine
    admin = m['admin']
    admin.execute('create table runtime.nexloop_test_probe_calls(intent text,body jsonb,subject text)')
    admin.execute('alter table runtime.nexloop_test_probe_calls owner to nexloop_owner')
    admin.execute("""create or replace function runtime.nexloop_test_write_probe(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb)
        returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
        begin insert into runtime.nexloop_test_probe_calls values(p_intent,body,p_subject); return jsonb_build_object('probe',true); end $$""")
    admin.execute("""insert into control.nexloop_machine_write_capabilities(capability,object_types,param_keys,description,source_task)
        values(%s,array['Commitment'],array['type_name','object_id','value'],'probe','NX-062')""", (PROBE,))
    probe_definition(admin)
    signer = m['f']['reader'].signer
    pool = ConnectionPool(make_conninfo(pg, user='nexloop_api'), min_size=1, max_size=2, open=True)
    obj = put(admin, 'Commitment', commitment(hexid()))
    try:
        yield {**m, 'pool': pool, 'signer': signer, 'object': obj, 'pg': pg}
    finally:
        pool.close()


def ids(w):
    return {'prefix': w['prefix'], 'digest': w['digest']}


def params(w, value='v1'):
    return {'type_name': 'Commitment', 'object_id': w['object'], 'value': value}


def prepare(w, intent, p=None, capability=PROBE):
    return W.prepare(w['pool'], **ids(w), capability=capability, intent_id=intent, params=p if p is not None else params(w))


def commit(w, intent, prepared, p=None, capability=PROBE):
    return W.commit(w['pool'], w['signer'], **ids(w), capability=capability, intent_id=intent, params=p if p is not None else params(w),
                    prepare_digest=prepared['prepare_digest'])


def calls(w):
    return w['admin'].execute('select count(*) from runtime.nexloop_test_probe_calls').fetchone()[0]


def test_prepare_commit_query_and_replay(writes):
    w = writes
    admin = w['admin']
    p = prepare(w, 'intent-0001')
    assert set(p) == {'intent_id', 'prepare_digest', 'expires_at', 'normalized', 'replayed'} and p['normalized'] == params(w) and p['replayed'] is False
    assert prepare(w, 'intent-0001')['prepare_digest'] == p['prepare_digest']  # same parameters: the same prepare
    with pytest.raises(psycopg.Error) as e:
        prepare(w, 'intent-0001', params(w, 'other'))
    assert e.value.sqlstate == 'NX409'
    done = commit(w, 'intent-0001', p)
    assert done['phase'] == 'committed' and done['result_code'] == 'succeeded' and done['replayed'] is False and re.fullmatch('[0-9a-f]{64}', done['outcome_id'])
    assert calls(w) == 1
    assert admin.execute('select subject from runtime.nexloop_test_probe_calls').fetchone()[0] == 'machine'
    again = commit(w, 'intent-0001', p)
    assert again == {**done, 'replayed': True} and calls(w) == 1  # the first result, the handler not run again
    assert W.intent(w['pool'], **ids(w), intent_id='intent-0001') == {**{k: v for k, v in done.items() if k != 'replayed'}, 'approval_status': None}
    # R3: the governed entry's intent id is the business intent of (tenant, client, intent).
    business = 'machine:' + hashlib.sha256(json.dumps(['nexloop-machine-intent-v1', TENANT, 'acme-agent', 'intent-0001'], separators=(',', ':')).encode()).hexdigest()
    assert admin.execute('select intent_id from runtime.nexloop_action_claims where action_name=%s', (PROBE,)).fetchone()[0] == business
    # C6: the intent row holds no parameter and no value.
    row = json.dumps(admin.execute('select to_jsonb(i) from runtime.nexloop_machine_intents i').fetchone()[0])
    assert w['object'] not in row and 'v1' not in row
    # Audit: ids, codes and the key prefix, never the parameters.
    audit = admin.execute("select action,payload from runtime.audit_events where action like 'machine.intent.%%' order by created_at").fetchall()
    assert [a for a, _ in audit] == ['machine.intent.prepare', 'machine.intent.commit']
    assert all(p_['key_prefix'] == w['prefix'] and w['object'] not in json.dumps(p_) for _, p_ in audit)


def test_prepare_refuses_what_is_not_allowed(writes):
    w = writes
    admin = w['admin']
    for bad, code in (({**params(w), 'tenant_id': TENANT}, '22023'), ({**params(w), 'note': 'x'}, '22023'),
                      ({**params(w), 'type_name': 'CommercialRecord'}, 'P0002'), ({**params(w), 'object_id': hexid()}, 'P0002'),
                      ({'object_id': w['object'], 'value': 'x'}, 'P0002')):
        with pytest.raises(psycopg.Error) as e:
            prepare(w, 'intent-bad-' + code, bad)
        assert e.value.sqlstate == code, bad
    with pytest.raises(psycopg.Error) as e:
        prepare(w, 'short')
    assert e.value.sqlstate == '22023'
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        prepare(w, 'intent-scope', capability='object.get')  # in scope, but a read verb
    # A key whose scope lacks the capability (granted and allowlisted): refused at prepare.
    from nexloop_eios import machine_keys as K
    from test_workbench_actions_http import act
    with w['client']() as client:
        csrf = owner(client, w)
        narrow = act(client, csrf, 'rotate_machine_key', {'previous_key_id': w['key_id'], 'overlap_days': 1, 'scope': ['object.get'], 'expires_in_days': 10})
        assert narrow.status_code == 200, narrow.text
    nk = K.parse(narrow.json()['key'])
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='not_permitted'):
        W.prepare(w['pool'], prefix=nk.prefix_id, digest=K.digest(nk, w['peppers'], 1).hex(), capability=PROBE, intent_id='intent-narrow', params=params(w))
    grant_row(admin, 'revoked')
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='not_permitted'):
        prepare(w, 'intent-revoked')
    grant_row(admin, 'active')
    # An effect capability without an approval policy (C5 2.5).
    admin.execute("insert into control.nexloop_machine_capabilities(capability,revision,state,kind,effect,description,source,recorded_by) values(%s,2,'active','write',true,'effect','migration:0442_nx062_machine_writes.sql','test')",
                  (PROBE,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='approval_policy_required'):
        prepare(w, 'intent-effect')


def test_commit_checks_digest_expiry_and_answers_a_finished_intent_first(writes):
    w = writes
    admin = w['admin']
    p = prepare(w, 'intent-0002')
    for bad in (params(w, 'changed'), {**params(w), 'value': 'v1 '}):
        with pytest.raises(psycopg.Error) as e:
            commit(w, 'intent-0002', p, bad)
        assert e.value.sqlstate == 'NX409'
    with pytest.raises(psycopg.Error) as e:
        commit(w, 'intent-0002', {'prepare_digest': '0' * 64})
    assert e.value.sqlstate == 'NX409'
    with pytest.raises(psycopg.Error) as e:
        commit(w, 'intent-none', p)
    assert e.value.sqlstate == 'P0002'
    assert calls(w) == 0
    admin.execute("update runtime.nexloop_machine_intents set expires_at=clock_timestamp()-interval '1 second' where intent_id='intent-0002'")
    with pytest.raises(psycopg.Error) as e:
        commit(w, 'intent-0002', p)
    assert e.value.sqlstate == 'NX410'
    # R4: committed, then past the prepare's expiry: the first result, not 410.
    q = prepare(w, 'intent-0003')
    first = commit(w, 'intent-0003', q)
    admin.execute("update runtime.nexloop_machine_intents set expires_at=clock_timestamp()-interval '1 hour' where intent_id='intent-0003'")
    assert {k: v for k, v in commit(w, 'intent-0003', q).items() if k != 'expires_at'} == {**{k: v for k, v in first.items() if k != 'expires_at'}, 'replayed': True}
    with pytest.raises(psycopg.Error) as e:
        prepare(w, 'intent-0003')
    assert e.value.sqlstate == 'NX409'


def test_every_authority_check_runs_again_at_commit(writes):
    w = writes
    admin = w['admin']
    p = prepare(w, 'intent-0004')
    grant_row(admin, 'revoked')  # revoked between prepare and commit
    refused = commit(w, 'intent-0004', p)
    assert refused['phase'] == 'refused' and refused['result_code'] == 'not_permitted' and calls(w) == 0
    assert commit(w, 'intent-0004', p) == {**refused, 'replayed': True}
    grant_row(admin, 'active')
    p = prepare(w, 'intent-0005')
    admin.execute("insert into control.nexloop_machine_capabilities(capability,revision,state,kind,description,source,recorded_by) values(%s,2,'retired','write','retired','migration:0442_nx062_machine_writes.sql','test')",
                  (PROBE,))
    assert commit(w, 'intent-0005', p)['result_code'] == 'not_permitted' and calls(w) == 0


def test_a_human_capability_is_refused_even_when_misregistered(writes):
    """C7: the core's own subject_rule check, with the allowlist and the write registry bypassed (a mis-registration)."""
    w = writes
    admin = w['admin']
    p = prepare(w, 'intent-0006')
    admin.execute("set session_replication_role=replica")
    try:
        admin.execute("update control.nexloop_governed_capabilities set subject_rule='human' where capability=%s", (PROBE,))
    finally:
        admin.execute("set session_replication_role=origin")
    assert commit(w, 'intent-0006', p)['result_code'] == 'not_permitted' and calls(w) == 0


def test_the_signed_command_and_the_entry(writes):
    w = writes
    admin = w['admin']
    p = prepare(w, 'intent-0007')
    payload = json.dumps({**params(w), 'operation': 'write_probe',
                          'request_id': 'machine:' + hashlib.sha256(json.dumps(['nexloop-machine-intent-v1', TENANT, 'acme-agent', 'intent-0007'], separators=(',', ':')).encode()).hexdigest()},
                         sort_keys=True, separators=(',', ':'))
    with w['pool'].connection() as c:
        text, sig = W.signed_command(c, w['signer'], **ids(w), capability=PROBE, intent_id='intent-0007', prepare_digest=p['prepare_digest'])
    claims = json.loads(text)
    assert {claims[k] for k in ('client_id', 'intent_id', 'prepare_digest', 'world')} == {'acme-agent', 'intent-0007', p['prepare_digest'], 'real'}
    entry = 'select authz.nexloop_goal_governed_action(%s,%s,%s,%s,%s)'
    nxm = f"nxm:{w['prefix']}:{w['digest']}"
    with psycopg.connect(make_conninfo(w['pg'], user='nexloop_api'), autocommit=True) as api:
        # R5: each bound field changed (signature redone with the real key) or the signature itself: refused.
        import hmac
        for field, value in (('client_id', 'other-agent'), ('intent_id', 'intent-0008'), ('prepare_digest', '0' * 64), ('world', 'test')):
            t = json.dumps({**claims, field: value}, sort_keys=True, separators=(',', ':'))
            s = hmac.new(w['signer'].material, (W.PROTOCOL + ':' + t).encode(), 'sha256').hexdigest()
            with pytest.raises(psycopg.Error) as e:
                api.execute(entry, (nxm, 'real', t, s, payload))
            assert e.value.sqlstate in ('42501', 'NX409', 'P0002'), field
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api.execute(entry, (nxm, 'real', text, '0' * 64, payload))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api.execute(entry, ('nxm:bad', 'real', text, sig, payload))
        # The renamed entry is no role's; with the machine digest it is refused by the service identity (C1, R2).
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api.execute('select authz.nexloop_goal_governed_action_before_machine(%s,%s,%s,%s,%s)', (w['digest'], 'real', text, sig, payload))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute('select authz.nexloop_goal_governed_action_before_machine(%s,%s,%s,%s,%s)', (w['digest'], 'real', text, sig, payload))
    assert calls(w) == 0
    with w['pool'].connection() as c:  # the genuine command still works
        assert c.execute('select authz.nexloop_machine_commit(%s,%s,%s,%s,%s::jsonb,%s,%s,%s)',
                         (w['prefix'], w['digest'], PROBE, 'intent-0007', json.dumps(params(w)), p['prepare_digest'], text, sig)).fetchone()[0]['phase'] == 'committed'


def approve(w, approval_id):
    admin = w['admin']
    principal = admin.execute("select principal_id from control.nexloop_browser_memberships where tenant_id=%s and payload->>'status'='active' order by principal_id",
                              (TENANT,)).fetchall()
    for (who,) in principal:
        try:
            return admin.execute('select control.nexloop_governed_approval_decide(%s,%s,%s,%s,%s,%s)',
                                 (TENANT, 'real', who, 'human', 'decide-' + who, json.dumps({'operation': 'decide_approval', 'request_id': 'decide-' + who,
                                  'approval_id': str(approval_id), 'decision': 'approve', 'reason': 'ok'}))).fetchone()[0]
        except psycopg.Error:
            continue
    raise AssertionError('no member could approve')


def test_after_approval_every_check_runs_again(writes):
    """R4 and L6 (098caeac): a machine request waits for approval; approved, it runs on a second commit; revoked during the approval,
    the second commit is refused and the handler never runs."""
    w = writes
    admin = w['admin']
    # The policy guard (NX-056) admits human capabilities only, so no machine request meets an approval in production yet (the effect
    # capabilities of phase two will need that guard widened); the path is proven with a policy written past the guard.
    admin.execute("set session_replication_role=replica")
    try:
        admin.execute("""insert into control.nexloop_approval_policies(capability,version,action_version,quorum,approver_roles,expires_after_seconds,solo_mode,published_by)
            values(%s,1,1,1,array['owner'],3600,'not_applicable','test')""", (PROBE,))
    finally:
        admin.execute("set session_replication_role=origin")
    admin.execute("select control.nexloop_approval_eligibility_record(%s)", (TENANT,))
    ripen_approvers(admin, TENANT)  # NX-058 slice D §2: the synthetic owner votes at once
    p = prepare(w, 'intent-0009')
    waiting = commit(w, 'intent-0009', p)
    assert waiting['phase'] == 'pending_approval' and waiting['approval_id'] and calls(w) == 0
    assert W.intent(w['pool'], **ids(w), intent_id='intent-0009')['approval_status'] == 'waiting_approval'
    assert approve(w, waiting['approval_id'])['status'] == 'approved'
    done = commit(w, 'intent-0009', p)
    assert done['phase'] == 'committed' and calls(w) == 1
    # Revoked while waiting: refused at execution.
    p = prepare(w, 'intent-0010', params(w, 'v2'))
    waiting = commit(w, 'intent-0010', p, params(w, 'v2'))
    assert waiting['phase'] == 'pending_approval'
    approve(w, waiting['approval_id'])
    grant_row(admin, 'revoked')
    refused = commit(w, 'intent-0010', p, params(w, 'v2'))
    assert refused['phase'] == 'refused' and refused['result_code'] == 'not_permitted' and calls(w) == 1


def defs(admin):
    return {name: src for name, src in admin.execute("""select p.oid::regprocedure::text,p.prosrc from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname in ('authz','control','runtime','ontology','public')""").fetchall()}


def test_entry_parity_and_no_other_handler_caller(writes):
    w = writes
    admin = w['admin']
    base = admin.execute("select pg_get_functiondef('authz.nexloop_goal_governed_action_before_machine(text,text,text,text,text)'::regprocedure)").fetchone()[0]
    original = base.replace('authz.nexloop_goal_governed_action_before_machine(', 'authz.nexloop_goal_governed_action(', 1)
    assert hashlib.sha256(original.encode()).hexdigest() == BASE_ENTRY_SHA  # R2: any change of the base entry is red here
    core = admin.execute("select prosrc from pg_proc where oid='authz.nexloop_machine_governed_core(text,text,text,text,text,text)'::regprocedure").fetchone()[0]
    shared = base[base.index(' -- NX-056 A2 (design v5 §2, §4)'):base.index(' v_id:=encode(sha256(')]
    assert shared.strip() and shared in core  # the gate, the handler and the consumption are the base text unchanged
    all_defs = defs(admin)
    assert {n for n, s in all_defs.items() if 'nexloop_goal_governed_action_before_machine' in s} == {'authz.nexloop_goal_governed_action(text,text,text,text,text)'}
    assert {n for n, s in all_defs.items() if 'cap.handler::oid::regproc' in s} == {
        'authz.nexloop_goal_governed_action_before_machine(text,text,text,text,text)', 'authz.nexloop_machine_governed_core(text,text,text,text,text,text)'}


def holders(admin, f):
    return {r for (r,) in admin.execute("""select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
        where p.oid=%s::regprocedure and a.privilege_type='EXECUTE' and a.grantee<>p.proowner""", (f,)).fetchall()}


def test_grantees(writes):
    admin = writes['admin']
    for f in ('authz.nexloop_goal_governed_action_before_machine(text,text,text,text,text)', 'authz.nexloop_machine_governed_core(text,text,text,text,text,text)',
              'control.nexloop_approval_subject_of_before_machine(text,text)', 'control.nexloop_machine_verb_types_before_writes(text)'):
        assert holders(admin, f) == set(), f
    assert 'nexloop_api' in holders(admin, 'authz.nexloop_goal_governed_action(text,text,text,text,text)')
    for f in ('authz.nexloop_machine_prepare(text,text,text,text,jsonb)', 'authz.nexloop_machine_commit(text,text,text,text,jsonb,text,text,text)',
              'authz.nexloop_machine_intent(text,text,text)', 'authz.nexloop_machine_write_action(text,text,text)'):
        assert holders(admin, f) == {'nexloop_api'}, f
    # The machine requester of an approval is its client subject; a person's is unchanged.
    assert admin.execute("select control.nexloop_approval_subject_of(%s,'nexloop-machine:acme-agent')", (TENANT,)).fetchone()[0] == 'nexloop-machine-subject:acme-agent'
