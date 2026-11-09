"""NX-062 M5a (docs/implementation/NX-062-M5-design.md §2, §5, §9 R1, R6 and the probe-capability rule) on real PostgreSQL.

The machine authority facts are derived from the grants at snapshot time and never stored; the machine Action authority assertion
recomputes every hash and locks the client, key and latest grant rows so a concurrent revoke serialises with it (two connections);
no authority fact row names a machine principal; and the approvals never let a machine decide, never count it as an eligible approver
and never give it the single-person mode, all judged by the machine client table. The write capability used here is registered by the
fixture only; a guard proves no migration, manifest or allowlist carries a machine-writable capability. Keys are generated at run time."""
import json
import pathlib
import threading
import time
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from nexloop_eios import machine_keys as K
from support.tenants import TENANT_A as TENANT
from test_machine_lifecycle_pg import desk, owner  # noqa: F401
from test_machine_reads_pg import grant, publish_types
from test_machine_realm_pg import publish
from test_workbench_actions_http import act
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBE = 'test.machine.write_probe'
KINDS = ('actor', 'grants', 'machine_authentication', 'policies', 'scope', 'subject')
MIGRATION = 'migration:0441_nx062_machine_authority.sql'


def register_probe(admin):
    """Fixture only: a non-human governed capability with a harmless handler, allowlisted as a write, granted by the owner path's row."""
    admin.execute("""create or replace function runtime.nexloop_test_write_probe(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb)
        returns jsonb language sql as $$ select jsonb_build_object('probe',true) $$""")
    admin.execute('alter function runtime.nexloop_test_write_probe(text,text,text,text,text,jsonb) owner to nexloop_owner')
    admin.execute("""insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task)
        values(%s,'write_probe','agent_or_human','runtime.nexloop_test_write_probe(text,text,text,text,text,jsonb)','NX-062')""", (PROBE,))
    admin.execute("""insert into control.nexloop_machine_capabilities(capability,revision,state,kind,description,source,recorded_by)
        values(%s,1,'active','write','probe',%s,'test')""", (PROBE, MIGRATION))


def grant_row(admin, state='active', types=('Commitment',)):
    """M5b extends the grant guard to write capabilities; until then the fixture writes the grant row past the read-verb guard."""
    admin.execute("set session_replication_role=replica")
    try:
        admin.execute("""insert into authz.nexloop_machine_client_grants(tenant_id,client_id,capability,revision,state,object_types,issued_by_principal,issue_request)
            select %s,'acme-agent',%s,coalesce(max(revision),0)+1,%s,%s,'test','m5a' from authz.nexloop_machine_client_grants
            where tenant_id=%s and client_id='acme-agent' and capability=%s""",
                      (TENANT, PROBE, state, list(types) if state == 'active' else [], TENANT, PROBE))
    finally:
        admin.execute("set session_replication_role=origin")


@pytest.fixture
def machine(desk):
    w = desk
    admin = w['admin']
    publish(w)
    publish_types(admin)
    register_probe(admin)
    with w['client']() as client:
        csrf = owner(client, w)
        assert act(client, csrf, 'register_machine_client', {'client_id': 'acme-agent', 'display_name': 'A', 'purpose': 'p'}).status_code == 200
        issued = act(client, csrf, 'issue_machine_key', {'client_id': 'acme-agent', 'world': 'real', 'expires_in_days': 30, 'scope': [PROBE, 'object.get']})
        assert issued.status_code == 200, issued.text
    key = K.parse(issued.json()['key'])
    digest = K.digest(key, w['peppers'], 1).hex()
    key_id = admin.execute("select key_id::text from authz.nexloop_machine_keys where prefix_id=%s", (key.prefix_id,)).fetchone()[0]
    grant_row(admin)
    return {**w, 'prefix': key.prefix_id, 'digest': digest, 'key_id': key_id, 'principal': 'nexloop-machine:acme-agent',
            'subject': 'nexloop-machine-subject:acme-agent'}


def keys(m, capability=PROBE):
    return {'subject': [m['subject']], 'actor': [m['principal']], 'machine_authentication': [m['key_id']], 'grants': [m['principal'], capability],
            'scope': [m['key_id'], capability], 'policies': [capability]}


def snap(conn, m, kind, key):
    return conn.execute('select authz.nexloop_machine_authority_fact_snapshot(%s,%s,%s,%s)', (m['prefix'], m['digest'], kind, key)).fetchone()[0]


def claims(conn, m, capability=PROBE, **override):
    facts = [{'kind': k, 'key': v, 'record_hash': snap(conn, m, k, v)['record_hash']} for k, v in keys(m, capability).items()]
    c = {'tenant_id': TENANT, 'credential_id': m['key_id'], 'principal_id': m['principal'], 'client_id': 'acme-agent', 'world': 'real',
         'operation': 'execute', 'capability': capability, 'expires_at': (datetime.now(UTC) + timedelta(seconds=20)).isoformat(), 'facts': facts}
    return {**c, **override}


def assert_authority(conn, m, c):
    return conn.execute('select authz.nexloop_machine_assert_action_authority(%s,%s,%s,%s)', (m['prefix'], m['digest'], 'real', json.dumps(c))).fetchone()[0]


def test_facts_are_derived_bound_to_the_key_and_change_with_every_grant_revision(machine):
    m = machine
    admin = m['admin']
    with psycopg.connect(m['dsn'] if 'dsn' in m else admin.info.dsn, autocommit=True) as api:
        api.execute('set role nexloop_api')
        got = {k: snap(api, m, k, v) for k, v in keys(m).items()}
        assert set(got) == set(KINDS) and all(g and len(g['record_hash']) == 64 for g in got.values())
        assert got['grants']['payload']['revision'] == 1 and got['scope']['payload']['in_scope'] is True
        assert m['digest'] not in json.dumps(got)  # the digest is never a fact
        # Another principal's or key's fact, or an unknown kind: refused.
        for kind, key in (('actor', ['nexloop-machine:other-agent']), ('grants', ['nexloop-machine:other-agent', PROBE]),
                          ('scope', ['00000000-0000-0000-0000-000000000000', PROBE]), ('membership', [m['principal']])):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                snap(api, m, kind, key)
        # R1: a revoke and a re-grant of the very same types still changes the grants hash.
        grant_row(admin, 'revoked')
        grant_row(admin, 'active')
        again = snap(api, m, 'grants', keys(m)['grants'])
        assert again['payload']['object_types'] == got['grants']['payload']['object_types'] and again['record_hash'] != got['grants']['record_hash']
    # No stored fact row for a machine principal, ever (R1, judged by the client table).
    assert admin.execute("select count(*) from authz.nexloop_authority_facts where %s=any(entity_key) or %s=any(entity_key)",
                         (m['principal'], m['subject'])).fetchone()[0] == 0
    for key in ([m['principal']], [m['subject']], ['x', m['principal']]):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='derived, never stored'):
            admin.execute("insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,'actor',%s,%s)",
                          (TENANT, key, json.dumps({'tenant_id': TENANT, 'kind': 'human'})))


def test_the_assertion_recomputes_every_fact_and_refuses_each_change(machine):
    m = machine
    admin = m['admin']
    ok = assert_authority(admin, m, claims(admin, m))
    assert ok['client_id'] == 'acme-agent' and ok['capability'] == PROBE and ok['grant_revision'] == 1
    # Not executable by any application role.
    with psycopg.connect(admin.info.dsn, autocommit=True) as api:
        api.execute('set role nexloop_api')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            assert_authority(api, m, claims(admin, m))
    good = claims(admin, m)
    for i in range(len(good['facts'])):  # every single hash is checked
        bad = json.loads(json.dumps(good))
        bad['facts'][i]['record_hash'] = '0' * 64
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='changed'):
            assert_authority(admin, m, bad)
    for override in ({'facts': good['facts'][:5]}, {'facts': good['facts'][:5] + good['facts'][:1]}, {'world': 'test'}, {'principal_id': 'x'},
                     {'client_id': 'other-agent'}, {'credential_id': '00000000-0000-0000-0000-000000000000'}, {'operation': 'read'},
                     {'expires_at': (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
                     {'expires_at': (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            assert_authority(admin, m, {**good, **override})
    # A key whose scope lacks the capability, its facts copied faithfully: still denied (in_scope false).
    with m['client']() as client:
        csrf = owner(client, m)
        narrow = act(client, csrf, 'rotate_machine_key', {'previous_key_id': m['key_id'], 'overlap_days': 1, 'scope': ['object.get'], 'expires_in_days': 10})
        assert narrow.status_code == 200, narrow.text
    nk = K.parse(narrow.json()['key'])
    m2 = {**m, 'prefix': nk.prefix_id, 'digest': K.digest(nk, m['peppers'], 1).hex(),
          'key_id': admin.execute("select key_id::text from authz.nexloop_machine_keys where prefix_id=%s", (nk.prefix_id,)).fetchone()[0]}
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='denied'):
        assert_authority(admin, m2, claims(admin, m2))
    # Revoked, then retired in the allowlist: the facts copied as they now are still refuse.
    grant_row(admin, 'revoked')
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='denied'):
        assert_authority(admin, m, claims(admin, m))
    grant_row(admin, 'active')
    assert assert_authority(admin, m, claims(admin, m))['grant_revision'] == 3
    admin.execute("insert into control.nexloop_machine_capabilities(capability,revision,state,kind,description,source,recorded_by) values(%s,2,'retired','write','retired',%s,'test')",
                  (PROBE, MIGRATION))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='denied'):
        assert_authority(admin, m, claims(admin, m))


def test_a_concurrent_revoke_serialises_with_the_assertion(machine):
    """R1, two connections: a revoke holding the client row waits the assertion out; once the revoke commits the assertion refuses."""
    m = machine
    admin = m['admin']
    c = claims(admin, m)
    revoker = psycopg.connect(admin.info.dsn)
    try:
        revoker.execute("select 1 from authz.nexloop_machine_clients where tenant_id=%s and client_id='acme-agent' for update", (TENANT,))
        revoker.execute("set local session_replication_role=replica")
        revoker.execute("""insert into authz.nexloop_machine_client_grants(tenant_id,client_id,capability,revision,state,object_types,issued_by_principal,issue_request)
            values(%s,'acme-agent',%s,2,'revoked','{}','test','m5a-race')""", (TENANT, PROBE))
        outcome = {}

        def run():
            with psycopg.connect(admin.info.dsn, autocommit=True) as other:
                try:
                    outcome['ok'] = assert_authority(other, m, c)
                except psycopg.Error as e:
                    outcome['error'] = e
        t = threading.Thread(target=run)
        t.start()
        for _ in range(100):
            if admin.execute("select count(*) from pg_stat_activity where wait_event_type='Lock' and query like '%%machine_assert_action_authority%%'").fetchone()[0]:
                break
            time.sleep(0.05)
        else:
            pytest.fail('the assertion did not wait for the revoke')
        assert t.is_alive() and not outcome
        revoker.commit()
        t.join(10)
        assert isinstance(outcome.get('error'), psycopg.errors.InsufficientPrivilege), outcome
    finally:
        revoker.close()


def holders(admin, f):
    return {r for (r,) in admin.execute("""select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
        where p.oid=%s::regprocedure and a.privilege_type='EXECUTE' and a.grantee<>p.proowner""", (f,)).fetchall()}


def test_approvals_never_let_a_machine_decide_count_or_approve_alone(machine):
    m = machine
    admin = m['admin']
    # 1. A machine decision row, whichever path writes it: refused by the client table (cancel and expiry rows are no decision).
    admin.execute("set session_replication_role=replica")  # a synthetic request row (its own guards are not under test)
    try:
        approval = admin.execute("""insert into control.nexloop_approval_requests(tenant_id,world,capability,action_resource,policy_version,request_digest,
            request_id,body,requester_subject,requester_principal,due_by) values(%s,'real','x','eios:action:x:1',1,%s,'m5a','{}','human-subject-a','someone',
            clock_timestamp()+interval '1 hour') returning approval_id""", (TENANT, '0' * 64)).fetchone()[0]
    finally:
        admin.execute("set session_replication_role=origin")
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    row = "insert into control.nexloop_approval_decisions(tenant_id,world,approval_id,outcome,actor_subject,actor_principal,reason) values(%s,'real',%s,%s,%s,%s,'x')"
    for outcome, subject, principal in (('approved', m['subject'], 'someone'), ('rejected', 'human-subject-a', m['principal'])):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='approver_unauthorized'):
            admin.execute(row, (TENANT, approval, outcome, subject, principal))
    admin.execute(row, (TENANT, approval, 'rejected', 'human-subject-a', 'someone'))  # a person's decision row is written
    # 2. and 3. The wrapped bodies stubbed inside a rolled-back transaction: a machine subject is never eligible, never solo.
    with psycopg.connect(admin.info.dsn) as tx:
        tx.execute("""create or replace function control.nexloop_approval_eligible_subjects_before_machine(p_tenant text,p_roles text[]) returns text[]
            language sql as $$ select array['human-subject-a',%s] $$""".replace('%s', "'" + m['subject'] + "'"))
        tx.execute("""create or replace function control.nexloop_approval_solo_effective_before_machine(p_tenant text,p_world text,p_capability text,
            p_policy_version integer,p_requester text) returns boolean language sql as $$ select true $$""")
        assert tx.execute("select control.nexloop_approval_eligible_subjects(%s,array['owner'])", (TENANT,)).fetchone()[0] == ['human-subject-a']
        for who, expected in ((m['subject'], False), (m['principal'], False), ('human-subject-a', True)):
            assert tx.execute("select control.nexloop_approval_solo_effective(%s,'real','x',1,%s)", (TENANT, who)).fetchone()[0] is expected
        tx.rollback()
    # Each wrapper keeps the grantees; the moved bodies are granted to no role.
    for f in ('control.nexloop_approval_eligible_subjects_before_machine(text,text[])',
              'control.nexloop_approval_solo_effective_before_machine(text,text,text,integer,text)'):
        assert holders(admin, f) == set(), f


def test_no_machine_writable_capability_is_published_anywhere(pg):
    """The probe capability lives in fixtures only (L6 on 63bb2c85): after every migration no allowlist row is a write and no governed
    capability is a test one; no deployment manifest names one either."""
    with psycopg.connect(pg, autocommit=True) as admin:
        assert admin.execute("select capability from control.nexloop_machine_capabilities where kind='write'").fetchall() == []
        assert admin.execute("select capability from control.nexloop_governed_capabilities where capability like 'test.%'").fetchall() == []
    for path in list((ROOT / 'deploy').rglob('*.json')) + list((ROOT / 'packages/eios-core/src/eios/migrations').glob('*.sql')):
        assert 'test.machine' not in path.read_text(encoding='utf-8'), path
