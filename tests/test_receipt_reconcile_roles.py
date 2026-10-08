"""Actual disposable PostgreSQL application-role recovery boundary."""
from pathlib import Path
import pytest
import psycopg
from psycopg.conninfo import make_conninfo
from nexloop_eios.bootstrap import bootstrap

@pytest.mark.parametrize('role',['nexloop_action_worker','nexloop_api','nexloop_domain_worker','nexloop_scheduler'])
def test_application_roles_cannot_mint_query_lineage_or_write_recovery(admin,pg,role):
    bootstrap(admin)
    with psycopg.connect(make_conninfo(pg,user=role),autocommit=True) as db:
        for table in ('nexloop_effect_query_admissions','nexloop_effect_recovery_audits'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('select * from runtime.'+table)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('delete from runtime.'+table)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("select authz.nexloop_receipt_reconcile_command('synthetic-not-credential','real','{}','synthetic-invalid','{}')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("select authz.nexloop_effect_execution_before_receipt_v0064('synthetic-not-credential','real','{}','synthetic-invalid','{}')")
