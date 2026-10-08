from support.release_metadata import BOOTSTRAP_REVISION
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.sandbox_profile import publish_test_artifact_identity


def test_actual_one_shot_sandbox_cli_bootstraps_smokes_and_retires_owned_pg():
    pg_bin=os.environ.get('NEXLOOP_TEST_PG_BIN','/opt/homebrew/opt/postgresql@18/bin')
    child=subprocess.run([sys.executable,'-m','nexloop_eios.core_sandbox','--mode','test',
        '--pg-bin',pg_bin],capture_output=True,text=True,timeout=60)
    assert child.returncode==0,'owned core sandbox failed'
    report=json.loads(child.stdout)
    assert report['passed'] and not report['product_ready'] and report['model_validation']=='not_run'
    assert report['shutdown_confirmed'] and report['cleanup_confirmed']
    assert report['catalog']['revision']==BOOTSTRAP_REVISION
    assert report['smoke']['passed'] and report['smoke']['artifact']['world']=='test'
    assert report['doctor']['foundation_checks_passed']
    assert 'token' not in report and 'dsn' not in report


def test_sandbox_profile_never_overwrites_an_existing_identity(admin):
    bootstrap(admin);token=publish_test_artifact_identity(admin)
    assert len(token)>=32
    before=admin.execute('select count(*) from authz.nexloop_authority_facts').fetchone()[0]
    with pytest.raises(PermissionError):publish_test_artifact_identity(admin)
    assert admin.execute('select count(*) from authz.nexloop_service_credentials').fetchone()[0]==1
    assert admin.execute('select count(*) from authz.nexloop_authority_facts').fetchone()[0]==before


def test_missing_pg_binaries_create_no_sandbox(tmp_path,monkeypatch):
    import nexloop_eios.core_sandbox as module
    calls=[]
    def forbidden(**kwargs):calls.append(True);raise AssertionError('must not create a root')
    monkeypatch.setattr(module.tempfile,'mkdtemp',forbidden)
    report=module.run_sandbox(pg_bin=tmp_path/'missing')
    assert not report['passed'] and calls==[]
