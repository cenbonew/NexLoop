from support.release_metadata import BOOTSTRAP_REVISION
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from psycopg.conninfo import conninfo_to_dict,make_conninfo
from nexloop_eios.compose_bootstrap import initialize_test_profile,KEY_ID
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.artifact_smoke import run_smoke


def settings(pg,tmp_path):
    fields=conninfo_to_dict(pg)
    return dict(config_root=tmp_path/'config',artifact_root=tmp_path/'artifacts',host=fields['host'],port=int(fields['port']),dbname=fields['dbname'])


def test_fresh_profile_real_restricted_artifact_smoke_and_readonly_reopen(admin,pg,tmp_path):
    options=settings(pg,tmp_path)
    first=initialize_test_profile(admin,**options)
    assert first['passed'] and not first['already_initialized'] and first['catalog']['revision']==BOOTSTRAP_REVISION
    config=options['config_root']
    assert set(p.name for p in config.iterdir())=={'api_dsn','service_credential','artifact_key','profile.json'}
    for p in config.iterdir():assert p.stat().st_mode&0o777==0o600
    assert config.stat().st_mode&0o777==0o700
    assert options['artifact_root'].stat().st_mode&0o777==0o700
    smoke=run_smoke(database_url_file=config/'api_dsn',service_credential_file=config/'service_credential',
        signing_key_file=config/'artifact_key',signing_key_id=KEY_ID,artifact_root=options['artifact_root'])
    assert smoke['passed'] and smoke['artifact']['world']=='test'
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in config.iterdir()}
    counts=admin.execute('select (select count(*) from authz.nexloop_service_credentials),(select count(*) from authz.nexloop_authority_facts),(select count(*) from runtime.nexloop_local_artifacts)').fetchone()
    second=initialize_test_profile(admin,**options)
    assert second['already_initialized'] and second['passed']
    assert hashes=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in config.iterdir()}
    assert counts==admin.execute('select (select count(*) from authz.nexloop_service_credentials),(select count(*) from authz.nexloop_authority_facts),(select count(*) from runtime.nexloop_local_artifacts)').fetchone()


def test_existing_catalog_without_matching_config_is_not_seeded(admin,pg,tmp_path):
    bootstrap(admin)
    with pytest.raises((PermissionError,FileNotFoundError)):initialize_test_profile(admin,**settings(pg,tmp_path))
    assert admin.execute('select count(*) from control.nexloop_tenants').fetchone()[0]==0
    assert not (tmp_path/'config').exists()


def test_foreign_database_is_refused_before_filesystem_change(admin,pg,tmp_path):
    admin.execute('create table public.synthetic_existing_configuration(value integer)')
    with pytest.raises(PermissionError,match='fresh empty'):initialize_test_profile(admin,**settings(pg,tmp_path))
    assert not (tmp_path/'config').exists()
    assert admin.execute("select to_regclass('control.schema_migrations')").fetchone()[0] is None


def test_changed_private_config_and_new_endpoint_are_refused(admin,pg,tmp_path):
    options=settings(pg,tmp_path);initialize_test_profile(admin,**options)
    with pytest.raises(PermissionError,match='endpoints'):initialize_test_profile(admin,**(options|{'host':'different-host'}))
    f=options['config_root']/'service_credential';original=f.read_bytes();f.write_bytes(b'changed')
    with pytest.raises(PermissionError,match='changed'):initialize_test_profile(admin,**options)
    assert f.read_bytes()==b'changed' and original!=b'changed'


def test_cli_errors_are_sanitized_and_do_not_read_model_env(tmp_path):
    sentinel='synthetic-not-a-real-secret';dsn=tmp_path/'dsn';dsn.write_text('invalid='+sentinel);dsn.chmod(0o600)
    child=subprocess.run([sys.executable,'-I','-m','nexloop_eios.compose_bootstrap','--mode','test','--database-url-file',str(dsn),
        '--config-root',str(tmp_path/'config'),'--artifact-root',str(tmp_path/'artifacts'),'--postgres-host','postgres','--postgres-port','5432','--postgres-db','nexloop_test'],
        capture_output=True,text=True,timeout=10,env=os.environ|{'MODEL_API_KEY':sentinel})
    assert child.returncode==1 and sentinel not in child.stdout+child.stderr
    assert not json.loads(child.stdout)['passed'] and not (tmp_path/'config').exists()
