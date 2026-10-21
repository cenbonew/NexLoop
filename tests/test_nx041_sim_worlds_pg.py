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
    rows = dict(db.execute("select capability,subject_rule from control.nexloop_governed_capabilities where source_task='NX-041' and capability like 'sim_world.%'").fetchall())
    assert rows == {'sim_world.create': 'human', 'sim_world.delete': 'human', 'sim_world.generate': 'human', 'sim_world.publish_catalog': 'human'}
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

    def insert(worlds, suffix, principal=None, tenant=TENANT_A, use='api'):
        binding = {**row[2], 'credential_id': 'k' + suffix, 'tenant_id': tenant, 'credential_tenant_id': tenant}
        if principal:
            binding.update(principal_id=principal, subject_principal_id=principal)
        db.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at,sim_use) values(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                   (uuid.uuid4().hex * 2, tenant, 'k' + suffix, Jsonb(binding), worlds, row[3], 'active', row[4], use))
    unregistered = 'sim:' + str(uuid.uuid4())
    empty = create(db)['world']  # registered, no credential yet: only the principal rule can refuse there
    # Each refusal names its rule (review 5.2): a negative that fails for another reason does not pass.
    refusals = (
        ('exactly its one world', lambda: insert([a, b], '1', 'p1')),
        ('exactly its one world', lambda: insert([a, 'test'], '2', 'p2')),
        ('exactly its one world', lambda: insert(['real', a], '3', 'p3')),
        ('not registered or not active', lambda: insert([unregistered], '4', 'p4')),
        ('not registered or not active', lambda: insert([a], '5', 'p5', tenant=TENANT_B)),
        ('has one active credential per use', lambda: insert([a], '6', 'p6')),  # D10: a second api credential
        ('names its use', lambda: insert([a], '6b', 'p6b', use=None)),
        ('never carried into a simulation world', lambda: insert([empty], '7', TENANT_A + '-real-principal')),
        ('never holds a real or test credential', lambda: insert(['real'], '8', TENANT_A + '-sim-a-principal')),
        ('never holds a real or test credential', lambda: insert(['test'], '9', TENANT_A + '-sim-a-principal')),
    )
    for rule, attempt in refusals:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match=rule), db.transaction():
            attempt()
    # D10 positive control: the world takes its worker and agent credentials beside its api one (three uses, one each), each
    # under its own principal; a real credential carries no use.
    with db.transaction(force_rollback=True):
        insert([a], '11', TENANT_A + '-sim-a-worker', use='worker')
        insert([a], '12', TENANT_A + '-sim-a-agent', use='agent')
        assert db.execute("select array_agg(sim_use order by sim_use) from authz.nexloop_service_credentials where worlds=%s and status='active'", ([a],)).fetchone()[0] == ['agent', 'api', 'worker']
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='has one active credential per use'), db.transaction():
            insert([a], '13', TENANT_A + '-sim-a-worker-2', use='worker')
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='never changed'), db.transaction():
            db.execute("update authz.nexloop_service_credentials set sim_use='worker' where worlds=%s and sim_use='agent'", ([a],))
    with pytest.raises(psycopg.errors.CheckViolation, match='nexloop_service_credentials_sim_use'), db.transaction():
        insert(['real'], '14', TENANT_A + '-real-with-use', use='api')
    # A changed principal binding is checked like a new credential (review 2.2), in both directions.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='never carried into a simulation world'), db.transaction():
        db.execute("""update authz.nexloop_service_credentials set binding=binding||jsonb_build_object('principal_id',%s::text,'subject_principal_id',%s::text)
            where worlds=%s""", (TENANT_A + '-real-principal', TENANT_A + '-real-principal', [b]))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='never holds a real or test credential'), db.transaction():
        db.execute("""update authz.nexloop_service_credentials set binding=binding||jsonb_build_object('principal_id',%s::text,'subject_principal_id',%s::text)
            where worlds=array['real'] and credential_id=%s""", (TENANT_A + '-sim-a-principal', TENANT_A + '-sim-a-principal', row[1].replace('-sim-a', '-real')))
    # Positive control: an unchanged binding still updates (revocation of the real credential, then nothing else).
    assert db.execute("update authz.nexloop_service_credentials set expires_at=expires_at where worlds=%s", ([b],)).rowcount == 1
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
    assert sum(totals(db, [a]).values()) == 18  # 15 seeded rows, the world's clock with its creation event (S5) and its model budget (S6)
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
    assert db.execute('select runtime.nexloop_sim_world_snapshot(%s,%s,100000)', (TENANT_A, a)).fetchone()[0] is True
    passes = []
    while True:
        r = db.execute('select runtime.nexloop_sim_world_purge(%s,%s,4)', (TENANT_A, a)).fetchone()[0]
        passes.append(r)
        assert totals(db, ['real', 'test', b, other]) == before_kept  # after every pass
        if r['status'] == 'deleted':
            break
        assert r['deleted'] <= 4 and len(passes) < 20, passes
    assert len(passes) >= 4 and passes[-1]['remaining'] == 0 and sum(p['deleted'] for p in passes) == 18
    assert sum(totals(db, [a]).values()) == 0 and world_row(db, a)[1:] == ('deleted', True)
    # Idempotent: the Action and the pass on a deleted world change nothing.
    assert delete(db, a)['status'] == 'deleted'
    assert db.execute('select runtime.nexloop_sim_world_purge(%s,%s,4)', (TENANT_A, a)).fetchone()[0]['deleted'] == 0
    # The summary: counts per table and digests only, no row content.
    counts, digests, done = db.execute('select counts,digests,completed_at is not null from control.nexloop_sim_world_summaries where world=%s', (a,)).fetchone()
    assert counts == {'runtime.nexloop_read_audit_daily': 5, 'runtime.nexloop_semantic_accesses': 5, 'runtime.nexloop_work_feed': 5,
                      'control.nexloop_sim_clocks': 1, 'control.nexloop_sim_clock_events': 1, 'control.nexloop_budget_limits': 1} and done
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
    assert (result['status'], result['remaining'], result['deleted']) == ('deleted', 0, 9)
    # Only the deletion ledger gained the world's entry in real (review 1.8); no real row left.
    assert {t: n for t, n in totals(db, ['real']).items() if t != 'control.nexloop_deletion_ledger'} == {t: n for t, n in kept.items() if t != 'control.nexloop_deletion_ledger'}
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
    assert db.execute('select runtime.nexloop_sim_world_snapshot(%s,%s,100000)', (TENANT_A, c)).fetchone()[0] is True
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
    for bad in (lambda: ops['kind']('real'), lambda: ops['expires_in_days'](0), lambda: ops['expires_in_days'](366), lambda: ops['expires_in_days'](31),
                lambda: OPERATIONS['delete_sim_world']['world']('real'), lambda: OPERATIONS['delete_sim_world']['world']('sim:1')):
        with pytest.raises(ValueError):
            bad()
    assert OPERATIONS['delete_sim_world']['world']('sim:' + str(uuid.uuid4()))


# Review fixes ------------------------------------------------------------------------------------------------------------------

def sweep_summaries(admin, batch):
    return admin.execute("select runtime.nexloop_retention_sweep(%s,'real','sim_world_summary',%s,'sweep:t','service:retention_keeper')",
                         (TENANT_A, batch)).fetchone()[0]


def test_expired_summaries_leave_beyond_the_batch_of_deleted_worlds(db):
    """Review 4.1: the limit applies to expired summaries, not to registry rows that stay forever."""
    worlds = [create(db)['world'] for _ in range(3)]
    for w in worlds:
        assert delete(db, w)['status'] == 'deleted'
    db.execute("update control.nexloop_sim_world_summaries set completed_at=completed_at-interval '31 days' where world=any(%s)", (worlds,))
    assert sweep_summaries(db, 2)['processed'] == 2
    assert sweep_summaries(db, 2)['processed'] == 1  # the third, beyond the first batch of deleted worlds
    assert db.execute('select count(*) from control.nexloop_sim_world_summaries where world=any(%s)', (worlds,)).fetchone()[0] == 0


def test_two_concurrent_inserts_give_one_active_credential_per_world(db, pg):
    """Review 2.1 (D10: per world and use): the trigger cannot see an uncommitted credential; the partial unique index on (world, use)
    makes the second insert of the same use wait and fail."""
    a = create(db)['world']
    seed_authority(db, TENANT_A, 'eios:artifact:' + 'd' * 32, world=a, identity_suffix='-sim-c')
    row = db.execute('select tenant_id,binding,worlds,audience,expires_at from authz.nexloop_service_credentials where worlds=%s', ([a],)).fetchone()
    db.execute("update authz.nexloop_service_credentials set status='revoked' where worlds=%s", ([a],))

    def insert(conn, suffix):
        conn.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at,sim_use) values(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                     (uuid.uuid4().hex * 2, row[0], 'c' + suffix, Jsonb({**row[1], 'credential_id': 'c' + suffix}), row[2], row[3], 'active', row[4], 'worker'))
    first, second = psycopg.connect(pg), psycopg.connect(pg)
    try:
        insert(first, '1')  # uncommitted
        outcome = {}

        def race():
            try:
                insert(second, '2'); second.commit(); outcome['ok'] = True
            except psycopg.Error as e:
                second.rollback(); outcome['error'] = e
        thread = __import__('threading').Thread(target=race)
        thread.start()
        thread.join(1.0)
        assert thread.is_alive()  # waits on the first, uncommitted insert
        first.commit()
        thread.join(10)
        assert isinstance(outcome.get('error'), psycopg.errors.UniqueViolation) and 'ok' not in outcome
    finally:
        first.close(); second.close()
    assert db.execute("select count(*) from authz.nexloop_service_credentials where worlds=%s and status='active'", ([a],)).fetchone()[0] == 1


def test_deleting_the_world_refuses_its_run_credential(db):
    """Review 2.4: a Run credential of the world authenticates before the deletion and is refused after it."""
    a = create(db)['world']
    token, _ = seed_authority(db, TENANT_A, 'eios:artifact:' + 'e' * 32, world=a, identity_suffix='-sim-run')
    source = hashlib.sha256(token.encode()).hexdigest()
    run_token = uuid.uuid4().hex
    db.execute("""insert into authz.nexloop_run_credentials(token_digest,run_id,source_digest,source_directory_hash,world,audience,allowed_resources,status,issued_at,expires_at)
        values(%s,%s,%s,(select authz.nexloop_root_identity_snapshot(%s,%s)->>'directory_hash'),%s,'nexloop-agent-host',%s,'active',clock_timestamp(),
        clock_timestamp()+interval '200 seconds')""",
               (hashlib.sha256(run_token.encode()).hexdigest(), uuid.uuid4(), source, source, a, a, ['eios:artifact:' + 'e' * 32]))
    assert identity(db, run_token, a)['binding']['tenant_id'] == TENANT_A  # positive control
    assert delete(db, a)['status'] == 'deleted'
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='Run authentication denied'), db.transaction():
        identity(db, run_token, a)
    assert denied(db, token, a)


def test_every_foreign_key_into_a_world_table_is_covered(db):
    """Review 1.1 guard, recursive: a table without a world column whose foreign key points at a table with one, or at a declared
    dependent (a grandchild, any depth), must be declared; so must every table without a world column holding a run id (a Run
    belongs to a world), unless declared not_applicable with its reason. A new table that is not declared fails here."""
    found = set(db.execute("""with recursive reach(t) as (
            select t from runtime.nexloop_sim_world_tables() t
            union select c.conrelid::regclass from pg_constraint c join reach r on c.confrelid=r.t
             where c.contype='f' and c.conrelid<>c.confrelid
             and not exists(select 1 from pg_attribute a where a.attrelid=c.conrelid and a.attname='world' and not a.attisdropped))
        select c.conrelid::regclass::text,c.conname from pg_constraint c where c.contype='f' and c.conrelid<>c.confrelid and c.confrelid in (select t from reach)
         and not exists(select 1 from pg_attribute a where a.attrelid=c.conrelid and a.attname='world' and not a.attisdropped)""").fetchall())
    declared = set(db.execute('select child::text,constraint_name from control.nexloop_sim_world_dependents').fetchall())
    assert found == declared and len(declared) == 9
    run_holders = {r[0] for r in db.execute("""select distinct r.oid::regclass::text from pg_class r join pg_namespace n on n.oid=r.relnamespace
        join pg_attribute a on a.attrelid=r.oid and not a.attisdropped and a.attname ~ '(^|_)run_id$'
        where r.relkind in ('r','p') and not r.relispartition and n.nspname in ('authz','control','ontology','runtime')
        and not exists(select 1 from pg_attribute w where w.attrelid=r.oid and w.attname='world' and not w.attisdropped)""").fetchall()}
    assert run_holders and run_holders <= {c for c, _ in declared}, run_holders - {c for c, _ in declared}
    assert db.execute("select count(*) from control.nexloop_sim_world_dependents where mode='not_applicable' and reason=''").fetchone()[0] == 0
    # Every declared delete target has a condition, ordered before the world tables.
    targets = db.execute('select target::text,ord from runtime.nexloop_sim_world_targets()').fetchall()
    deps = [t for t, o in targets if o < 100000]
    assert set(deps) == {c for c, _ in declared if c != 'ontology.nexloop_recall_embeddings'} and [o for _, o in targets] == sorted(o for _, o in targets)
    # Dependency order (review N1): every world table comes after each world table whose foreign key references it.
    pos = {t: i for i, (t, _) in enumerate(targets)}
    edges = db.execute("""select distinct c.conrelid::regclass::text,c.confrelid::regclass::text from pg_constraint c where c.contype='f'
        and c.conrelid<>c.confrelid and c.conrelid in (select * from runtime.nexloop_sim_world_tables()) and c.confrelid in (select * from runtime.nexloop_sim_world_tables())""").fetchall()
    cyclic = {(a, b) for a, b in edges if (b, a) in {(x, y) for x, y in edges}}
    late = [(child, parent) for child, parent in edges if (child, parent) not in cyclic and pos[child] > pos[parent]]
    assert edges and late == [], late


def test_append_only_tables_the_erasure_scope_cannot_name_hold_no_simulation_rows(db):
    """Review N2: the 0129 erasure scope names only ^[a-z_]+\\.[a-z_]+$; a target whose name it cannot hold and that refuses deletes
    with a trigger (evo_l1_definitions, evo_l1_sessions) would block a world forever, so it must refuse simulation rows (a real-only
    world CHECK). A new such table without that CHECK fails here."""
    rows = db.execute("""select t.target::text,
        exists(select 1 from pg_trigger g where g.tgrelid=t.target and not g.tgisinternal and (g.tgtype & 8)<>0),
        exists(select 1 from pg_constraint c where c.conrelid=t.target and c.contype='c' and pg_get_constraintdef(c.oid) like '%world = ''real''%')
        from runtime.nexloop_sim_world_targets() t where t.target::text !~ '^[a-z_]+\\.[a-z_]+$'""").fetchall()
    guarded_by_trigger = {t for t, deletes_refused, _ in rows if deletes_refused}
    assert {'runtime.nexloop_evo_l1_definitions'} <= guarded_by_trigger
    assert [t for t, deletes_refused, real_only in rows if deletes_refused and not real_only] == []


def seed_event_rows(admin, tenant, world, tag):
    """An object and its event: ontology.events has no world column and belongs to the world through its object."""
    oid = tag + '-' + uuid.uuid4().hex
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
            values(%s,%s,'Note',%s,1,'{}','test','synthetic',now(),now())''', (tenant, world, oid))
        admin.execute("""insert into ontology.events(event_id,tenant_id,event_type,object_type,object_id,sequence,created_at)
            values(%s,%s,'noted','Note',%s,1,now())""", (oid, tenant, oid))
    return oid


def test_dependent_rows_leave_with_the_world_and_real_ones_stay(db):
    a = create(db)['world']
    gone, kept = seed_event_rows(db, TENANT_A, a, 'gone'), seed_event_rows(db, TENANT_A, 'real', 'keep')
    r = delete(db, a)
    assert r['status'] == 'deleted' and r['blocked'] == {}, r
    assert db.execute('select count(*) from ontology.events where event_id=%s', (gone,)).fetchone()[0] == 0
    assert db.execute('select count(*) from ontology.events where event_id=%s', (kept,)).fetchone()[0] == 1
    counts = db.execute('select counts from control.nexloop_sim_world_summaries where world=%s', (a,)).fetchone()[0]
    assert counts['ontology.events'] == 1 and counts['ontology.objects'] == 1


def test_a_failing_table_is_reported_and_the_deletion_continues_after_the_fix(db):
    """Review 1.4 / 5.1: a table nexloop_owner cannot read or delete (a real failure inside the pass) is reported blocked with its
    sqlstate, the other tables are still deleted and the world stays deleting; after the fix the next pass finishes."""
    a = create(db)['world']
    seed_rows(db, TENANT_A, a, 2, 'gone')
    seed_rows(db, TENANT_A, 'real', 2, 'keep')
    kept = totals(db, ['real'])
    db.execute('create table runtime.zz_nx041_blocking(tenant_id text,world text)')  # owned by the test superuser, no grant
    db.execute('insert into runtime.zz_nx041_blocking values(%s,%s),(%s,%s)', (TENANT_A, a, TENANT_A, 'real'))
    r = delete(db, a)
    assert r['status'] == 'deleting' and r['blocked'] == {'runtime.zz_nx041_blocking': '42501'} and r['remaining'] == 1, r
    assert sum(n for t, n in totals(db, [a]).items() if t != 'runtime.zz_nx041_blocking') == 0  # everything else left
    digests = db.execute('select digests from control.nexloop_sim_world_summaries where world=%s', (a,)).fetchone()[0]
    assert digests['runtime.zz_nx041_blocking'] == 'unreadable:42501'
    again = db.execute('select runtime.nexloop_sim_world_purge(%s,%s,100)', (TENANT_A, a)).fetchone()[0]
    assert again['status'] == 'deleting' and again['blocked'] == {'runtime.zz_nx041_blocking': '42501'}
    db.execute('grant select,delete on runtime.zz_nx041_blocking to nexloop_owner')  # the fix
    done = db.execute('select runtime.nexloop_sim_world_purge(%s,%s,100)', (TENANT_A, a)).fetchone()[0]
    assert (done['status'], done['remaining'], done['blocked']) == ('deleted', 0, {}) and world_row(db, a)[1] == 'deleted'
    assert db.execute('select world from runtime.zz_nx041_blocking').fetchall() == [('real',)]
    after = totals(db, ['real'])
    assert {t: after[t] for t in kept if t != 'control.nexloop_deletion_ledger'} == {t: n for t, n in kept.items() if t != 'control.nexloop_deletion_ledger'}
    db.execute('drop table runtime.zz_nx041_blocking')


def test_the_snapshot_is_taken_in_bounded_steps(db):
    """Review 1.5: with a budget of one row each call stops after the first non-empty table; the summary appears at the end."""
    a = create(db)['world']
    seed_rows(db, TENANT_A, a, 2, 'gone')
    db.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    db.execute("update control.nexloop_sim_worlds set status='deleting' where world=%s", (a,))
    steps = 0
    while not db.execute('select runtime.nexloop_sim_world_snapshot(%s,%s,1)', (TENANT_A, a)).fetchone()[0]:
        steps += 1
        assert steps < 10
    # Six non-empty tables (the three seeded, S5's clock and its creation event, S6's model budget), one per call. The summary is
    # written by the call that reads the last non-empty table when no target follows it, else by one more call over the empty rest
    # (the dependency order of review N1 decides which).
    assert steps in (5, 6)
    assert db.execute('select counts from control.nexloop_sim_world_summaries where world=%s', (a,)).fetchone()[0] == {
        'runtime.nexloop_read_audit_daily': 2, 'runtime.nexloop_semantic_accesses': 2, 'runtime.nexloop_work_feed': 2,
        'control.nexloop_sim_clocks': 1, 'control.nexloop_sim_clock_events': 1, 'control.nexloop_budget_limits': 1}
    assert db.execute('select count(*) from control.nexloop_sim_world_snapshot_parts where world=%s', (a,)).fetchone()[0] == 0


def test_a_restore_from_before_the_deletion_replays_it(db):
    """Review 1.8: the deletion enters the deletion ledger; after a restore that brought the world back (registry active, credential
    active, rows present), replaying the ledger sets it deleting and revokes the credential; the keeper finishes the deletion."""
    a = create(db)['world']
    token, _ = seed_authority(db, TENANT_A, 'eios:artifact:' + '9' * 32, world=a, identity_suffix='-sim-restore')
    seed_rows(db, TENANT_A, a, 2, 'gone')
    start = db.execute('select coalesce(max(seq),0) from control.nexloop_deletion_ledger').fetchone()[0]
    delete(db, a)
    entries = db.execute('select runtime.nexloop_deletion_ledger_since(%s,1000)', (start,)).fetchone()[0]
    mine = [e for e in entries if e['kind'] == 'sim_world']
    assert [(e['tenant_id'], e['world'], e['subject_ref']) for e in mine] == [(TENANT_A, 'real', a)]
    # The restored database as it was before the deletion (written as the restore does, triggers off).
    with db.transaction():
        db.execute('set local session_replication_role=replica')
        db.execute("update control.nexloop_sim_worlds set status='active',deleted_at=null where world=%s", (a,))
        db.execute("update authz.nexloop_service_credentials set status='active' where worlds=%s", ([a],))
        db.execute('delete from control.nexloop_sim_world_summaries where world=%s', (a,))
    seed_rows(db, TENANT_A, a, 2, 'back')
    assert identity(db, token, a) is not None
    db.execute("select runtime.nexloop_replay_begin('restore-t1',%s)", (start,))
    out = db.execute("select runtime.nexloop_replay_deletions('restore-t1',%s)", (Jsonb(mine),)).fetchone()[0]
    assert out['sim_worlds'] == 1 and world_row(db, a)[1] == 'deleting' and denied(db, token, a)
    # Replaying again is already applied.
    assert db.execute("select runtime.nexloop_replay_deletions('restore-t1',%s)", (Jsonb(mine),)).fetchone()[0]['already_applied'] == 1
    assert sweep_summaries(db, 100)['counts']['worlds_resumed'] == 1 and world_row(db, a)[1] == 'deleted'
    assert sum(totals(db, [a]).values()) == 0


def test_row_security_never_hides_a_tenants_rows_from_the_deletion(db):
    """Review 1.2 guard: the purge reads and deletes as nexloop_owner with row security on and eios.tenant_id set. Policies are
    permissive (ored), so a target is fully visible when no policy is restrictive and every RLS target has one tenant (or open)
    policy for nexloop_owner. A restrictive or principal-filtered policy added later fails here."""
    rows = db.execute("""select c.oid::regclass::text,c.relrowsecurity,
        coalesce(array_agg(p.polpermissive) filter (where p.polname is not null),'{}'),
        coalesce(array_agg(pg_get_expr(p.polqual,p.polrelid)) filter (where p.polname is not null and (p.polroles='{0}' or 'nexloop_owner'::regrole=any(p.polroles))),'{}')
        from runtime.nexloop_sim_world_targets() t join pg_class c on c.oid=t.target left join pg_policy p on p.polrelid=c.oid group by 1,2""").fetchall()
    tenant = __import__('re').compile(r"tenant_id = current_setting\('eios\.tenant_id'::text, true\)")
    assert rows and all(all(perm) for _, _, perm, _ in rows), [r for r in rows if not all(r[2])]
    hidden = [t for t, rls, _, quals in rows if rls and not any(q == 'true' or tenant.search(q or '') for q in quals)]
    assert hidden == []


def test_deleting_a_world_revokes_its_three_credentials(db):
    """D10: the world's api, worker and agent credentials all stop at its deletion; another world's are untouched."""
    a, b = create(db)['world'], create(db)['world']
    tokens = {use: seed_authority(db, TENANT_A, 'eios:artifact:' + c * 32, world=a, identity_suffix=f'-d10-{use}', sim_use=use)[0]
              for use, c in (('api', '1'), ('worker', '2'), ('agent', '3'))}
    other, _ = seed_authority(db, TENANT_A, 'eios:artifact:' + '4' * 32, world=b, identity_suffix='-d10-b', sim_use='worker')
    assert all(identity(db, t, a)['binding']['tenant_id'] == TENANT_A for t in tokens.values())  # positive control: all three active
    assert delete(db, a)['credentials_revoked'] == 3
    assert all(denied(db, t, a) for t in tokens.values()) and identity(db, other, b)['binding']['tenant_id'] == TENANT_A
