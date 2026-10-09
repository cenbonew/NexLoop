"""0099: temporary-schema exposure is closed for the NexLoop lineage (disposable PG only).

Owner-run routines pin search_path with pg_temp explicitly last, and no runtime role
may create temporary objects, so names inside owner-run code cannot resolve to a
session's temporary schema. scripts/check_definer_search_path.py enforces the same
invariants in local CI.
"""
import importlib.util
import pathlib
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from nexloop_eios.bootstrap import bootstrap

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_definer_search_path', ROOT / 'scripts' / 'check_definer_search_path.py')
check = importlib.util.module_from_spec(spec); spec.loader.exec_module(check)


def test_bootstrapped_lineage_has_no_temporary_schema_exposure(admin):
    bootstrap(admin)
    found, summary = check.problems(admin)
    assert found == []
    assert summary['routines_with_search_path'] > 0 and summary['security_definer'] > 0


@pytest.mark.parametrize('role', check.RUNTIME_ROLES)
def test_runtime_roles_cannot_create_temporary_objects(admin, pg, role):
    bootstrap(admin)
    row = admin.execute('select rolcanlogin from pg_catalog.pg_roles where rolname=%s', (role,)).fetchone()
    assert row is not None, role
    assert admin.execute("select has_database_privilege(%s,current_database(),'TEMPORARY')", (role,)).fetchone()[0] is False
    if not row[0]:
        return  # NOLOGIN group role: the privilege check above is the whole guarantee
    with psycopg.connect(make_conninfo(pg, user=role), autocommit=True) as app:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute('create temp table synthetic_temp_probe(x int)')


def test_check_flags_a_routine_without_pg_temp_last(admin):
    """Positive control: the static check reports a regressed routine and a re-granted PUBLIC TEMP."""
    bootstrap(admin)
    database = admin.execute('select current_database()').fetchone()[0]
    admin.execute('set role nexloop_owner')
    try:
        admin.execute("create function runtime.synthetic_path_regression() returns integer language sql "
                      "security definer set search_path = pg_catalog as 'select 1'")
    finally:
        admin.execute('reset role')
    admin.execute(f'grant temporary on database "{database}" to public')
    found, _ = check.problems(admin)
    assert any('synthetic_path_regression' in line for line in found)
    assert any('PUBLIC has TEMPORARY' in line for line in found)
