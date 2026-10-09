"""Non-editable distribution and actual PG smoke without workspace import paths."""
from support.release_metadata import BOOTSTRAP_REVISION
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import zipfile

import pytest
from psycopg.conninfo import make_conninfo

from authority_fixture import seed_authority
from eios.authz.operations import Operation

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(scope='session')
def installed_core_wheel(tmp_path_factory):
    owned=tmp_path_factory.mktemp('nexloop-wheel-install')
    subprocess.run(['uv','build','--package','nexloop-eios-core','--wheel','--offline',
        '--out-dir',str(owned/'wheels')],cwd=ROOT,check=True,capture_output=True,text=True,timeout=60)
    wheel=next((owned/'wheels').glob('*.whl'))
    requirements=owned/'requirements.txt'
    subprocess.run(['uv','export','--frozen','--offline','--no-dev','--no-emit-workspace',
        '--format','requirements-txt','--output-file',str(requirements)],cwd=ROOT,
        check=True,capture_output=True,text=True,timeout=30)
    environment=owned/'venv'
    subprocess.run(['uv','venv','--offline','--python',sys.executable,str(environment)],
        check=True,capture_output=True,text=True,timeout=30)
    python=environment/'bin/python'
    subprocess.run(['uv','pip','install','--python',str(python),'--offline','--no-deps',
        '--require-hashes','-r',str(requirements)],check=True,capture_output=True,text=True,timeout=60)
    subprocess.run(['uv','pip','install','--python',str(python),'--offline','--no-deps',str(wheel)],
        check=True,capture_output=True,text=True,timeout=30)
    return owned,python,wheel


def test_wheel_preserves_licenses_provenance_and_all_catalog_files(installed_core_wheel):
    _,_,wheel=installed_core_wheel
    with zipfile.ZipFile(wheel) as archive:
        names=archive.namelist()
        for name in ('LICENSE','NOTICE'):
            entry=next(x for x in names if x.endswith('.dist-info/licenses/'+name))
            assert archive.read(entry)==(ROOT/name).read_bytes()
        provenance=json.loads(archive.read('eios/PROVENANCE.json'))
        assert provenance==json.loads((ROOT/'packages/eios-core/PROVENANCE.json').read_text())
        for row in provenance['files']:
            path=row['destination'].removeprefix('packages/eios-core/src/')
            assert hashlib.sha256(archive.read(path)).hexdigest()==row['nexloop_sha256']
        catalog=json.loads(archive.read('eios/migrations/catalog.json'))
        for row in catalog['migrations']:
            assert hashlib.sha256(archive.read('eios/migrations/'+row['name'])).hexdigest()==row['sha256']
        assert not any(part in x for x in names for part in ('/tennis','/sports8','/tos/'))
        assert not any(x.endswith(('.pyc','.sqlite')) or '/.git/' in x for x in names)


def test_noneditable_wheel_bootstraps_and_runs_actual_pg_artifact_smoke(installed_core_wheel,admin,pg,tmp_path):
    owned,python,_=installed_core_wheel
    program='''
import json,pathlib,sys
import psycopg
import eios,nexloop_eios.bootstrap,nexloop_eios.artifact_smoke
root=pathlib.Path(sys.prefix).resolve()
for module in (eios,nexloop_eios.bootstrap,nexloop_eios.artifact_smoke):
    assert pathlib.Path(module.__file__).resolve().is_relative_to(root)
from nexloop_eios.bootstrap import bootstrap,verify
with psycopg.connect(sys.stdin.read(),autocommit=True) as c:
    bootstrap(c)
    print(json.dumps(verify(c)))
'''
    child=subprocess.run([str(python),'-I','-c',program],input=pg,
        cwd=owned,capture_output=True,text=True,timeout=30)
    assert child.returncode==0, 'installed wheel bootstrap failed'
    assert json.loads(child.stdout)['revision']==BOOTSTRAP_REVISION
    clean_environment={key:value for key,value in os.environ.items() if key not in ('PYTHONPATH','PYTHONHOME')}
    clean_environment['PYTHONNOUSERSITE']='1'
    console_commands=('nexloop-doctor','nexloop-artifact-smoke','nexloop-core-sandbox','nexloop-compose-bootstrap','nexloop-api','nexloop-runtime-worker','nexloop-effect-worker','nexloop-outbound-recorder')
    for command in console_commands:
        help_result=subprocess.run([str(python.parent/command),'--help'],cwd=owned,
            env=clean_environment,capture_output=True,text=True,timeout=15)
        assert help_result.returncode==0 and 'usage:' in help_result.stdout
    sandbox=subprocess.run([str(python.parent/'nexloop-core-sandbox'),'--mode','test',
        '--pg-bin',os.environ.get('NEXLOOP_TEST_PG_BIN','/opt/homebrew/opt/postgresql@18/bin')],
        cwd=owned,env=clean_environment,capture_output=True,text=True,timeout=60)
    assert sandbox.returncode==0,'installed wheel owned sandbox failed'
    sandbox_report=json.loads(sandbox.stdout)
    assert sandbox_report['passed'] and sandbox_report['shutdown_confirmed'] and sandbox_report['cleanup_confirmed']
    token,_=seed_authority(admin,'synthetic-a','eios:artifact:local_test',world='test',
        operations=(Operation.CREATE,Operation.READ,Operation.DELETE))
    material=secrets.token_bytes(32)
    admin.execute('insert into authz.nexloop_authority_signing_keys values(%s,%s,true)',('synthetic-wheel',material))
    paths={}
    for name,value in [('dsn',make_conninfo(pg,user='nexloop_api').encode()),('credential',token.encode()),('key',material)]:
        p=tmp_path/name;p.write_bytes(value);p.chmod(0o600);paths[name]=p
    smoke=subprocess.run([str(python),'-I','-m','nexloop_eios.artifact_smoke','--mode','test',
        '--database-url-file',str(paths['dsn']),'--service-credential-file',str(paths['credential']),
        '--signing-key-file',str(paths['key']),'--signing-key-id','synthetic-wheel',
        '--artifact-root',str(tmp_path/'blobs')],cwd=owned,capture_output=True,text=True,timeout=30)
    assert smoke.returncode==0,'installed wheel test-world smoke failed'
    report=json.loads(smoke.stdout)
    assert report['passed'] and not report['product_ready'] and report['artifact']['world']=='test'
    assert token not in smoke.stdout+smoke.stderr
    assert paths['dsn'].read_text() not in smoke.stdout+smoke.stderr
    assert material.hex() not in smoke.stdout+smoke.stderr
    assert admin.execute('select world,status from runtime.nexloop_local_artifacts').fetchall()==[('test','available')]
    evidence=ROOT/'.ci-results/wheel-install.json'
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text(json.dumps({'wheel_sha256':hashlib.sha256(installed_core_wheel[2].read_bytes()).hexdigest(),
        'noneditable_install':True,'isolated_module_origins_verified':True,'bootstrap_revision':BOOTSTRAP_REVISION,
        'actual_restricted_pg_artifact_smoke':True,'installed_console_help_passed':len(console_commands),
        'installed_sandbox_test_loop':True,
        'dependency_install':'offline, frozen exported requirements, require-hashes, no-deps'},indent=2)+'\n')
