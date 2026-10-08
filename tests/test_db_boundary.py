import json
from pathlib import Path
import psycopg
from psycopg.conninfo import make_conninfo
import pytest
from nexloop_eios.bootstrap import bootstrap, verify

def test_application_roles_have_no_business_table_writes(admin,pg):
    bootstrap(admin)
    for role in ['nexloop_api','nexloop_domain_worker','nexloop_action_worker','nexloop_scheduler','nexloop_runtime']:
        with psycopg.connect(make_conninfo(pg,user=role),autocommit=True) as app:
            assert app.execute('select current_user').fetchone()[0]==role
            flags=app.execute('select rolsuper,rolbypassrls,rolcreaterole,rolcreatedb from pg_roles where rolname=current_user').fetchone()
            assert flags==(False,False,False,False)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app.execute("insert into ontology.objects(tenant_id,type_name,object_id,schema_version,properties,created_at,updated_at) values ('synthetic','Consumer','unauthorized',1,'{}',now(),now())")
            if role!='nexloop_runtime':assert verify(app)['revision']==json.loads((Path(__file__).resolve().parents[1]/'versions.lock.json').read_text())['packages']['nexloop-eios-core']['bootstrap_revision']
            else:
                with pytest.raises(psycopg.errors.InsufficientPrivilege): verify(app)
    assert admin.execute("select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname in ('ontology','runtime') and c.relkind='r' and (not c.relrowsecurity or not c.relforcerowsecurity)").fetchone()[0]==0


def test_new_owner_functions_are_not_implicitly_executable(admin,pg):
    bootstrap(admin)
    # DDL only, as migration owner; never write fixture business objects as admin.
    admin.execute('set role nexloop_owner')
    try:
        admin.execute("create function runtime.synthetic_default_acl_probe() returns integer language sql as 'select 1'")
    finally:
        admin.execute('reset role')
    for role in ['nexloop_api','nexloop_domain_worker','nexloop_action_worker','nexloop_scheduler','nexloop_runtime']:
        assert not admin.execute("select has_function_privilege(%s,'runtime.synthetic_default_acl_probe()','execute')",(role,)).fetchone()[0]
    assert admin.execute("select not rolcanlogin from pg_roles where rolname='nexloop_owner'").fetchone()[0]
    assert admin.execute("select count(*) from pg_auth_members m join pg_roles r on r.oid=m.roleid where r.rolname='nexloop_owner'").fetchone()[0]==0
