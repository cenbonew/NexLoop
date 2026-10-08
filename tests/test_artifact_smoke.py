import hashlib
import json
import os
import secrets
import subprocess
import sys

import pytest
from psycopg.conninfo import make_conninfo

from authority_fixture import seed_authority
from eios.authz.operations import Operation
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.artifact_smoke import run_smoke, read_private_text, SMOKE_BYTES


def private_file(path,value):
    path.write_bytes(value if type(value) is bytes else value.encode())
    path.chmod(0o600)
    return path


@pytest.mark.parametrize('world',['test','real'])
def test_actual_smoke_cli_uses_restricted_pg_test_world_and_sanitized_result(admin,pg,tmp_path,world):
    bootstrap(admin)
    token,_=seed_authority(admin,'synthetic-a','eios:artifact:local_'+world,world=world,
        operations=(Operation.CREATE,Operation.READ,Operation.DELETE))
    material=secrets.token_bytes(32)
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',('synthetic-smoke',material))
    dsn=private_file(tmp_path/'dsn',make_conninfo(pg,user='nexloop_api'))
    credential=private_file(tmp_path/'credential',token)
    key=private_file(tmp_path/'signer',material)
    completed=subprocess.run([sys.executable,'-m','nexloop_eios.artifact_smoke','--mode','test',
        '--database-url-file',str(dsn),'--service-credential-file',str(credential),
        '--signing-key-file',str(key),'--signing-key-id','synthetic-smoke',
        '--artifact-root',str(tmp_path/'blobs')],capture_output=True,text=True,timeout=20)
    report=json.loads(completed.stdout)
    assert token not in completed.stdout+completed.stderr
    assert dsn.read_text() not in completed.stdout+completed.stderr
    assert material.hex() not in completed.stdout+completed.stderr
    if world=='real':
        assert completed.returncode==1 and not report['passed']
        assert report['checks']['backend_startup'] and not report['checks']['service_authentication']
        assert not report['checks']['artifact_write'] and 'artifact' not in report
        assert admin.execute('select count(*) from runtime.nexloop_local_artifacts').fetchone()[0]==0
        return
    assert completed.returncode==0, 'test-world smoke failed'
    assert report['passed'] and all(report['checks'].values())
    assert not report['product_ready'] and report['model_validation']=='not_run'
    assert report['artifact']['world']=='test'
    assert report['artifact']['sha256']==hashlib.sha256(SMOKE_BYTES).hexdigest()
    assert admin.execute('select world,status from runtime.nexloop_local_artifacts').fetchall()==[('test','available')]


def test_smoke_missing_configuration_fails_before_backend_or_directory(tmp_path,monkeypatch):
    import nexloop_eios.artifact_smoke as module
    def forbidden(**kwargs): raise AssertionError('must not open a backend')
    monkeypatch.setattr(module,'open_backend',forbidden)
    root=tmp_path/'absent'
    result=run_smoke(database_url_file=tmp_path/'missing',service_credential_file=tmp_path/'missing',
        signing_key_file=tmp_path/'missing',signing_key_id='unused',artifact_root=root)
    assert not result['passed'] and not any(result['checks'].values())
    assert not root.exists() and str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize('kind',['public','symlink','fifo','oversized','empty'])
def test_private_smoke_configuration_rejects_unsafe_files(tmp_path,kind):
    path=tmp_path/'configuration'
    if kind=='fifo':os.mkfifo(path,0o600)
    elif kind=='symlink':path.symlink_to(private_file(tmp_path/'target','synthetic'))
    else:
        private_file(path,'x'*65 if kind=='oversized' else '' if kind=='empty' else 'synthetic')
        if kind=='public':path.chmod(0o644)
    with pytest.raises((PermissionError,OSError,ValueError)):read_private_text(path,maximum=64)
