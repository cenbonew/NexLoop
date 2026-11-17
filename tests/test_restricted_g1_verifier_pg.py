"""G1 in a disposable cluster: no credential or production admission is provisioned."""
import uuid
import importlib.util
import json
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios.bootstrap import bootstrap


ROLE = 'nexloop_g1_verifier'
QUERY = 'select nexloop_maintenance.nexloop_g1_verify()'
SPEC = importlib.util.spec_from_file_location('g1_verify', Path(__file__).parents[1] / 'scripts/acceptance/g1_verify.py')
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


@pytest.fixture
def g1(admin):
    bootstrap(admin)
    admin.execute('alter role nexloop_g1_verifier login')  # disposable fixture only; no password
    tenants = [str(uuid.uuid4()), str(uuid.uuid4())]
    for t, status in zip(tenants, ('active', 'suspended')):
        admin.execute('insert into control.nexloop_tenants(tenant_id,status) values(%s,%s)', (t, status))
    return tenants


def connect(pg, role=ROLE):
    return psycopg.connect(make_conninfo(pg, user=role), autocommit=True)


def begin(db):
    db.execute('begin isolation level repeatable read read only')


def admission(admin, tenant):
    # Disposable admin fixture fault/positive injection, never a governed production admission.
    admin.execute("""insert into control.nexloop_effect_action_admissions
      (tenant_id,world,stable_name,version,consumer_version,predecessor_resource,publication_id,admitted_by,judgement)
      values(%s,'real','nexloop.service.request',2,2,'eios:action:nexloop.service.request:1',%s,'human:owner','{"admissible":true}')""",
                  (tenant, uuid.uuid4()))


def test_zero_and_suspended_cross_tenant_positive_despite_caller_guc(g1, admin, pg):
    with connect(pg) as db:
        assert db.execute('select session_user,current_user').fetchone() == (ROLE, ROLE)
        db.execute("set eios.tenant_id='caller-controlled'")
        db.execute('set row_security=off')
        begin(db)
        out = db.execute(QUERY).fetchone()[0]
        assert out['status'] == 'clear' and out['active_admissions'] == 0
        assert out['tenants_checked'] == 2 and out['suspended_checked'] == 1 and out['effect_actions'] > 0
        assert set(out) == {'status', 'active_admissions', 'tenants_checked', 'suspended_checked', 'effect_actions'}
        assert db.execute("select current_setting('eios.tenant_id'),current_setting('row_security'),current_user").fetchone() == ('caller-controlled', 'off', ROLE)
        db.execute('rollback')
    admission(admin, g1[1])
    with connect(pg) as db:
        begin(db)
        out = db.execute(QUERY).fetchone()[0]
        assert out['status'] == 'blocked_nonzero' and out['active_admissions'] == 1 and out['tenants_checked'] == 2
        assert not any(t in str(out) for t in g1)
        db.execute('rollback')


def test_snapshot_is_stable_until_rollback(g1, admin, pg):
    with connect(pg) as db:
        begin(db)
        before = db.execute(QUERY).fetchone()[0]
        admission(admin, g1[1])
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'suspended')", (str(uuid.uuid4()),))
        assert db.execute(QUERY).fetchone()[0] == before
        db.execute('rollback')
        begin(db)
        after = db.execute(QUERY).fetchone()[0]
        assert after['active_admissions'] == 1 and after['tenants_checked'] == 3
        db.execute('rollback')


@pytest.mark.parametrize('mode', ['begin read only', 'begin isolation level repeatable read read write'])
def test_wrong_transaction_is_refused(g1, pg, mode):
    with connect(pg) as db:
        db.execute(mode)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute(QUERY)
        db.execute('rollback')


@pytest.mark.parametrize('role', ['nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_runtime', 'nexloop_bootstrap'])
def test_ordinary_or_bootstrap_identity_cannot_call(g1, pg, role):
    with connect(pg, role) as db:
        begin(db)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute(QUERY)
        db.execute('rollback')


def test_verifier_has_no_owner_or_table_or_ddl_authority(g1, admin, pg):
    assert admin.execute('select rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolreplication from pg_roles where rolname=%s', (ROLE,)).fetchone() == (False,) * 5
    assert admin.execute("select count(*) from pg_auth_members where member=(select oid from pg_roles where rolname=%s)", (ROLE,)).fetchone() == (0,)
    assert admin.execute("""select n.nspname,p.proname from pg_proc p join pg_namespace n on n.oid=p.pronamespace
      where n.nspname not in ('pg_catalog','information_schema') and n.nspname not like 'pg_temp%%'
      and has_schema_privilege(%s,n.oid,'usage') and has_function_privilege(%s,p.oid,'execute')
      order by 1,2""", (ROLE, ROLE)).fetchall() == [('nexloop_maintenance', 'nexloop_g1_verify')]
    with connect(pg) as db:
        # ACLs, not the resettable read-only default, must prevent arbitrary writes.
        db.execute('set default_transaction_read_only=off')
        assert db.execute('select has_database_privilege(current_user,current_database(),\'TEMPORARY\')').fetchone() == (False,)
        for query in ['set role nexloop_owner', 'select * from control.nexloop_tenants',
                      'select * from control.nexloop_effect_action_admissions',
                      'delete from control.nexloop_effect_action_admissions',
                      'create table authz.synthetic_g1_unauthorized(x int)',
                      'alter function nexloop_maintenance.nexloop_g1_verify() owner to nexloop_g1_verifier']:
            with pytest.raises(psycopg.Error):
                db.execute(query)


@pytest.mark.parametrize('damage', [
    'alter table control.nexloop_effect_action_admissions no force row level security',
    'alter policy effect_action_admissions_tenant on control.nexloop_effect_action_admissions using (false)',
    'alter table control.nexloop_tenants enable row level security',
    'grant nexloop_owner to nexloop_g1_verifier',
    'grant select on control.nexloop_effect_action_admissions to nexloop_g1_verifier',
])
def test_hidden_rows_or_overgrants_fail_closed(g1, admin, pg, damage):
    admission(admin, g1[1])
    admin.execute(damage)
    with connect(pg) as db:
        begin(db)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute(QUERY)
        db.execute('rollback')


def test_empty_registry_and_database_error_are_not_zero(admin, pg):
    bootstrap(admin)
    admin.execute('alter role nexloop_g1_verifier login')
    with connect(pg) as db:
        begin(db)
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            db.execute(QUERY)
        db.execute('rollback')
    admin.execute('alter table control.nexloop_effect_actions rename to synthetic_g1_missing_actions')
    with connect(pg) as db:
        begin(db)
        with pytest.raises(psycopg.errors.UndefinedTable):
            db.execute(QUERY)
        db.execute('rollback')


def test_migration_provisions_no_login_or_credential(admin):
    bootstrap(admin)
    assert admin.execute('select rolcanlogin,rolpassword from pg_authid where rolname=%s', (ROLE,)).fetchone() == (False, None)


def test_accidental_third_party_execute_grant_still_cannot_call(g1, admin, pg):
    admin.execute('grant usage on schema nexloop_maintenance to nexloop_api')
    admin.execute('grant execute on function nexloop_maintenance.nexloop_g1_verify() to nexloop_api')
    with connect(pg, 'nexloop_api') as db:
        begin(db)
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='restricted read-only snapshot'):
            db.execute(QUERY)
        db.execute('rollback')


def test_fixed_cli_zero_nonzero_and_error_exit_codes(g1, admin, pg, tmp_path, capsys):
    path = tmp_path / 'test-only-dsn'
    path.write_text(make_conninfo(pg, user=ROLE))
    path.chmod(0o600)
    assert CLI.main(['--database-url-file', str(path)]) == 0
    zero = json.loads(capsys.readouterr().out)
    assert zero['status'] == 'clear' and zero['tenants_checked'] == 2
    admission(admin, g1[1])
    assert CLI.main(['--database-url-file', str(path)]) == 1
    nonzero = json.loads(capsys.readouterr().out)
    assert nonzero['status'] == 'blocked_nonzero' and nonzero['active_admissions'] == 1
    admin.execute('alter table control.nexloop_effect_action_admissions no force row level security')
    assert CLI.main(['--database-url-file', str(path)]) == 2
    assert json.loads(capsys.readouterr().out) == {'status': 'blocked', 'reason': 'verification_or_rollback_unavailable'}


@pytest.mark.parametrize('changed', [{'status': 'clear', 'active_admissions': 1}, {'tenants_checked': 0},
                                   {'effect_actions': 0}, {'active_admissions': True}, {'extra': 'not-allowed'},
                                   {'suspended_checked': 3}])
def test_cli_rejects_inconsistent_or_incomplete_summary(changed):
    value = {'status': 'clear', 'active_admissions': 0, 'tenants_checked': 2, 'effect_actions': 9, 'suspended_checked': 1}
    with pytest.raises(ValueError):
        CLI.verdict({**value, **changed})


def test_cli_never_releases_clear_if_rollback_is_unconfirmed(monkeypatch, capsys):
    value = {'status': 'clear', 'active_admissions': 0, 'tenants_checked': 2, 'effect_actions': 9, 'suspended_checked': 1}
    class Stub:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, query):
            if query == 'rollback': raise RuntimeError('unconfirmed')
            return self
        def fetchone(self): return (value,)
    monkeypatch.setattr(CLI, 'read_private_text', lambda *a, **k: 'unused')
    monkeypatch.setattr(CLI.psycopg, 'connect', lambda *a, **k: Stub())
    assert CLI.main(['--database-url-file', 'unused']) == 2
    assert json.loads(capsys.readouterr().out) == {'status': 'blocked', 'reason': 'verification_or_rollback_unavailable'}
