"""NX-052 0050 tenant row lock review (docs/implementation/NX-052-tenant-lock-review.md §5).

configure_manifest and initial_identity_create take FOR NO KEY UPDATE on the tenant row instead of FOR UPDATE: a tenant
foreign key check (FOR KEY SHARE) is no longer blocked while either is uncommitted, and everything else they serialize
against is unchanged. Session A runs the function in an open transaction; session B uses lock_timeout and reads 55P03
(lock_not_available) as "blocked". The old definitions are re-installed from their published migrations to prove the
scenarios distinguish the two states.
"""
import copy
import hashlib
import hmac
import re
import secrets
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from argon2 import PasswordHasher
from psycopg.conninfo import make_conninfo

from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.identity.ports import SaveLocalAccountCommand
from nexloop_eios.browser_identity import decode_local_account
from nexloop_eios.initial_identity import validate_identity_manifest
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.trusted_configuration import validate_manifest
from authority_fixture import authority_records
from test_trusted_configuration_pg import configured, identity_manifest  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / 'packages/eios-core/src/eios/migrations'
sys.path.insert(0, str(ROOT / 'scripts/load'))
import tenant_fk_cost  # noqa: E402

MANIFEST_LOCK = 'select authority_revision into v_revision from control.nexloop_tenants where tenant_id=p_tenant for '
IDENTITY_LOCK = "perform 1 from control.nexloop_tenants where tenant_id=p_tenant and status='active' for "


def definition(path, head):
    text = path.read_text()
    start = text.index(head)
    tag = re.compile(r'\$(\w*)\$').search(text, start)
    end = text.index(tag.group(0), tag.end()) + len(tag.group(0)) + 1
    return text[start:end]


def current_migration():
    return next(p for p in sorted(MIGRATIONS.glob('*_nx052_tenant_row_no_key_lock.sql')))


OLD_MANIFEST = definition(MIGRATIONS / '0142_relay_source_read.sql', 'create or replace function control.nexloop_configure_manifest(')
OLD_IDENTITY = definition(MIGRATIONS / '0050_trusted_configuration.sql', 'create function control.nexloop_initial_identity_create(').replace(
    'create function', 'create or replace function', 1)
NEW_MANIFEST = definition(current_migration(), 'create or replace function control.nexloop_configure_manifest(')
NEW_IDENTITY = definition(current_migration(), 'create or replace function control.nexloop_initial_identity_create(')


def test_new_bodies_differ_from_the_published_ones_only_in_the_two_locks():
    assert NEW_MANIFEST == OLD_MANIFEST.replace(MANIFEST_LOCK + 'update;', MANIFEST_LOCK + 'no key update;')
    # The only other change: initial_identity_create lists pg_temp last (definer search_path rule for a newly defined function).
    assert NEW_IDENTITY == OLD_IDENTITY.replace(IDENTITY_LOCK + 'update;', IDENTITY_LOCK + 'no key update;').replace(
        'security definer set search_path=pg_catalog set row_security', 'security definer set search_path=pg_catalog,pg_temp set row_security', 1)
    assert NEW_MANIFEST != OLD_MANIFEST and NEW_IDENTITY != OLD_IDENTITY


def install(admin, *bodies):
    for body in bodies:
        admin.execute(body)


def test_installed_functions_keep_owner_grants_and_search_path_and_carry_the_new_lock(configured, admin):  # noqa: F811
    rows = admin.execute("""select p.proname, pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig, p.proacl::text, pg_get_functiondef(p.oid)
        from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname='control' and p.proname in ('nexloop_configure_manifest','nexloop_initial_identity_create') order by 1""").fetchall()
    assert len(rows) == 2 and all(r[1] == 'nexloop_owner' and r[2] for r in rows)
    assert all('for no key update' in r[5] and r[5].count('nexloop_tenants where tenant_id=p_tenant') >= 1 for r in rows)
    assert dict((r[0], r[3]) for r in rows)['nexloop_initial_identity_create'][0] == 'search_path=pg_catalog, pg_temp'
    before = {r[0]: (r[3], r[4]) for r in rows}
    install(admin, OLD_MANIFEST, OLD_IDENTITY, NEW_MANIFEST, NEW_IDENTITY)
    after = {r[0]: (r[1], r[2]) for r in admin.execute("""select p.proname, p.proconfig, p.proacl::text from pg_proc p join pg_namespace n
        on n.oid=p.pronamespace where n.nspname='control' and p.proname in ('nexloop_configure_manifest','nexloop_initial_identity_create')""").fetchall()}
    assert after == before


def test_no_unique_index_on_tenants_covers_status_or_authority_revision(configured, admin):  # noqa: F811
    # A unique index on an updated column would make the functions' own update take FOR UPDATE again (a key update).
    columns = admin.execute("""select a.attname from pg_index i join pg_attribute a on a.attrelid=i.indrelid and a.attnum=any(i.indkey)
        where i.indrelid='control.nexloop_tenants'::regclass and i.indisunique""").fetchall()
    assert {c for (c,) in columns} == {'tenant_id'}
    expressions = admin.execute("""select count(*) from pg_index where indrelid='control.nexloop_tenants'::regclass and indisunique
        and (indexprs is not null or indpred is not null)""").fetchone()[0]
    assert expressions == 0


# --- helpers: the exact calls of the production ports, inside a transaction the test keeps open ---

def manifest_call(db, f, body, token=None):
    body = validate_manifest(body)
    text = canonical_payload(body)
    key = bytes.fromhex(f['paths']['signing'].read_text())
    digests = canonical_payload({'source': hashlib.sha256((token or f['token']).encode()).hexdigest()})
    return db.execute('select control.nexloop_configure_manifest(%s,%s,%s,%s,%s,%s::jsonb)',
                      (body['tenant_id'], text, hashlib.sha256(text.encode()).hexdigest(), 'explicit-configuration', key, digests)).fetchone()[0]


def identity_call(db, f, body):
    body['membership']['subject_id'] = body['subject']['subject_id']
    body['account']['subject_id'] = body['subject']['subject_id']
    value = validate_identity_manifest(body)
    password = f['paths']['password'].read_text()
    seal = bytes.fromhex(f['paths']['seal'].read_text())
    fingerprint = hmac.new(seal, canonical_payload({'domain': 'nexloop-initial-password-v1', 'tenant_id': value['tenant_id'], 'application_id': value['application_id'],
                                                    'request_id': value['request_id'], 'password': password}).encode(), 'sha256').hexdigest()
    account = decode_local_account({**value['account'], 'password_hash': PasswordHasher().hash(password), 'password_history': []})
    command = SaveLocalAccountCommand(local_account=account, expected_revision=None)
    return db.execute('select control.nexloop_initial_identity_create(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s)',
                      (value['tenant_id'], value['application_id'], 'nexloop_identity', value['request_id'], value['operator_label'],
                       canonical_payload(value['subject']), canonical_payload(value['membership']), canonical_payload(value['account']),
                       command.local_account.password_hash.get_secret_value(), fingerprint, seal)).fetchone()[0]


def next_manifest(f, admin, **changes):
    body = copy.deepcopy(f['manifest'])
    revision = admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s', (f['tenant'],)).fetchone()[0]
    body.update(manifest_id=str(uuid.uuid4()), expected_revision=revision, **changes)
    return body


def blocked(dsn, statement, params=()):
    """True when the statement waits on a lock past 200 ms (55P03); False when it completes."""
    with psycopg.connect(dsn) as b:
        b.execute("set lock_timeout='200ms'")
        try:
            b.execute(statement, params)
            b.rollback()
            return False
        except psycopg.errors.LockNotAvailable:
            return True


FK_INSERT = "insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop-lock-probe',3,60,true)"
SHARE_READ = 'select 1 from control.nexloop_tenants where tenant_id=%s for share'


def holding(f, kind):
    """Session A: the configurator (manifest) or identity role (initial identity); the test keeps its transaction open."""
    return psycopg.connect(make_conninfo(f["pg"], user="nexloop_configurator" if kind == "manifest" else "nexloop_identity"))


def run_scenarios(f, admin, other_tenant):
    pg, tenant = f['pg'], f['tenant']
    results = {}
    # 1-3 and 6: configure_manifest held by A.
    a = holding(f, 'manifest')
    try:
        manifest_call(a, f, next_manifest(f, admin))
        results[1] = blocked(pg, FK_INSERT, (tenant,))
        with psycopg.connect(make_conninfo(pg, user='nexloop_configurator')) as b:
            b.execute("set lock_timeout='200ms'")
            try:
                manifest_call(b, f, next_manifest(f, admin))
                results[2] = False
            except psycopg.errors.LockNotAvailable:
                results[2] = True
        results[3] = blocked(pg, SHARE_READ, (tenant,))
        results[6] = blocked(pg, FK_INSERT, (other_tenant,))
    finally:
        a.rollback()
        a.close()
    # 4-5: initial_identity_create held by A.
    a = holding(f, 'identity')
    try:
        identity_call(a, f, identity_manifest(f))
        results[4] = blocked(pg, FK_INSERT, (tenant,))
        with psycopg.connect(make_conninfo(pg, user='nexloop_configurator')) as b:
            b.execute("set lock_timeout='200ms'")
            try:
                manifest_call(b, f, next_manifest(f, admin, tenant_status='suspended'))
                results[5] = False
            except psycopg.errors.LockNotAvailable:
                results[5] = True
    finally:
        a.rollback()
        a.close()
    return results


@pytest.fixture
def other_tenant(admin):
    tenant = str(uuid.uuid4())
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active')", (tenant,))
    return tenant


def test_scenarios_before_and_after_the_lock_change(configured, admin, other_tenant):  # noqa: F811
    admin.execute("alter role nexloop_identity login")
    # Before: the published definitions (FOR UPDATE) block the foreign key check (1, 4).
    install(admin, OLD_MANIFEST, OLD_IDENTITY)
    before = run_scenarios(configured, admin, other_tenant)
    assert before == {1: True, 2: True, 3: True, 4: True, 5: True, 6: False}, before
    # After: only the foreign key check is released; serialization (2, 5) and FOR SHARE readers (3) are unchanged.
    install(admin, NEW_MANIFEST, NEW_IDENTITY)
    after = run_scenarios(configured, admin, other_tenant)
    assert after == {1: False, 2: True, 3: True, 4: False, 5: True, 6: False}, after


def fresh_manifest(tenant):
    """A first publication for a new tenant, built like the configured fixture's (own credential binding)."""
    binding, _, rows = authority_records(tenant, 'eios:artifact:explicit-configuration-source', operation=Operation.READ, resource_type=ResourceType.ARTIFACT)
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    return {'schema_version': '1.0', 'manifest_id': str(uuid.uuid4()), 'tenant_id': tenant, 'expected_revision': 0, 'tenant_status': 'active',
            'object_types': [], 'actions': [], 'functions': [],
            'authority_facts': [{'kind': kind, 'key': key, 'payload': fact.model_dump(mode='json')} for kind, key, fact in rows],
            'service_credentials': [{'reference': 'source', 'binding': binding.model_dump(mode='json'), 'worlds': ['real'], 'expires_at': expiry, 'status': 'active'}],
            'browser_applications': [], 'browser_business_applications': [], 'browser_rate_policies': [], 'identity_allowances': []}


def test_concurrent_first_publication_of_a_new_tenant_still_admits_one(configured, admin):  # noqa: F811
    # Scenario 7: the insert branch takes no tenant row lock in either version; the primary key decides between two first publications.
    tenant = str(uuid.uuid4())
    bodies = [fresh_manifest(tenant), fresh_manifest(tenant)]
    barrier, outcomes = threading.Barrier(2), []

    def publish(body):
        token = secrets.token_urlsafe(48)
        with psycopg.connect(make_conninfo(configured['pg'], user='nexloop_configurator')) as db:
            barrier.wait()
            try:
                manifest_call(db, configured, body, token)
                db.commit()
                outcomes.append('ok')
            except psycopg.Error as error:
                db.rollback()
                outcomes.append(error.sqlstate)

    threads = [threading.Thread(target=publish, args=(b,)) for b in bodies]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes)[1] == 'ok' and sorted(outcomes)[0] in ('23505', '40001'), outcomes
    assert admin.execute('select count(*) from control.nexloop_tenants where tenant_id=%s', (tenant,)).fetchone() == (1,)


def test_lock_probe_wait_under_no_key_update_is_below_ten_percent_of_the_hold(configured, admin, pg):  # noqa: F811
    report = tenant_fk_cost.measure(pg, 'control.nexloop_commercial_connectors', configured['tenant'], rows=20, rounds=1, hold_seconds=0.5)
    probe = report['lock_probe']
    assert probe['blocked_by_for_update'] and probe['insert_wait_with_fk_under_no_key_update_s'] < 0.1 * probe['tenant_row_lock_held_s'], probe
