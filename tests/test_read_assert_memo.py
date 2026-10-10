"""O5b level 1 (0095): statement- and snapshot-scoped read-authority assertion memo.

A repeated identical assertion inside one statement is answered without the full check
only while the visible snapshot is unchanged, no new advisory lock was taken, there was
no own write to an authority input, and every clock condition still holds. Synthetic
disposable PG only; the helper loop function is test-owned and lives in public.
"""
import hashlib
import json
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.object_reads import AuthorizedObjectReader
from authority_fixture import replace_fact, seed_authority
from test_action_definitions import published_action  # noqa: F401

INNER = "authz.nexloop_assert_read_authority_before_read_memo_v0095(text,text,jsonb)"
LOOP = """
create or replace function public.o5b_loop(p_digest text,p_world text,p_claims jsonb,p_n int,p_sleep float8,p_between text)
returns jsonb language plpgsql as $$
declare i int;out jsonb:='[]';ok boolean;err text;t0 timestamptz;m0 bigint;s0 text;rows bigint:=0;
begin
 m0:=coalesce(pg_stat_get_xact_function_calls('{inner}'::regprocedure),0);
 for i in 1..p_n loop
  if i>1 and p_between is not null then s0:=pg_current_snapshot()::text;execute p_between;else s0:=null;end if;
  t0:=clock_timestamp();
  begin perform authz.nexloop_assert_read_authority(p_digest,p_world,p_claims);ok:=true;err:=null;
  exception when others then ok:=false;err:=sqlstate||' '||sqlerrm;end;
  out:=out||jsonb_build_object('t0',t0,'ok',ok,'err',err,'before_between',s0,'after_between',pg_current_snapshot()::text,
   'misses',coalesce(pg_stat_get_xact_function_calls('{inner}'::regprocedure),0)-m0);
  if p_sleep>0 then perform pg_sleep(p_sleep);end if;
 end loop;
 -- Function statistics count successful full checks only (a denial exits with an error).
 return jsonb_build_object('start',statement_timestamp(),'results',out,
  'misses',coalesce(pg_stat_get_xact_function_calls('{inner}'::regprocedure),0)-m0,
  'memo_rows',public.o5b_memo_rows());
end $$;
create or replace function public.o5b_memo_rows() returns bigint language plpgsql as $$
declare n bigint:=0;
begin
 if to_regclass('pg_temp.nexloop_read_memo') is not null then execute 'select count(*) from pg_temp.nexloop_read_memo' into n;end if;
 return n;
end $$;
create table if not exists public.o5b_gate(id int primary key);
insert into public.o5b_gate values(1) on conflict do nothing;
""".replace('{inner}', INNER)


@pytest.fixture
def memo(published_action, admin, pg):
    reader, _, _ = published_action
    object_id = hashlib.sha256(b'o5b-consumer').hexdigest()
    target = 'eios:object:Consumer/' + object_id
    token, _ = seed_authority(admin, 'synthetic-a', target, operation=Operation.READ, resource_type=ResourceType.OBJECT, identity_suffix='-o5b')
    session = authenticate_service(reader.pool, token, world='real')
    projector = AuthorizedObjectReader(reader.pool, session, reader.signer)
    admin.execute(LOOP); admin.execute("set track_functions='pl'")
    # As in the application, the tenant context is already set when assertions run; the
    # pre-call tenant is part of the key (see test_tenant_context_is_part_of_the_key).
    admin.execute("select set_config('eios.tenant_id','synthetic-a',false)")
    state = {'reader': reader, 'admin': admin, 'pg': pg, 'token': token, 'session': session, 'target': target, 'object_id': object_id}
    def claims(expires_in=None, session=None):
        s = session or state['session']
        value = AuthorizedObjectReader(reader.pool, s, reader.signer)._configured(ResourceType.OBJECT, 'Consumer/' + object_id, Operation.READ)
        if expires_in is not None:
            value['expires_at'] = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
        return value
    def loop(value, n=5, sleep=0.0, between=None, connection=None, session=None):
        c = connection or admin
        with c.transaction():
            return c.execute('select public.o5b_loop(%s,%s,%s,%s,%s,%s)',
                             ((session or state['session']).token_digest, 'real', Jsonb(value), n, sleep, between)).fetchone()[0]
    def reauthenticate():
        state['session'] = authenticate_service(reader.pool, token, world='real'); return state['session']
    state.update(claims=claims, loop=loop, reauthenticate=reauthenticate, projector=projector)
    return state


def grant_key(m):
    return [m['session'].authentication.subject_principal_id, m['target']]


def test_repeated_assertion_in_one_statement_runs_one_full_check(memo):
    result = memo['loop'](memo['claims']())
    assert [r['ok'] for r in result['results']] == [True] * 5 and result['misses'] == 1
    # Same binding as the full path; the switch off gives identical results with no memo.
    memo['admin'].execute('update authz.nexloop_read_memo_settings set enabled=false')
    off = memo['loop'](memo['claims']())
    assert [r['ok'] for r in off['results']] == [True] * 5 and off['misses'] == 5
    with memo['admin'].transaction():
        on_binding = memo['admin'].execute('select authz.nexloop_assert_read_authority(%s,%s,%s)', (memo['session'].token_digest, 'real', Jsonb(memo['claims']()))).fetchone()[0]
    memo['admin'].execute('update authz.nexloop_read_memo_settings set enabled=true')
    with memo['admin'].transaction():
        value = memo['claims']()
        first = memo['admin'].execute('select authz.nexloop_assert_read_authority(%s,%s,%s)', (memo['session'].token_digest, 'real', Jsonb(value))).fetchone()[0]
    assert first == on_binding


def test_tenant_context_is_part_of_the_key(memo):
    memo['admin'].execute("select set_config('eios.tenant_id','',false)")
    result = memo['loop'](memo['claims'](), n=3)
    # The first call runs with an empty tenant context and sets it: the second call has a
    # different key (full check), the third hits.
    assert [r['misses'] for r in result['results']] == [1, 2, 2]


def test_session_switch_can_only_disable(memo):
    with memo['admin'].transaction():
        memo['admin'].execute("set local nexloop.read_memo='off'")
        result = memo['admin'].execute('select public.o5b_loop(%s,%s,%s,3,0,null)', (memo['session'].token_digest, 'real', Jsonb(memo['claims']()))).fetchone()[0]
    assert result['misses'] == 3
    memo['admin'].execute('update authz.nexloop_read_memo_settings set enabled=false')
    with memo['admin'].transaction():
        memo['admin'].execute("set local nexloop.read_memo='on'")
        result = memo['admin'].execute('select public.o5b_loop(%s,%s,%s,3,0,null)', (memo['session'].token_digest, 'real', Jsonb(memo['claims']()))).fetchone()[0]
    assert result['misses'] == 3


def test_separate_statements_never_share(memo):
    value = memo['claims']()
    with memo['admin'].transaction():
        for _ in range(3):
            memo['admin'].execute('select authz.nexloop_assert_read_authority(%s,%s,%s)', (memo['session'].token_digest, 'real', Jsonb(value)))
        misses = memo['admin'].execute(f"select pg_stat_get_xact_function_calls('{INNER}'::regprocedure)").fetchone()[0]
    assert misses == 3


def test_own_write_invalidates_and_revocation_is_seen_in_the_same_statement(memo):
    value = memo['claims']()
    grant = memo['admin'].execute("select payload from authz.nexloop_authority_facts where fact_kind='grants' and entity_key=%s", (grant_key(memo),)).fetchone()[0]
    revoked = dict(grant); revoked['grants'] = []
    # An own no-op write (statement trigger) empties the memo: every following call is a full check.
    noop = "update authz.nexloop_authority_facts set payload=payload where false"
    assert memo['loop'](value, n=3, between=noop)['misses'] == 3
    revoke = "update authz.nexloop_authority_facts set payload='%s'::jsonb where fact_kind='grants' and entity_key=array['%s','%s']" % (
        json.dumps(revoked), *grant_key(memo))
    result = memo['loop'](value, n=3, between=revoke)  # own revocation inside the same statement
    assert [r['ok'] for r in result['results']] == [True, False, False], result


def test_claims_expiry_inside_the_statement_is_rechecked_on_every_hit(memo):
    value = memo['claims'](expires_in=0.6)
    result = memo['loop'](value, n=8, sleep=0.15)
    deadline = datetime.fromisoformat(value['expires_at'])
    oks = [(datetime.fromisoformat(r['t0']), r['ok']) for r in result['results']]
    assert any(ok for _, ok in oks) and not oks[-1][1]
    assert all(not ok for t0, ok in oks if t0 >= deadline), oks
    assert sum(ok for _, ok in oks) > result['misses']  # hits happened before the deadline


def test_credential_expiry_inside_the_statement_is_rechecked_on_every_hit(memo):
    memo['admin'].execute("update authz.nexloop_service_credentials set expires_at=clock_timestamp()+interval '0.7 seconds' where token_digest=%s", (memo['session'].token_digest,))
    session = memo['reauthenticate'](); value = memo['claims'](session=session)
    expiry = memo['admin'].execute('select expires_at from authz.nexloop_service_credentials where token_digest=%s', (session.token_digest,)).fetchone()[0]
    result = memo['loop'](value, n=8, sleep=0.15)
    oks = [(datetime.fromisoformat(r['t0']), r['ok']) for r in result['results']]
    assert any(ok for _, ok in oks) and all(not ok for t0, ok in oks if t0 >= expiry), oks
    assert sum(ok for _, ok in oks) > result['misses']


def test_advisory_lock_is_a_barrier(memo):
    value = memo['claims']()
    # Each iteration takes a new advisory lock: every following call is a full check.
    assert memo['loop'](value, n=3, between='select pg_advisory_xact_lock((random()*1e12)::bigint)')['misses'] == 3
    # Re-taking a lock already held can never wait: not a barrier.
    assert memo['loop'](value, n=3, between='select pg_advisory_xact_lock(4242)')['misses'] == 2
    assert memo['loop'](value, n=3, between='select 1')['misses'] == 1


def hold(pg, sql, seconds, finish='commit'):
    ready = threading.Event()
    def run():
        with psycopg.connect(pg) as c:
            c.execute(sql); ready.set(); time.sleep(seconds); c.execute(finish)
    thread = threading.Thread(target=run); thread.start(); assert ready.wait(10)
    return thread


def test_advisory_wait_without_xid_is_a_barrier_and_the_final_deadline_holds(memo):
    """The holder takes only an advisory lock (no xid): its release changes no snapshot,
    so only the advisory barrier makes the call after the wait a full check."""
    value = memo['claims']()
    thread = hold(memo['pg'], 'begin; select pg_advisory_xact_lock(777)', 0.6)
    result = memo['loop'](value, n=2, between='select pg_advisory_xact_lock(777)')
    thread.join(10)
    first, second = result['results']
    assert first['ok'] and second['ok'] and second['before_between'] == second['after_between']  # no snapshot change
    assert result['misses'] == 2  # still a full check after the wait
    # The claims expire during the wait: the assertion after the lock denies.
    value = memo['claims'](expires_in=1.0)
    thread = hold(memo['pg'], 'begin; select pg_advisory_xact_lock(778)', 1.4)
    result = memo['loop'](value, n=2, between='select pg_advisory_xact_lock(778)')
    thread.join(10)
    assert result['results'][0]['ok'] and not result['results'][1]['ok'], result


@pytest.mark.parametrize('finish', ['commit', 'rollback'])
def test_row_lock_wait_changes_the_snapshot_and_reruns(memo, finish):
    value = memo['claims']()
    thread = hold(memo['pg'], 'begin; select 1 from public.o5b_gate where id=1 for update', 0.6, finish)
    waited = memo['loop'](value, n=2, between='select 1 from public.o5b_gate where id=1 for update')
    thread.join(10)
    second = waited['results'][1]
    assert second['before_between'] != second['after_between'] and waited['misses'] == 2, waited
    # Control: the same lock taken without waiting leaves the snapshot alone and may hit.
    free = memo['loop'](value, n=2, between='select 1 from public.o5b_gate where id=1 for update')
    assert free['results'][1]['before_between'] == free['results'][1]['after_between'] and free['misses'] == 1, free


def test_snapshot_taken_before_a_lock_wait_is_never_a_hit_basis(memo):
    """(b) The key's snapshot is read at the call, after every earlier lock. A full check
    that itself waited (holder commits during it) stores the pre-wait snapshot, so the very
    next identical call misses; only the call after that may hit."""
    value = memo['claims']()
    lock = "begin; select 1 from authz.nexloop_authority_facts where fact_kind='grants' and entity_key=array['%s','%s'] for update" % tuple(grant_key(memo))
    thread = hold(memo['pg'], lock, 0.8)
    result = memo['loop'](value, n=3)
    thread.join(10)
    assert [r['ok'] for r in result['results']] == [True] * 3
    assert [r['misses'] for r in result['results']] == [1, 2, 2], result


def test_subtransaction_rollback_removes_entries(memo):
    value = memo['claims']()
    from psycopg import sql
    rows = lambda: memo['admin'].execute('select public.o5b_memo_rows()').fetchone()[0]
    memo['loop'](value, n=1)  # the owner's memo table now exists in this session (empty after commit)
    with memo['admin'].transaction():
        memo['admin'].execute(sql.SQL("""do $$begin
          begin perform authz.nexloop_assert_read_authority({},'real',{}::jsonb);raise exception 'rollback subtransaction';
          exception when raise_exception then null;end;end$$""").format(sql.Literal(memo['session'].token_digest), sql.Literal(json.dumps(value))))
        assert rows() == 0  # the entry written inside the rolled-back subtransaction is gone
        memo['admin'].execute('select authz.nexloop_assert_read_authority(%s,%s,%s)', (memo['session'].token_digest, 'real', Jsonb(value)))
        assert rows() == 1
    assert rows() == 0  # on commit delete rows


def test_read_only_transaction_runs_full_checks_without_error(memo):
    with psycopg.connect(memo['pg'], autocommit=True) as fresh:  # no memo table yet in this session
        fresh.execute("set track_functions='pl'")
        with fresh.transaction():
            fresh.execute('set transaction read only')
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):  # the full check's FOR SHARE, never the memo
                fresh.execute('select authz.nexloop_assert_read_authority(%s,%s,%s)', (memo['session'].token_digest, 'real', Jsonb(memo['claims']())))
        assert fresh.execute("select to_regclass('pg_temp.nexloop_read_memo')").fetchone() == (None,)


def test_isolation_between_digests_worlds_and_tenants(memo):
    other_token, _ = seed_authority(memo['admin'], 'synthetic-a', memo['target'], operation=Operation.READ, resource_type=ResourceType.OBJECT, identity_suffix='-o5b-other')
    other = authenticate_service(memo['reader'].pool, other_token, world='real')
    memo['reauthenticate'](); value = memo['claims'](); other_value = memo['claims'](session=other)
    with memo['admin'].transaction():
        for digest, claims in ((memo['session'].token_digest, value), (other.token_digest, other_value), (other.token_digest, value), (memo['session'].token_digest, other_value)):
            try:
                with memo['admin'].transaction():  # savepoint per call
                    memo['admin'].execute('select authz.nexloop_assert_read_authority(%s,%s,%s)', (digest, 'real', Jsonb(claims)))
            except psycopg.Error:pass
        # Cross pairs are denied by the full check; nothing allowed was ever served across digests.
        assert memo['admin'].execute('select public.o5b_memo_rows()').fetchone() == (2,)
    with memo['admin'].transaction():
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            memo['admin'].execute('select authz.nexloop_assert_read_authority(%s,%s,%s)', (other.token_digest, 'real', Jsonb(value)))


def test_denials_are_never_memoized(memo):
    value = memo['claims'](); value['directory_hash'] = 'f' * 64
    result = memo['loop'](value, n=3)
    assert [r['ok'] for r in result['results']] == [False] * 3 and result['memo_rows'] == 0


# ---- Forgery: the session role tries to feed or shadow the memo ----------------------

def api_read(memo, connection, *, revoke=False):
    """A governed object read on a raw nexloop_api connection (read_object asserts the
    object claims twice in one statement: a memo hit when the memo is usable)."""
    from nexloop_eios.postgres_artifacts import canonical_payload
    import hmac
    reader = memo['reader']; object_id = memo['object_id']
    claims = memo['projector'].authorities([(ResourceType.OBJECT, 'Consumer/' + object_id)])[0]
    claims.update(protocol='nexloop-object-read-v1', key_id=reader.signer.key_id, type_name='Consumer', object_id=object_id, fields=(), property_authorities=[])
    text = canonical_payload(claims); signature = hmac.new(reader.signer.material, ('nexloop-object-read-v1:' + text).encode(), 'sha256').hexdigest()
    if revoke:
        replace_fact(memo['admin'], 'synthetic-a', 'grants', grant_key(memo), F.GrantFacts, grants=[])
    return connection.execute('select authz.nexloop_read_object(%s,%s,%s,%s)', (memo['session'].token_digest, 'real', text, signature)).fetchone()[0]


@pytest.fixture
def consumer(memo):
    from nexloop_eios.object_actions import GovernedObjectCreator
    reader = memo['reader']
    # The creator's session predates later identity seeding (tenant revision moved): re-key it.
    token = secrets.token_urlsafe(48)
    memo['admin'].execute('update authz.nexloop_service_credentials set token_digest=%s where token_digest=%s', (hashlib.sha256(token.encode()).hexdigest(), reader.session.token_digest))
    creator = authenticate_service(reader.pool, token, world='real')
    receipt = GovernedObjectCreator(reader.pool, creator, reader.signer).create(action_name='Consumer.create', action_version=1,
        intent_id='o5b-forgery-' + secrets.token_hex(4), type_name='Consumer', properties={})
    memo['object_id'] = receipt['object_id']; memo['target'] = 'eios:object:Consumer/' + receipt['object_id']
    token, _ = seed_authority(memo['admin'], 'synthetic-a', memo['target'], operation=Operation.READ, resource_type=ResourceType.OBJECT, identity_suffix='-o5b-forge')
    memo['session'] = authenticate_service(reader.pool, token, world='real')
    memo['projector'] = AuthorizedObjectReader(reader.pool, memo['session'], reader.signer)
    return memo


def temp_allowed(connection):
    """After the TEMPORARY tightening the session role cannot create temporary objects at
    all, so no forgery can even be set up; the owner-only checks stay as defense in depth."""
    allowed = connection.execute("select has_database_privilege(current_user,current_database(),'TEMPORARY')").fetchone()[0]
    if not allowed:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute('create temp table forgery_attempt(x int)')
    return allowed


CANARY = """
create temp table canary_log(n int);
create function pg_temp.canary() returns boolean language sql as 'insert into pg_temp.canary_log values(1) returning true';
"""


def test_forged_precreated_memo_table_disables_the_memo(consumer):
    m = consumer
    with psycopg.connect(make_conninfo(m['pg'], user='nexloop_api'), autocommit=True) as api:
        if not temp_allowed(api):
            assert api_read(m, api)['object_id'] == m['object_id']
            return
        api.execute(CANARY)
        # A forged "memo" that would allow anything, and logs any read of it.
        api.execute("create temp view nexloop_read_memo as select k.key,null::text tenant,'{}'::jsonb binding,0::bigint advisory_locks "
                    "from (select md5(random()::text) key) k where pg_temp.canary()")
        assert api_read(m, api)['object_id'] == m['object_id']  # memo disabled, full checks still allow
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api_read(m, api, revoke=True)
        assert api.execute('select count(*) from pg_temp.canary_log').fetchone() == (0,)


def test_search_path_and_shadow_catalog_objects_do_not_reach_the_memo(consumer):
    m = consumer
    with psycopg.connect(make_conninfo(m['pg'], user='nexloop_api'), autocommit=True) as api:
        if not temp_allowed(api):
            assert api_read(m, api)['object_id'] == m['object_id']
            return
        api.execute(CANARY)
        # Shadows that would hide new advisory locks or fake table ownership if any reference were unqualified.
        api.execute("create temp view pg_locks as select * from pg_catalog.pg_locks where pg_temp.canary()")
        api.execute("create temp view pg_class as select * from pg_catalog.pg_class where pg_temp.canary()")
        api.execute("set search_path=pg_temp,public")
        assert api_read(m, api)['object_id'] == m['object_id']
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api_read(m, api, revoke=True)
        assert api.execute('select count(*) from pg_temp.canary_log').fetchone() == (0,)


def test_session_role_cannot_write_entries_even_inside_a_subtransaction(consumer):
    m = consumer
    with psycopg.connect(make_conninfo(m['pg'], user='nexloop_api'), autocommit=True) as api:
        assert api_read(m, api)['object_id'] == m['object_id']  # creates the owner's memo table in this session
        with api.transaction():
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with api.transaction():  # savepoint
                    api.execute("insert into pg_temp.nexloop_read_memo values('forged',null,'{}'::jsonb,0)")
            for statement in ('select * from pg_temp.nexloop_read_memo', 'delete from pg_temp.nexloop_read_memo',
                              'drop table pg_temp.nexloop_read_memo', 'alter table pg_temp.nexloop_read_memo owner to nexloop_api'):
                with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.WrongObjectType)):
                    with api.transaction():api.execute(statement)
            with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedFunction)):
                with api.transaction():api.execute("select authz.nexloop_read_memo_ready()")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api_read(m, api, revoke=True)


# ---- (a) Concurrent revocation / expiry pressure --------------------------------------

def test_concurrent_revocation_never_serves_a_stale_allow(memo):
    """Two workers revoke/restore the grant and the credential (>=200 revocations in all)
    while four asserters run multi-assertion statements, reusing each resolved claim set
    for several statements and half of them with 30 ms claims that expire mid-statement. Any statement that starts
    after a revocation commit must never be allowed with claims resolved before that
    commit, and no assertion may be allowed at or after its claims deadline."""
    m = memo; admin_dsn = m['pg']; reader = m['reader']
    grant = m['admin'].execute("select payload from authz.nexloop_authority_facts where fact_kind='grants' and entity_key=%s", (grant_key(m),)).fetchone()[0]
    revocations = []; lock = threading.Lock(); stop = threading.Event(); errors = []; records = []
    def now(c):return c.execute('select clock_timestamp()').fetchone()[0]
    def revoker(kind):
        try:
            with psycopg.connect(admin_dsn, autocommit=True) as c:
                for _ in range(100):
                    if kind == 'grant':
                        payload = dict(grant); payload['grants'] = []
                        c.execute("update authz.nexloop_authority_facts set payload=%s where fact_kind='grants' and entity_key=%s", (Jsonb(payload), grant_key(m)))
                    else:
                        c.execute("update authz.nexloop_service_credentials set status='revoked' where token_digest=%s", (m['session'].token_digest,))
                    with lock:revocations.append(now(c))  # after the revocation committed
                    time.sleep(0.02)
                    if kind == 'grant':
                        c.execute("update authz.nexloop_authority_facts set payload=%s where fact_kind='grants' and entity_key=%s", (Jsonb(grant), grant_key(m)))
                    else:
                        # Restored with a new expiry: claims resolved before the revocation stay stale.
                        c.execute("update authz.nexloop_service_credentials set status='active',expires_at=expires_at+interval '1 millisecond' where token_digest=%s", (m['session'].token_digest,))
                    time.sleep(0.03)
        except Exception as error:errors.append(error)
    def asserter(index):
        try:
            with psycopg.connect(admin_dsn, autocommit=True) as c:
                c.execute("set track_functions='pl'"); c.execute("select set_config('eios.tenant_id','synthetic-a',false)")
                while not stop.is_set():
                    try:
                        session = authenticate_service(reader.pool, m['token'], world='real')
                        value = AuthorizedObjectReader(reader.pool, session, reader.signer)._configured(ResourceType.OBJECT, 'Consumer/' + m['object_id'], Operation.READ)
                        resolved_at = now(c)
                        if index % 2:value['expires_at'] = (datetime.now(UTC) + timedelta(milliseconds=30)).isoformat()
                    except Exception:
                        time.sleep(0.005); continue
                    for _ in range(4):  # the same claims across later revocations
                        with c.transaction():
                            out = c.execute('select public.o5b_loop(%s,%s,%s,6,0.008,null)', (session.token_digest, 'real', Jsonb(value))).fetchone()[0]
                        records.append((resolved_at, datetime.fromisoformat(value['expires_at']), out))
                        time.sleep(0.01)
        except Exception as error:errors.append(error)
    revokers = [threading.Thread(target=revoker, args=(k,)) for k in ('grant', 'credential')]
    asserters = [threading.Thread(target=asserter, args=(i,)) for i in range(4)]
    for t in asserters + revokers:t.start()
    for t in revokers:t.join(300)
    stop.set()
    for t in asserters:t.join(60)
    assert not errors, errors
    violations = []; statements = stale_statements = hits = allowed = 0
    for resolved_at, deadline, out in records:
        statements += 1; start = datetime.fromisoformat(out['start'])
        stale = any(resolved_at < r < start for r in revocations)  # revocation committed between resolve and statement start
        stale_statements += stale
        for row in out['results']:
            if row['ok']:
                allowed += 1
                if stale or datetime.fromisoformat(row['t0']) >= deadline:violations.append((resolved_at, start, row))
        hits += sum(1 for row in out['results'] if row['ok']) - out['misses']
    print(f'O5B_STRESS revocations={len(revocations)} statements={statements} stale_statements={stale_statements} '
          f'assertions={statements*6} allowed={allowed} hits={hits} violations={len(violations)}')
    assert len(revocations) >= 200 and statements >= 200 and stale_statements >= 50 and hits > 0 and allowed > 0
    assert violations == [], violations[:5]


def test_erasing_consumer_refuses_the_public_entry_and_the_inner_v0072(memo):
    """NX-029 §4.2a: a configured Consumer READ is refused while erasing at the public entry and at v0072 itself."""
    import psycopg
    from erasure_support import ERASING, mark_erasing, withdraw
    admin=memo['admin']
    call=lambda function:admin.execute(f'select {function}(%s,%s,%s)',(memo['session'].token_digest,'real',Jsonb(memo['claims']()))).fetchone()[0]
    with admin.transaction():assert call('authz.nexloop_assert_read_authority')
    mark_erasing(admin,'synthetic-a',memo['object_id'])
    for function in ('authz.nexloop_assert_read_authority','authz.nexloop_assert_read_authority_before_message_read_v0072'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege,match=ERASING),admin.transaction():call(function)
    withdraw(admin,'synthetic-a',memo['object_id'])
    with admin.transaction():assert call('authz.nexloop_assert_read_authority')
