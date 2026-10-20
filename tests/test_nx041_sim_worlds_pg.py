"""NX-041 S2 (design D2, D10, §11 S2): the simulation world registry, the governed owner Actions that create and delete a world,
credentials per world (AT-046: a sim credential never reaches real or another sim world; a real or test principal is never carried
into a sim world) and the whole deletion (a 30-day summary of counts and digests, resumable and idempotent, real and test untouched).

Real PostgreSQL on a clean catalog. Admin calls the SQL handlers the governed entry calls (as test_semantic_release_pg) and seeds
rows as trusted configuration; credentials are checked at authz.nexloop_service_identity, the gate every service path uses.
"""
import hashlib
import json
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from authority_fixture import seed_authority
from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_B

ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]
MARK = 'zz-sim-content-marker'  # row content that must never reach the summary


def call(admin, fn, body, tenant=TENANT_A, world='real'):
    return admin.execute(f"select control.{fn}(%s,%s,'owner-principal','owner-subject',%s,%s)",
                         (tenant, world, 'intent-' + uuid.uuid4().hex[:12], Jsonb(body))).fetchone()[0]


def create(admin, kind='simulation', request=None, tenant=TENANT_A, purpose='价格敏感度实验'):
    return call(admin, 'nexloop_governed_create_sim_world', {'operation': 'create_sim_world', 'request_id': request or uuid.uuid4().hex,
                                                             'kind': kind, 'purpose': purpose, 'expires_in_days': 30}, tenant)


def delete(admin, world, tenant=TENANT_A):
    return call(admin, 'nexloop_governed_delete_sim_world', {'operation': 'delete_sim_world', 'request_id': uuid.uuid4().hex, 'world': world,
                                                             'reason': '实验结束'}, tenant)


def identity(admin, token, world):
    return admin.execute('select authz.nexloop_service_identity(%s,%s)', (hashlib.sha256(token.encode()).hexdigest(), world)).fetchone()[0]


def denied(admin, token, world):
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='service authentication denied'), admin.transaction():
        identity(admin, token, world)
    return True


@pytest.fixture
def db(admin):
    bootstrap(admin)
    for t in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (t,))
    return admin


def world_row(admin, world):
    return admin.execute('select tenant_id,status,deleted_at is not null from control.nexloop_sim_worlds where world=%s', (world,)).fetchone()


def test_the_owner_creates_a_world_once_per_request_and_real_or_test_are_never_registered(db):
    request = uuid.uuid4().hex
    first = create(db, request=request)
    assert first['created'] is True and first['status'] == 'active' and first['world'].startswith('sim:')
    assert db.execute('select runtime.nexloop_world_valid(%s)', (first['world'],)).fetchone()[0] is True
    again = create(db, request=request)
    assert (again['world'], again['created']) == (first['world'], False)  # a retried request gives the same world
    assert create(db, kind='shadow')['world'].startswith('shadow:')
    assert world_row(db, first['world'])[:2] == (TENANT_A, 'active')
    assert db.execute("select created_by,purpose,expires_at>created_at from control.nexloop_sim_worlds where world=%s",
                      (first['world'],)).fetchone() == ('owner-principal', '价格敏感度实验', True)
    # Real, test, the data mode 'simulation' and malformed worlds are refused by the S1 check.
    for world in ('real', 'test', 'simulation', 'sim:1'):
        with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
            db.execute("""insert into control.nexloop_sim_worlds(world,tenant_id,created_by,purpose,status,expires_at,request_id)
                values(%s,%s,'x','x','active',clock_timestamp()+interval '1 day','x')""", (world, TENANT_A))
    # Invalid bodies, a reused request with another kind, and a call outside the owner's real world are refused.
    for body in ({'operation': 'create_sim_world', 'request_id': 'r1', 'kind': 'real', 'purpose': 'x', 'expires_in_days': 1},
                 {'operation': 'create_sim_world', 'request_id': 'r1', 'kind': 'simulation', 'purpose': ' ', 'expires_in_days': 1},
                 {'operation': 'create_sim_world', 'request_id': 'r1', 'kind': 'simulation', 'purpose': 'x', 'expires_in_days': 366},
                 {'operation': 'create_sim_world', 'request_id': 'r1', 'kind': 'simulation', 'purpose': 'x', 'expires_in_days': 1, 'world': 'real'},
                 {'operation': 'create_sim_world', 'request_id': request, 'kind': 'shadow', 'purpose': '价格敏感度实验', 'expires_in_days': 30}):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='sim world: '), db.transaction():
            call(db, 'nexloop_governed_create_sim_world', body)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='owner_world_only'), db.transaction():
        call(db, 'nexloop_governed_create_sim_world', {'operation': 'create_sim_world', 'request_id': 'r2', 'kind': 'simulation', 'purpose': 'x',
                                                       'expires_in_days': 1}, world='test')
    # The registry never loses a row and its status moves forward only.
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute('delete from control.nexloop_sim_worlds where world=%s', (first['world'],))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("update control.nexloop_sim_worlds set status='deleted',deleted_at=now() where world=%s", (first['world'],))


def test_both_actions_are_owner_only_governed_human_actions(db):
    rows = dict(db.execute("select capability,subject_rule from control.nexloop_governed_capabilities where source_task='NX-041'").fetchall())
    assert rows == {'sim_world.create': 'human', 'sim_world.delete': 'human'}
    roles = json.loads((ROOT / 'deploy/authorization/workbench-roles.v1.json').read_text())['roles']
    for name in ('nexloop.sim_world.create', 'nexloop.sim_world.delete'):
        holders = sorted(r for r, grants in roles.items() if f'eios:action:{name}:1' in grants)
        assert holders == ['owner'], (name, holders)
    from nexloop_eios.business_actions import PROFILES
    assert PROFILES['sim_world.create'] == PROFILES['sim_world.delete'] == 'human_owner'


def test_a_sim_credential_reaches_only_its_own_world(db):
    a, b = create(db)['world'], create(db)['world']
    token_a, _ = seed_authority(db, TENANT_A, 'eios:artifact:' + 'a' * 32, world=a, identity_suffix='-sim-a')
    token_b, _ = seed_authority(db, TENANT_A, 'eios:artifact:' + 'b' * 32, world=b, identity_suffix='-sim-b')
    token_real, _ = seed_authority(db, TENANT_A, 'eios:artifact:' + 'c' * 32, world='real', identity_suffix='-real')
    assert identity(db, token_a, a)['binding']['tenant_id'] == TENANT_A
    # AT-046 negatives: sim A in real, test and sim B; the real credential in sim A.
    assert denied(db, token_a, 'real') and denied(db, token_a, 'test') and denied(db, token_a, b) and denied(db, token_b, a)
    assert denied(db, token_real, a)
    row = db.execute('select tenant_id,credential_id,binding,audience,expires_at from authz.nexloop_service_credentials where worlds=%s', ([a],)).fetchone()

    def insert(worlds, suffix, principal=None, tenant=TENANT_A):
        binding = {**row[2], 'credential_id': 'k' + suffix, 'tenant_id': tenant, 'credential_tenant_id': tenant}
        if principal:
            binding.update(principal_id=principal, subject_principal_id=principal)
        db.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at) values(%s,%s,%s,%s,%s,%s,%s,%s)',
                   (uuid.uuid4().hex * 2, tenant, 'k' + suffix, Jsonb(binding), worlds, row[3], 'active', row[4]))
    unregistered = 'sim:' + str(uuid.uuid4())
    refusals = {
        'two sim worlds': lambda: insert([a, b], '1', 'p1'),
        'sim and test': lambda: insert([a, 'test'], '2', 'p2'),
        'sim and real': lambda: insert(['real', a], '3', 'p3'),
        'unregistered world': lambda: insert([unregistered], '4', 'p4'),
        'registered for another tenant': lambda: insert([a], '5', 'p5', tenant=TENANT_B),
        'a second active credential': lambda: insert([a], '6', 'p6'),
        'the real principal carried into a sim world': lambda: insert([b], '7', TENANT_A + '-real-principal'),
        'the sim principal given a real credential': lambda: insert(['real'], '8', TENANT_A + '-sim-a-principal'),
        'the sim principal given a test credential': lambda: insert(['test'], '9', TENANT_A + '-sim-a-principal'),
    }
    for label, attempt in refusals.items():
        with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.CheckViolation)), db.transaction():
            attempt()
        assert label
    # A sim credential's worlds never change; once revoked it never returns.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='never changed'), db.transaction():
        db.execute('update authz.nexloop_service_credentials set worlds=%s where worlds=%s', ([b], [a]))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='never changed'), db.transaction():
        db.execute("update authz.nexloop_service_credentials set worlds=%s where worlds=array['real'] and credential_id=%s", ([a], row[1].replace('-sim-a', '-real')))
    # Deleting the world revokes its credential: it no longer authenticates even in its own world; sim B is unaffected.
    assert delete(db, a)['credentials_revoked'] == 1
    assert denied(db, token_a, a) and identity(db, token_b, b)['binding']['tenant_id'] == TENANT_A
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='never reactivated'), db.transaction():
        db.execute("update authz.nexloop_service_credentials set status='active' where worlds=%s", ([a],))
    # A deleted world takes no new credential either.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='not registered or not active'), db.transaction():
        insert([a], '10', 'p10')


def seed_rows(admin, tenant, world, n, tag):
    """Rows in three kinds of table holding a world: plain (read audit counts, work feed) and append-only with its own purge flag
    (semantic access records). Seeded as trusted configuration with triggers off."""
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        for i in range(n):
            admin.execute("insert into runtime.nexloop_read_audit_daily(tenant_id,world,day,read_purpose,object_kind,reads) values(%s,%s,current_date-%s,%s,'object',1)",
                          (tenant, world, i, tag))
            admin.execute("insert into runtime.nexloop_work_feed(tenant_id,world,feed,item_key,payload) values(%s,%s,'claim-match',%s,%s)",
                          (tenant, world, f'{tag}-{i}', Jsonb({'note': MARK})))
            admin.execute("""insert into runtime.nexloop_semantic_accesses(tenant_id,world,access_id,kind,operation,query_kind,term_hashes,coverage,ambiguity)
                values(%s,%s,%s,'access','browse','any',%s,0,false)""", (tenant, world, uuid.uuid4(), ['a' * 64]))


def totals(admin, worlds):
    """Rows per world-holding table for the given worlds (all tenants)."""
    out = {}
    for (t,) in admin.execute('select t::text from runtime.nexloop_sim_world_tables() t').fetchall():
        out[t] = admin.execute(f'select count(*) from {t} where world=any(%s)', (list(worlds),)).fetchone()[0]
    return out


def test_whole_deletion_keeps_a_summary_is_resumable_and_never_touches_real_or_test(db):
    a, b = create(db)['world'], create(db, kind='shadow')['world']
    other = create(db, tenant=TENANT_B)['world']
    for tenant, world in ((TENANT_A, 'real'), (TENANT_A, 'test'), (TENANT_B, 'real'), (TENANT_A, b), (TENANT_B, other)):
        seed_rows(db, tenant, world, 3, 'keep')
    seed_rows(db, TENANT_A, a, 5, 'gone')
    before_kept = totals(db, ['real', 'test', b, other])
    assert sum(totals(db, [a]).values()) == 15
    # Another tenant cannot delete A's world; real and test are not simulation worlds.
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='not_registered'), db.transaction():
        delete(db, a, tenant=TENANT_B)
    for world in ('real', 'test', 'simulation'):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='not_a_simulation_world'), db.transaction():
            delete(db, world)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='sim world: invalid'), db.transaction():
        db.execute("select runtime.nexloop_sim_world_purge(%s,'real',10)", (TENANT_A,))
    # The Action starts the deletion; simulate an interruption by running bounded passes of 4 rows from here on.
    db.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    db.execute("update control.nexloop_sim_worlds set status='deleting' where world=%s", (a,))  # as the Action does before its first pass
    db.execute('select runtime.nexloop_sim_world_snapshot(%s,%s)', (TENANT_A, a))
    passes = []
    while True:
        r = db.execute('select runtime.nexloop_sim_world_purge(%s,%s,4)', (TENANT_A, a)).fetchone()[0]
        passes.append(r)
        assert totals(db, ['real', 'test', b, other]) == before_kept  # after every pass
        if r['status'] == 'deleted':
            break
        assert r['deleted'] <= 4 and len(passes) < 20, passes
    assert len(passes) >= 4 and passes[-1]['remaining'] == 0 and sum(p['deleted'] for p in passes) == 15
    assert sum(totals(db, [a]).values()) == 0 and world_row(db, a)[1:] == ('deleted', True)
    # Idempotent: the Action and the pass on a deleted world change nothing.
    assert delete(db, a)['status'] == 'deleted'
    assert db.execute('select runtime.nexloop_sim_world_purge(%s,%s,4)', (TENANT_A, a)).fetchone()[0]['deleted'] == 0
    # The summary: counts per table and digests only, no row content.
    counts, digests, done = db.execute('select counts,digests,completed_at is not null from control.nexloop_sim_world_summaries where world=%s', (a,)).fetchone()
    assert counts == {'runtime.nexloop_read_audit_daily': 5, 'runtime.nexloop_semantic_accesses': 5, 'runtime.nexloop_work_feed': 5} and done
    assert set(digests) == set(counts) and all(len(d) == 64 for d in digests.values())
    row_text = db.execute('select s::text from control.nexloop_sim_world_summaries s where world=%s', (a,)).fetchone()[0]
    assert MARK not in row_text and 'gone-' not in row_text
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("update control.nexloop_sim_world_summaries set counts='{}' where world=%s", (a,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute('delete from control.nexloop_sim_world_summaries where world=%s', (a,))
    assert totals(db, ['real', 'test', b, other]) == before_kept


def test_the_delete_action_runs_a_pass_and_retention_resumes_then_removes_the_summary_after_30_days(db):
    a = create(db)['world']
    seed_rows(db, TENANT_A, a, 2, 'gone')
    seed_rows(db, TENANT_A, 'real', 2, 'keep')
    kept = totals(db, ['real'])
    result = delete(db, a)
    assert (result['status'], result['remaining'], result['deleted']) == ('deleted', 0, 6) and totals(db, ['real']) == kept
    # Retention: within 30 days the summary stays; after, the sweep removes it (the registry row stays 'deleted').
    sweep = lambda: db.execute("select runtime.nexloop_retention_sweep(%s,'real','sim_world_summary',100,'sweep:t','service:retention_keeper')",  # noqa: E731
                               (TENANT_A,)).fetchone()[0]
    assert sweep()['processed'] == 0
    db.execute("update control.nexloop_sim_world_summaries set completed_at=completed_at-interval '31 days' where world=%s", (a,))
    assert sweep()['processed'] == 1
    assert db.execute('select count(*) from control.nexloop_sim_world_summaries where world=%s', (a,)).fetchone()[0] == 0
    assert world_row(db, a)[1] == 'deleted'
    # A world left deleting (an interrupted deletion) is continued by the same retention pass.
    c = create(db)['world']
    seed_rows(db, TENANT_A, c, 2, 'gone')
    db.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    db.execute('select runtime.nexloop_sim_world_snapshot(%s,%s)', (TENANT_A, c))
    db.execute("update control.nexloop_sim_worlds set status='deleting' where world=%s", (c,))
    assert sweep()['counts']['worlds_resumed'] == 1 and world_row(db, c)[1] == 'deleted'
    # No real row left; the retention pass only added its own tombstone (and its ledger entry) in the keeper's world.
    after = totals(db, ['real'])
    assert all(after[t] >= kept[t] for t in kept) and {t for t in kept if after[t] != kept[t]} <= {'runtime.nexloop_erasure_tombstones', 'control.nexloop_deletion_ledger'}


def test_metrics_export_iterates_registered_active_sim_worlds(db):
    a, b = create(db)['world'], create(db, kind='shadow')['world']
    gone = create(db)['world']
    delete(db, gone)
    active = [r[0] for r in db.execute('select runtime.nexloop_sim_worlds_active(%s)', (TENANT_A,)).fetchall()]
    assert active == sorted([a, b])
    src = db.execute("select pg_get_functiondef('authz.nexloop_metrics_export()'::regprocedure)").fetchone()[0]
    assert "('real'),('test')" in src and 'runtime.nexloop_sim_worlds_active(t.tenant_id)' in src
    snap = db.execute('select runtime.nexloop_metrics_snapshot(%s,%s)', (TENANT_A, a)).fetchone()[0]
    assert isinstance(snap, dict)  # the snapshot answers for a registered sim world


def test_python_validators_mirror_the_sql_bodies():
    from nexloop_eios.workbench_actions_http import OPERATIONS
    ops = OPERATIONS['create_sim_world']
    assert ops['kind']('shadow') == 'shadow' and ops['expires_in_days'](30) == 30
    for bad in (lambda: ops['kind']('real'), lambda: ops['expires_in_days'](0), lambda: ops['expires_in_days'](366),
                lambda: OPERATIONS['delete_sim_world']['world']('real'), lambda: OPERATIONS['delete_sim_world']['world']('sim:1')):
        with pytest.raises(ValueError):
            bad()
    assert OPERATIONS['delete_sim_world']['world']('sim:' + str(uuid.uuid4()))
