"""NX-062 M2a (docs/implementation/NX-062-design.md §1, §2.2, §10 C1-C3) on real PostgreSQL: machine clients and keys, the machine
identity snapshot, realm exclusion against the service and browser realms in SQL, and the machine capability allowlist.

C1: a machine key never authenticates through the service snapshot (nor authenticate_service), a service token never through the machine
snapshot, a machine principal or digest can never be written into the service or browser realm and the other way round, and a row that
got there anyway (triggers bypassed, as a configuration error would) is still refused on read. C2: lookup by prefix_id, one HMAC with the
row's pepper version, validity at clock_timestamp(). C3: append-only revisions, latest wins, never-grant blacklist plus whitelist, no
privilege for any application role. Keys are generated at run time; no key literal appears here."""
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios import machine_access as A
from nexloop_eios import machine_keys as K
from nexloop_eios import service_grants as G
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A
from test_stage_register_pg import registered  # noqa: F401
from test_stage_secrets import tree  # noqa: F401

TENANT = TENANT_A
VERB = 'synthetic.read'  # a test-only read verb (object.get and object.list are registered by migration nx062_machine_reads)
WRITE = 'goals.agent.propose'  # a governed capability with subject rule agent_or_human (0124)
HUMAN = 'goals.metric.approve'  # subject rule human
ROLES = ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_runtime', 'nexloop_identity',
         'nexloop_configurator')
TABLES = ('authz.nexloop_machine_clients', 'authz.nexloop_machine_keys', 'control.nexloop_machine_capabilities',
          'control.nexloop_machine_read_verbs')
MIGRATION = 'migration:0434_nx062_machine_access.sql'


def denied(db, sql, args=(), message=None):
    with pytest.raises(psycopg.Error) as error:
        with db.transaction():
            db.execute(sql, args)
    if message:
        assert message in str(error.value), str(error.value)
    return error.value


def setup(db):
    bootstrap(db)
    db.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT,))
    db.execute("insert into control.nexloop_machine_read_verbs(verb,projection,description,source_task) values(%s,'[\"object_id\"]','synthetic read','NX-062')", (VERB,))
    db.execute("insert into control.nexloop_machine_capabilities(capability,revision,state,kind,description,source,recorded_by) values(%s,1,'active','read','synthetic',%s,'test')",
               (VERB, MIGRATION))
    return db


@pytest.fixture
def db(admin):
    return setup(admin)


@pytest.fixture
def peppers():
    return K.parse_peppers(f'1 {secrets.token_hex(32)}\n2 {secrets.token_hex(32)}\n')


def register(db, client='acme-agent'):
    return db.execute('select authz.nexloop_machine_client_register(%s,%s,%s,%s,%s)', (TENANT, client, 'Acme agent', 'synthetic', 'owner-principal')).fetchone()[0]


def issue(db, peppers, client='acme-agent', *, version=None, world='test', scope=(VERB,), days=30, previous=None, overlap=None):
    key = K.generate()
    version = version or peppers.current
    digest = K.digest(key, peppers, version).hex()
    key_id = uuid.uuid4()
    expires = datetime.now(UTC) + timedelta(days=days)
    if previous is None:
        db.execute('select authz.nexloop_machine_key_issue(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                   (TENANT, client, key_id, key.prefix_id, digest, version, world, list(scope), expires, 'owner-principal', 'req-' + key.prefix_id))
    else:
        db.execute('select authz.nexloop_machine_key_rotate(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                   (TENANT, previous, overlap, key_id, key.prefix_id, digest, version, list(scope), expires, 'owner-principal', 'req-' + key.prefix_id))
    return key, key_id


def authenticate(db, key, peppers):
    """What the API does (C2): the row's pepper version by prefix, one HMAC, then the snapshot. None when denied."""
    version = db.execute('select authz.nexloop_machine_key_pepper_version(%s)', (key.prefix_id,)).fetchone()[0]
    if version is None:
        return None
    try:
        with db.transaction():
            return db.execute('select authz.nexloop_machine_identity_snapshot(%s,%s)', (key.prefix_id, K.digest(key, peppers, version).hex())).fetchone()[0]
    except psycopg.errors.InsufficientPrivilege:
        return None


# Lifecycle ---------------------------------------------------------------------------------------------------------------

def test_issue_stores_only_the_digest_and_the_snapshot_derives_identity(db, peppers):
    register(db)
    key, key_id = issue(db, peppers)
    snap = authenticate(db, key, peppers)
    assert snap['binding'] == {'tenant_id': TENANT, 'credential_tenant_id': TENANT, 'credential_id': str(key_id), 'subject_id': 'nexloop-machine-subject:acme-agent',
                               'subject_kind': 'service', 'subject_principal_id': 'nexloop-machine:acme-agent', 'credential_kind': A.CREDENTIAL_KIND}
    assert snap['world'] == 'test' and snap['scope'] == [VERB] and snap['key_prefix'] == key.prefix_id
    # T-1: the secret (and the whole key) appears nowhere in the database.
    dump = db.execute("""select string_agg(t::text,' ') from (select to_jsonb(k) t from authz.nexloop_machine_keys k
        union all select to_jsonb(c) from authz.nexloop_machine_clients c) x""").fetchone()[0]
    assert key.secret not in dump and key.token not in dump and K.digest(key, peppers, peppers.current).hex() in dump


def test_c2_lookup_is_by_prefix_with_the_rows_pepper_version(db, peppers):
    register(db)
    key, _ = issue(db, peppers, version=1)
    assert db.execute('select authz.nexloop_machine_key_pepper_version(%s)', (key.prefix_id,)).fetchone()[0] == 1
    assert authenticate(db, key, peppers) is not None
    # The current version's digest of the same key is refused: one version, the row's.
    denied(db, 'select authz.nexloop_machine_identity_snapshot(%s,%s)', (key.prefix_id, K.digest(key, peppers, 2).hex()), 'machine authentication denied')
    # Another key's digest under a known prefix, a wrong secret, an unknown or malformed prefix: denied, one message.
    other = K.MachineKey(key.prefix_id, K.generate().secret)
    denied(db, 'select authz.nexloop_machine_identity_snapshot(%s,%s)', (key.prefix_id, K.digest(other, peppers, 1).hex()), 'machine authentication denied')
    denied(db, 'select authz.nexloop_machine_identity_snapshot(%s,%s)', (K.generate().prefix_id, K.digest(key, peppers, 1).hex()), 'machine authentication denied')
    denied(db, 'select authz.nexloop_machine_identity_snapshot(%s,%s)', ("' or 1=1 --", K.digest(key, peppers, 1).hex()), 'machine authentication denied')
    assert db.execute('select authz.nexloop_machine_key_pepper_version(%s)', ('AAAA',)).fetchone()[0] is None


def test_expiry_is_read_from_clock_timestamp(db, peppers):
    register(db)
    key, key_id = issue(db, peppers)
    assert authenticate(db, key, peppers)
    # Inside one transaction the first call passes; once expires_at is behind clock_timestamp() the next one in the same transaction fails.
    with db.transaction():
        db.execute("set local session_replication_role=replica")
        db.execute("update authz.nexloop_machine_keys set expires_at=clock_timestamp()+interval '300 milliseconds' where key_id=%s", (key_id,))
    with db.transaction():
        digest = K.digest(key, peppers, 2).hex()
        assert db.execute('select authz.nexloop_machine_identity_snapshot(%s,%s)', (key.prefix_id, digest)).fetchone()[0]
        db.execute('select pg_sleep(0.4)')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with db.transaction():
                db.execute('select authz.nexloop_machine_identity_snapshot(%s,%s)', (key.prefix_id, digest))


def test_revocation_is_immediate_and_final(db, peppers, pg):
    register(db)
    key, key_id = issue(db, peppers)
    with psycopg.connect(pg, autocommit=True) as other:
        assert authenticate(other, key, peppers)
        db.execute('select authz.nexloop_machine_key_revoke(%s,%s)', (TENANT, key_id))
        assert authenticate(other, key, peppers) is None
    denied(db, "update authz.nexloop_machine_keys set status='active',revoked_at=null,revoked_reason=null where key_id=%s", (key_id,), 'immutable')
    denied(db, 'select authz.nexloop_machine_key_revoke(%s,%s)', (TENANT, key_id), 'unavailable')


def test_two_key_limit_overlap_and_rotation(db, peppers):
    register(db)
    first, first_id = issue(db, peppers, days=60)
    # A second plain issue while the first runs beyond 7 days: overlap refused.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='machine_key_overlap'):
        with db.transaction():
            issue(db, peppers)
    second, second_id = issue(db, peppers, previous=first_id, overlap=timedelta(days=7))
    assert authenticate(db, first, peppers) and authenticate(db, second, peppers)
    ends = db.execute('select expires_at-clock_timestamp() from authz.nexloop_machine_keys where key_id=%s', (first_id,)).fetchone()[0]
    assert timedelta(days=6, hours=23) < ends <= timedelta(days=7)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='machine_key_limit'):
        with db.transaction():
            issue(db, peppers, previous=second_id, overlap=timedelta(days=1))
    with pytest.raises(psycopg.Error, match='machine_key_overlap'):
        with db.transaction():
            db.execute('select authz.nexloop_machine_key_revoke(%s,%s)', (TENANT, first_id))
            issue(db, peppers, previous=second_id, overlap=timedelta(days=8))
    # Expiry: at most 90 days; never extended later.
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            db.execute('select authz.nexloop_machine_key_revoke(%s,%s)', (TENANT, first_id))
            issue(db, peppers, previous=second_id, overlap=timedelta(0), days=91)
    denied(db, "update authz.nexloop_machine_keys set expires_at=expires_at+interval '1 day' where key_id=%s", (second_id,), 'immutable')


def test_deactivation_cascades_and_reactivation_revives_nothing(db, peppers):
    register(db)
    first, first_id = issue(db, peppers, days=5)
    second, _ = issue(db, peppers, previous=first_id, overlap=timedelta(days=3))
    result = db.execute('select authz.nexloop_machine_client_set_status(%s,%s,%s)', (TENANT, 'acme-agent', 'inactive')).fetchone()[0]
    assert result['keys_revoked'] == 2
    assert db.execute("select array_agg(distinct status||':'||revoked_reason) from authz.nexloop_machine_keys").fetchone()[0] == ['revoked:client_deactivated']
    db.execute('select authz.nexloop_machine_client_set_status(%s,%s,%s)', (TENANT, 'acme-agent', 'active'))
    assert authenticate(db, first, peppers) is None and authenticate(db, second, peppers) is None
    # A key of an inactive client never authenticates even if its row were active (two layers checked).
    third, third_id = issue(db, peppers)
    with db.transaction():
        db.execute("set local session_replication_role=replica")
        db.execute("update authz.nexloop_machine_clients set status='inactive'")
    assert authenticate(db, third, peppers) is None
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='inactive'):
        with db.transaction():
            issue(db, peppers)


def test_world_binding_scope_and_identity_immutability(db, peppers):
    register(db)
    denied(db, 'select authz.nexloop_machine_key_issue(%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+%s::interval,%s,%s)',
           (TENANT, 'acme-agent', uuid.uuid4(), K.generate().prefix_id, secrets.token_hex(32), 1, 'no world!', [VERB], '1 day', 'o', 'r'))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='scope outside the allowlist'):
        with db.transaction():
            issue(db, peppers, scope=(VERB, 'synthetic.unregistered'))
    key, key_id = issue(db, peppers, world='real')
    assert authenticate(db, key, peppers)['world'] == 'real'
    for column, value in (('world', 'test'), ('scope', '{synthetic.read,synthetic.unregistered}'), ('client_id', 'other-agent'), ('pepper_version', '1')):
        denied(db, f'update authz.nexloop_machine_keys set {column}=%s where key_id=%s', (value, key_id))
    denied(db, "update authz.nexloop_machine_clients set principal_id='nexloop-machine:other'", ())
    denied(db, 'delete from authz.nexloop_machine_keys', (), 'append-only')
    denied(db, 'delete from authz.nexloop_machine_clients', (), 'append-only')


# C1: realm exclusion -----------------------------------------------------------------------------------------------------

def test_c1_machine_digest_refused_by_the_service_snapshot(db, peppers):
    register(db)
    key, _ = issue(db, peppers)
    digest = K.digest(key, peppers, peppers.current).hex()
    denied(db, 'select authz.nexloop_service_identity_snapshot(%s,%s)', (digest, 'test'), 'authentication kind conflict')
    # The inner (pre-0434) snapshot has no grant left for any application role.
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler'):
        assert not db.execute("select has_function_privilege(%s,'authz.nexloop_service_identity_snapshot_before_machine(text,text)','execute')", (role,)).fetchone()[0]
        assert db.execute("select has_function_privilege(%s,'authz.nexloop_service_identity_snapshot(text,text)','execute')", (role,)).fetchone()[0]


def test_the_wrapper_keeps_every_grantee_of_the_snapshot_and_the_inner_function_has_none(db):
    """0434 fix (nx062_service_snapshot_grants): every role that may call the service snapshot calls the wrapper - the four service
    roles and the NX-041 simulation roles - and no role but the owner holds EXECUTE on the renamed inner function."""
    holders = lambda f: {r for (r,) in db.execute("""select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
        where p.oid=%s::regprocedure and a.privilege_type='EXECUTE' and a.grantee<>p.proowner""", (f,)).fetchall()}
    assert holders('authz.nexloop_service_identity_snapshot_before_machine(text,text)') == set()
    assert {'nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_sim_api', 'nexloop_sim_worker',
            'nexloop_sim_guard'} <= holders('authz.nexloop_service_identity_snapshot(text,text)')


def test_c1_write_side_exclusion(db, peppers):
    register(db)
    key, _ = issue(db, peppers)
    digest = K.digest(key, peppers, peppers.current).hex()
    binding = '{"tenant_id":"%s","credential_tenant_id":"%s","principal_id":"nexloop-machine:acme-agent","subject_principal_id":"nexloop-machine:acme-agent"}' % (TENANT, TENANT)
    denied(db, """insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at)
        values(%s,%s,'forged',%s::jsonb,'{test}','nexloop-core','active',clock_timestamp()+interval '1 day')""", (secrets.token_hex(32), TENANT, binding), 'authentication kind conflict')
    denied(db, """insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at)
        values(%s,%s,'forged',%s::jsonb,'{test}','nexloop-core','active',clock_timestamp()+interval '1 day')""",
           (digest, TENANT, binding.replace('nexloop-machine:acme-agent', 'svc-x')), 'authentication kind conflict')
    denied(db, "insert into control.nexloop_browser_memberships(tenant_id,principal_id) values(%s,'nexloop-machine:acme-agent')", (TENANT,), 'authentication kind conflict')
    denied(db, 'insert into authz.nexloop_browser_token_realms values(%s,%s,%s)', (digest, TENANT, str(uuid.uuid4())), 'authentication kind conflict')


def test_c1_cross_realm_with_a_registered_stage(registered, peppers):  # noqa: F811
    """A real registered stage: its service token never passes the machine snapshot, an nxm key never passes authenticate_service,
    and a forged cross-realm row (triggers bypassed) is refused on read by both snapshots."""
    r = registered
    admin = r['admin']
    import json
    tokens = json.loads((r['tree']['out'] / 'management' / 'service-secrets.json').read_text())
    principal = r['manifest']['principals'][0]
    token = next(t for k, t in tokens.items() if r['stage']['credential_files'].get(k.split(':', 1)[1]) == principal['role'])
    pool = r['pools'][principal['database_role']]
    assert authenticate_service(pool, token, world='real').authentication.subject_principal_id == principal['principal_id']
    setup_rows(admin)
    register(admin)
    key, _ = issue(admin, peppers, world='real')
    service_digest = hashlib.sha256(token.encode()).hexdigest()
    # Service token -> machine realm: its digest under any prefix is denied, and it can never be stored as a machine digest.
    denied(admin, 'select authz.nexloop_machine_identity_snapshot(%s,%s)', (key.prefix_id, service_digest), 'machine authentication denied')
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='authentication kind conflict'):
        with admin.transaction():
            admin.execute('select authz.nexloop_machine_key_issue(%s,%s,%s,%s,%s,1,%s,%s,clock_timestamp()+%s::interval,%s,%s)',
                          (TENANT, 'acme-agent', uuid.uuid4(), K.generate().prefix_id, service_digest, 'real', [VERB], '1 day', 'o', 'r'))
    # Machine key -> service realm: authenticate_service (sha256 of the token) and the snapshot with the HMAC digest are both denied.
    with pytest.raises(AuthorizationUnavailable):
        authenticate_service(pool, key.token, world='real')
    with pytest.raises(psycopg.Error, match='authentication kind conflict'):
        with pool.connection() as c:
            c.execute('select authz.nexloop_service_identity_snapshot(%s,%s)', (K.digest(key, peppers, peppers.current).hex(), 'real'))
    # Forged rows (as a configuration error would write them, triggers bypassed) are refused on read, not only on write.
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""update authz.nexloop_machine_keys set digest=%s where prefix_id=%s""", (service_digest, key.prefix_id))
    with pytest.raises(AuthorizationUnavailable):
        authenticate_service(pool, token, world='real')
    denied(admin, 'select authz.nexloop_machine_identity_snapshot(%s,%s)', (key.prefix_id, service_digest), 'machine authentication denied')
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute('update authz.nexloop_machine_keys set digest=%s where prefix_id=%s', (K.digest(key, peppers, peppers.current).hex(), key.prefix_id))
        # A service credential whose binding names the machine principal (the service snapshot must refuse it).
        admin.execute("""update authz.nexloop_service_credentials set binding=jsonb_set(binding,'{subject_principal_id}','"nexloop-machine:acme-agent"')
            where token_digest=%s""", (service_digest,))
    with pytest.raises(AuthorizationUnavailable):
        authenticate_service(pool, token, world='real')
    assert authenticate(admin, key, peppers) is None


def setup_rows(db):
    db.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (TENANT,))
    db.execute("insert into control.nexloop_machine_read_verbs(verb,projection,description,source_task) values(%s,'[\"object_id\"]','synthetic read','NX-062')", (VERB,))
    db.execute("insert into control.nexloop_machine_capabilities(capability,revision,state,kind,description,source,recorded_by) values(%s,1,'active','read','synthetic',%s,'test')",
               (VERB, MIGRATION))


# C3: allowlist -------------------------------------------------------------------------------------------------------------

CAP = "insert into control.nexloop_machine_capabilities(capability,revision,state,kind,description,source,recorded_by) values(%s,%s,%s,%s,'d',%s,'test')"


def test_c3_revisions_append_only_latest_wins(db, peppers):
    register(db)
    denied(db, CAP, (VERB, 1, 'retired', 'read', MIGRATION), 'revision must follow')
    denied(db, CAP, (VERB, 3, 'retired', 'read', MIGRATION), 'revision must follow')
    issue(db, peppers)
    db.execute(CAP, (VERB, 2, 'retired', 'read', MIGRATION))
    assert not db.execute('select control.nexloop_machine_capability_current(%s)', (VERB,)).fetchone()[0]
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='outside the allowlist'):
        with db.transaction():
            db.execute('select authz.nexloop_machine_client_register(%s,%s,%s,%s,%s)', (TENANT, 'second-agent', 'S', 'p', 'o'))
            issue(db, peppers, client='second-agent')
    db.execute(CAP, (VERB, 3, 'active', 'read', MIGRATION))
    assert db.execute('select control.nexloop_machine_capability_current(%s)', (VERB,)).fetchone()[0]
    for statement in ("update control.nexloop_machine_capabilities set state='active'", 'delete from control.nexloop_machine_capabilities',
                      'truncate control.nexloop_machine_capabilities', 'delete from control.nexloop_machine_read_verbs'):
        denied(db, statement, (), 'append-only')


def test_c3_whitelist_and_blacklist(db):
    denied(db, CAP, ('synthetic.unregistered', 1, 'active', 'read', MIGRATION), 'not a registered read verb')
    denied(db, CAP, (HUMAN, 1, 'active', 'write', MIGRATION), 'not a non-human governed capability')
    denied(db, CAP, ('nexloop.unknown', 1, 'active', 'write', MIGRATION), 'not a non-human governed capability')
    denied(db, CAP, (VERB, 2, 'active', 'write', MIGRATION), 'not a non-human governed capability')
    db.execute(CAP, (WRITE, 1, 'active', 'write', MIGRATION))
    denied(db, "insert into control.nexloop_machine_read_verbs(verb,projection,description,source_task) values('approval.decide','[\"x\"]','d','NX-062')", (), 'never granted')
    for name in ('approval.decide', 'machine_key.issue', 'workbench_member.invite', 'schema.publish', 'consumer.erase', 'retention.hold',
                 'nexloop.service.request', 'channel.enable', 'remediation.decide', 'service_grants.apply'):
        assert db.execute('select control.nexloop_machine_capability_forbidden(%s)', (name,)).fetchone()[0], name
        assert A.forbidden(name)
    denied(db, CAP, ('approval.decide', 1, 'active', 'read', MIGRATION), 'never granted')
    # The source must name a migration or a governed request; nothing else.
    denied(db, CAP, ('synthetic.read', 2, 'active', 'read', 'api:direct'), 'check')


def test_c3_never_grant_list_locked_to_python_and_covers_service_grants(db):
    assert tuple(db.execute('select control.nexloop_machine_never_grant_markers()').fetchone()[0]) == A.NEVER_GRANT_MARKERS
    assert all(A.forbidden(m) for m in G.FORBIDDEN_MARKERS)
    names = [r[0] for r in db.execute('select capability from control.nexloop_governed_capabilities').fetchall()]
    for name in names + ['object.get', 'goals.agent.propose', 'x.APPROVAL.y']:
        assert db.execute('select control.nexloop_machine_capability_forbidden(%s)', (name,)).fetchone()[0] == A.forbidden(name), name


def test_c3_no_application_role_holds_any_privilege(db):
    for table in TABLES:
        for role in ROLES:
            for privilege in ('select', 'insert', 'update', 'delete', 'truncate', 'references', 'trigger'):
                assert not db.execute('select has_table_privilege(%s,%s,%s)', (role, table, privilege)).fetchone()[0], (role, table, privilege)
    for function in ('authz.nexloop_machine_client_register(text,text,text,text,text)', 'authz.nexloop_machine_client_set_status(text,text,text)',
                     'authz.nexloop_machine_key_issue(text,text,uuid,text,text,integer,text,text[],timestamptz,text,text)',
                     'authz.nexloop_machine_key_rotate(text,uuid,interval,uuid,text,text,integer,text[],timestamptz,text,text)',
                     'authz.nexloop_machine_key_revoke(text,uuid)'):
        for role in ROLES:
            assert not db.execute("select has_function_privilege(%s,%s,'execute')", (role, function)).fetchone()[0], (role, function)
    for function in ('authz.nexloop_machine_identity_snapshot(text,text)', 'authz.nexloop_machine_key_pepper_version(text)'):
        assert [r for r in ROLES if db.execute("select has_function_privilege(%s,%s,'execute')", (r, function)).fetchone()[0]] == ['nexloop_api']


def test_snapshot_through_the_api_role(pg, db, peppers):
    """nexloop_api runs the machine snapshot itself (no other privilege on the tables)."""
    register(db)
    key, _ = issue(db, peppers)
    db.execute("set role nexloop_api")
    try:
        assert authenticate(db, key, peppers)['binding']['subject_principal_id'] == 'nexloop-machine:acme-agent'
        denied(db, 'select * from authz.nexloop_machine_keys', (), 'permission denied')
    finally:
        db.execute('reset role')
