"""NX-026 settings: the deployment file is the seeded v1 (control schema, append-only, no application role access)."""
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from nexloop_eios.commitments import load_settings

ROOT=Path(__file__).resolve().parents[1]


def test_settings_file_is_the_seeded_v1_and_owner_only(admin,pg):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    assert admin.execute('select definition from control.nexloop_commitment_settings where version=1').fetchone()[0]==load_settings(ROOT/'deploy/configuration/commitments.v1.json')
    with pytest.raises(Exception,match='append-only'):admin.execute("update control.nexloop_commitment_settings set published_by='x'")
    admin.rollback() if admin.info.transaction_status else None
    for role in ('nexloop_api','nexloop_domain_worker','nexloop_action_worker','nexloop_configurator'):
        try:
            with psycopg.connect(make_conninfo(pg,user=role)) as db:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('select * from control.nexloop_commitment_settings')
        except psycopg.OperationalError:pass  # roles without login (configurator) cannot connect at all
