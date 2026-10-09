"""0099: temporary-schema exposure is closed for the NexLoop lineage (disposable PG only).

Owner-run code must never resolve a name to a session's temporary schema: runtime roles
cannot create temporary objects at all, and every application routine that pins
search_path lists pg_temp explicitly and last. nexloop_owner keeps TEMPORARY (O5b memo).
"""
import importlib.util
import pathlib
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from nexloop_eios.bootstrap import bootstrap

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_definer_search_path', ROOT / 'scripts' / 'check_definer_search_path.py')
checker = importlib.util.module_from_spec(spec); spec.loader.exec_module(checker)
RUNTIME_LOGIN_ROLES = ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_runtime', 'nexloop_identity')
TEMP_OBJECTS = ('create temp table t(x int)', 'create type pg_temp.t as (x int)',
                "create function pg_temp.f() returns int language sql as 'select 1'", 'create temp view v as select 1')


@pytest.fixture
def bootstrapped(admin):
    bootstrap(admin)
    return admin


def test_static_check_is_clean_after_bootstrap(bootstrapped):
    found, summary = checker.problems(bootstrapped)
    assert found == [] and summary['routines_with_search_path'] > 200 and summary['security_definer'] > 200


def test_static_check_fails_on_an_unpinned_or_misordered_routine(bootstrapped):
    with bootstrapped.transaction():
        bootstrapped.execute("create function authz.nexloop_check_canary() returns int language sql security definer set search_path=pg_catalog as 'select 1'")
        bootstrapped.execute("create function authz.nexloop_check_canary2() returns int language sql security definer as 'select 1'")
        bootstrapped.execute("grant temporary on database postgres to public")
        found, _ = checker.problems(bootstrapped)
        assert any('nexloop_check_canary()' in f and 'pg_temp' in f for f in found)
        assert any('nexloop_check_canary2()' in f and 'without search_path' in f for f in found)
        assert any('PUBLIC has TEMPORARY' in f for f in found)
        raise psycopg.Rollback()  # leave the bootstrapped database as it was


@pytest.mark.parametrize('role', RUNTIME_LOGIN_ROLES)
def test_runtime_roles_cannot_create_temporary_objects(bootstrapped, pg, role):
    with psycopg.connect(make_conninfo(pg, user=role), autocommit=True) as c:
        for statement in TEMP_OBJECTS:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(statement)
        assert c.execute("select to_regnamespace('pg_temp') is null or pg_my_temp_schema()=0").fetchone()[0]


def test_owner_keeps_temporary_for_its_own_definer_code(bootstrapped):
    assert bootstrapped.execute("select has_database_privilege('nexloop_owner',current_database(),'TEMPORARY')").fetchone()[0]
    with bootstrapped.transaction():
        bootstrapped.execute('set local role nexloop_owner')
        bootstrapped.execute('create temp table owner_scratch(x int) on commit drop')


def test_every_pinned_routine_keeps_its_other_settings(bootstrapped):
    rows = bootstrapped.execute("""select p.proconfig from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname in ('authz','control','ontology','runtime') and p.proconfig is not null""").fetchall()
    assert rows and all(any(c == 'search_path=pg_catalog, pg_temp' for c in config) for (config,) in rows)
    assert any(any(c.startswith('row_security=') for c in config) for (config,) in rows)  # untouched
