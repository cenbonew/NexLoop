import json
import subprocess
import sys
import pytest
from psycopg.conninfo import make_conninfo
from test_final_artifact_orphans import orphan_collector,orphan_file,persist_plan
from test_postgres_artifacts import artifact_setup


def command(collector,token,pg,tmp_path,role='nexloop_domain_worker'):
    root=tmp_path/'worker-private';root.mkdir(mode=0o700)
    inputs={'dsn':make_conninfo(pg,user=role).encode(),'token':token.encode(),'key':collector.signer.material}
    for name,payload in inputs.items():
        path=root/name;path.write_bytes(payload);path.chmod(0o600)
    return [sys.executable,'-I','-m','nexloop_eios.domain_worker',
        '--database-url-file',str(root/'dsn'),'--signing-key-file',str(root/'key'),
        '--service-credential-file',str(root/'token'),'--artifact-root',str(collector.store.root),
        '--signing-key-id',collector.signer.key_id,'--world','real','--minimum-age-seconds','120']


def execute(args,token):
    result=subprocess.run(args,capture_output=True,text=True,timeout=30)
    assert token not in result.stdout+result.stderr
    assert 'password=' not in result.stdout+result.stderr
    return result,json.loads(result.stdout)


def test_actual_worker_process_bounded_page_protects_registered_artifact(orphan_collector,pg,admin,tmp_path):
    collector,token,formal=orphan_collector
    _,path=orphan_file(collector.store)
    result,report=execute(command(collector,token,pg,tmp_path),token)
    assert result.returncode==0 and report['passed'] and not report['product_ready']
    assert not path.exists() and (collector.store.root/formal.namespace/formal.artifact_id).exists()
    assert admin.execute('select status from runtime.nexloop_orphan_sweeps').fetchone()[0]=='finished'


def test_actual_worker_resumes_durable_plan_idempotently(orphan_collector,pg,admin,tmp_path):
    collector,token,_=orphan_collector;_,path=orphan_file(collector.store)
    plan=persist_plan(collector)
    args=command(collector,token,pg,tmp_path)+['--resume',plan['sweep_id']]
    first,a=execute(args,token);second,b=execute(args,token)
    assert first.returncode==second.returncode==0 and a==b and not path.exists()
    assert admin.execute('select count(*) from runtime.nexloop_orphan_sweeps').fetchone()[0]==1


@pytest.mark.parametrize('denial',['api-role','revoked-credential'])
def test_worker_denied_without_file_effect(orphan_collector,pg,admin,tmp_path,denial):
    collector,token,_=orphan_collector;_,path=orphan_file(collector.store)
    args=command(collector,token,pg,tmp_path,role='nexloop_api' if denial=='api-role' else 'nexloop_domain_worker')
    if denial=='revoked-credential':
        admin.execute("update authz.nexloop_service_credentials set status='revoked'")
    result,report=execute(args,token)
    assert result.returncode==1 and report=={'passed':False,'code':'maintenance_unavailable','product_ready':False}
    assert path.exists() and admin.execute('select count(*) from runtime.nexloop_orphan_sweeps').fetchone()[0]==0
