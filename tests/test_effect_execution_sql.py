"""Migration42 actual disposable PG ledger/role/RLS negative boundaries at the locked release head.

No provider call or synthetic grant is treated as execution authority.
"""
import json
from pathlib import Path
import pytest
import psycopg
from psycopg.conninfo import make_conninfo
from nexloop_eios.bootstrap import bootstrap,verify,catalog


def test_42_actual_bootstrap_and_publication_checksum(admin):
    locked_head=json.loads((Path(__file__).resolve().parents[1]/'versions.lock.json').read_text())['packages']['nexloop-eios-core']['bootstrap_revision']
    revisions=bootstrap(admin)
    assert revisions[-1]==locked_head and len(revisions)==int(locked_head)
    assert verify(admin)=={'lineage':'nexloop-eios-v1','revision':locked_head,'migration_count':int(locked_head)}
    assert len(catalog())==int(locked_head)
    assert bootstrap(admin)==[]
    rows=admin.execute("select relname,relrowsecurity,relforcerowsecurity from pg_class where relnamespace='runtime'::regnamespace and relname in ('nexloop_effect_attempts','nexloop_effect_observations','nexloop_effect_events') order by relname").fetchall()
    assert len(rows)==3 and all(enabled and forced for _,enabled,forced in rows)
    assert admin.execute("select has_function_privilege('nexloop_action_worker','authz.nexloop_effect_execution_command(text,text,text,text,text)','EXECUTE')").fetchone()[0]
    assert all(not admin.execute("select has_function_privilege(%s,'authz.nexloop_effect_execution_command(text,text,text,text,text)','EXECUTE')",(role,)).fetchone()[0]
        for role in ('nexloop_api','nexloop_domain_worker','nexloop_scheduler'))


@pytest.mark.parametrize('role',['nexloop_action_worker','nexloop_api','nexloop_domain_worker','nexloop_scheduler'])
def test_42_application_cannot_write_ledger_or_call_owner_helper(admin,pg,role):
    bootstrap(admin)
    with psycopg.connect(make_conninfo(pg,user=role),autocommit=True) as connection:
        for name in ('nexloop_effect_attempts','nexloop_effect_observations','nexloop_effect_events'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute('select * from runtime.'+name)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute('delete from runtime.'+name)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("select authz.nexloop_effect_assert_claim(null::runtime.nexloop_effect_intents,'{}','reserve','{}')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("select authz.nexloop_effect_execution_command('not-a-credential','real','{}','not-a-signature','{}')")
