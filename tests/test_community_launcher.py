import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]
# Entry scripts are executable modules, not application packages.
def launcher():
    import sys
    spec=importlib.util.spec_from_file_location('prepare_community_build',ROOT/'scripts/prepare_community_build.py')
    prepare=importlib.util.module_from_spec(spec);spec.loader.exec_module(prepare)
    sys.modules['prepare_community_build']=prepare
    spec=importlib.util.spec_from_file_location('prepare_host_build',ROOT/'scripts/prepare_host_build.py')
    host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host);sys.modules['prepare_host_build']=host
    spec=importlib.util.spec_from_file_location('community_test',ROOT/'scripts/community_test.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

@pytest.fixture
def project():
    module=launcher();output=ROOT/'.ci-results'/('launcher-test-'+secrets.token_hex(12))
    report=module.prepare_project(output)
    return module,output,report


def test_private_inputs_consistent_and_actual_compose_client_renders(project):
    module,output,report=project
    assert module.verify_project(output)==report
    for name in ['compose.env','manifest.json','secrets/pg_bootstrap_password','secrets/bootstrap_dsn']:
        assert (output/name).stat().st_mode&0o777==0o600
    assert output.stat().st_mode&0o777==0o700
    password=(output/'secrets/pg_bootstrap_password').read_text()
    assert password not in json.dumps(report)
    result=subprocess.run(['docker','compose','--env-file',str(output/'compose.env'),'-f',str(ROOT/'deploy/community/compose.test.yaml'),'config','--format','json'],capture_output=True,text=True,timeout=20)
    assert result.returncode==0
    config=json.loads(result.stdout)
    assert config['name']==report['project'] and config['services']['postgres']['image']==report['postgres_image']
    lock=json.loads((ROOT/'versions.lock.json').read_text())['images']['postgres']
    # The recall migration needs pgvector: the compose PostgreSQL is the locked pgvector PG18 index digest.
    assert lock['reference'].startswith('docker.io/pgvector/pgvector:') and lock['reference'].endswith('-pg18-bookworm')
    assert report['postgres_image']=='docker.io/pgvector/pgvector@'+lock['digest']
    assert password not in result.stdout
    with pytest.raises(ValueError):module.prepare_project(output)


@pytest.mark.parametrize('mutation',['world-dsn','mode','context-extra','references','context-symlink','cache-cert','cache-mode','host-context','host-key-mode'])
def test_refuses_modified_private_or_build_inputs(project,mutation,tmp_path):
    module,output,report=project
    if mutation=='world-dsn':(output/'secrets/bootstrap_dsn').write_text('host=other dbname=production user=admin')
    elif mutation=='mode':(output/'secrets/pg_bootstrap_password').chmod(0o644)
    elif mutation=='context-extra':(output/'build/.env').write_text('synthetic-never-build')
    elif mutation=='references':(output/'compose.env').write_text('SECRET_DIR=/other')
    elif mutation=='host-context':(output/'host-build/agent-host-main.js').write_text('modified')
    elif mutation=='host-key-mode':(output/'secrets/host-key.pem').chmod(0o644)
    elif mutation=='cache-cert':(output/'secrets/cache-cert.pem').write_text('tampered certificate')
    elif mutation=='cache-mode':(output/'secrets/cache-key.pem').chmod(0o644)
    else:
        original=output/'build/Dockerfile';copy=tmp_path/'Dockerfile';copy.write_bytes(original.read_bytes());original.unlink();original.symlink_to(copy)
    with pytest.raises(ValueError):module.verify_project(output)


def test_existing_project_resources_refused_before_build(project,monkeypatch):
    module,output,report=project;calls=[]
    monkeypatch.setenv("MODEL_API_KEY","synthetic-unused");monkeypatch.setenv("COMPOSE_FILE","synthetic-override")
    def fake(args,**kwargs):
        assert "MODEL_API_KEY" not in kwargs["env"] and "COMPOSE_FILE" not in kwargs["env"]
        calls.append(args)
        return subprocess.CompletedProcess(args,0,'existing-owned-container\n' if args[1]=='ps' else 'Docker-version\n','')
    monkeypatch.setattr(module.subprocess,'run',fake)
    with pytest.raises(ValueError):module.run_project(output)
    assert not any('build' in args or 'up' in args for args in calls)


def test_failure_cli_sanitizes_and_does_not_create_invalid_output(tmp_path):
    result=subprocess.run(['uv','run','--frozen','python','scripts/community_test.py','prepare','--output',str(tmp_path/'outside')],cwd=ROOT,capture_output=True,text=True,timeout=20,env=os.environ|{'MODEL_API_KEY':'synthetic-never-read'})
    assert result.returncode==1 and 'synthetic-never-read' not in result.stdout+result.stderr
    assert not (tmp_path/'outside').exists() and json.loads(result.stdout)['secrets_logged'] is False


@pytest.mark.parametrize('http_evidence',['valid','missing','failed','cache_missing','cache_failed','host_missing','host_failed','browser_missing','browser_failed','worker_missing','worker_failed'])
def test_reads_complete_multiline_container_report(project, monkeypatch, http_evidence):
    module, output, report = project
    calls = []
    check = {"passed": True, "product_ready": False, "model_validation": "not_run",
             "smoke": {"passed": True}, "doctor": {"foundation_checks_passed": True},
             "http_checks": {key:True for key in ['liveness','foundation_ready_product_unready','anonymous_denied','wrong_service_denied','authorized_artifact_read','api_host_control']}}
    check['cache']={'passed':True,'cache_authoritative':False,'checks':{key:True for key in ['default_user_denied','authenticated','ping','cache_write','cache_read','foreign_key_denied','admin_command_denied','wrong_password_denied','unknown_ca_denied','wrong_hostname_denied']}}
    check['host']={'passed':True,'business_authority':False,'checks':{key:True for key in ['reachable','authenticated','owner_lock','product_unready','anonymous_denied','wrong_key_denied','browser_origin_denied']}}
    check['browser']={'passed':True,'business_action_grants':False,'synthetic_identity':True,'checks':{key:True for key in ['frontend_html','frontend_asset','frontend_no_credentials','wrong_password_denied','missing_origin_denied','login','secure_cookie','session_reload','human_cookie_not_service_authority','wrong_csrf_denied','logout','logged_out_session_denied']}}
    if http_evidence=='browser_missing':check.pop('browser')
    elif http_evidence=='browser_failed':check['browser']['checks']['human_cookie_not_service_authority']=False
    if http_evidence=='host_missing':check.pop('host')
    elif http_evidence=='host_failed':check['host']['checks']['owner_lock']=False
    if http_evidence=='cache_missing':check.pop('cache')
    elif http_evidence=='cache_failed':check['cache']['checks']['unknown_ca_denied']=False
    if http_evidence=='missing':check.pop('http_checks')
    elif http_evidence=='failed':check['http_checks']['authorized_artifact_read']=False
    worker={'passed':True,'mode':'test','world':'test','product_ready':False,'business_action_grants':False,'checks':{key:True for key in ['restricted_role','governed_orphan_removed','durable_resume_idempotent']}}
    if http_evidence=='worker_missing':worker.pop('checks')
    elif http_evidence=='worker_failed':worker['checks']['governed_orphan_removed']=False
    def fake(args, **kwargs):
        calls.append(args)
        if args[1] == 'logs': value = json.dumps(worker if args[2]=='owned-worker-container' else check, indent=2) + '\n'
        elif args[1] == 'inspect': value = '0\n'
        elif args[1] == 'compose' and 'ps' in args: value = 'owned-worker-container\n' if 'worker-test' in args else 'owned-check-container\n'
        else: value = ''
        return subprocess.CompletedProcess(args, 0, value, '')
    monkeypatch.setattr(module.subprocess, 'run', fake)
    if http_evidence!='valid':
        with pytest.raises(RuntimeError):module.run_project(output)
    else:
        result = module.run_project(output)
        assert result['core_test_runtime_verified'] and result['actual_api_http_verified'] and not result['product_ready']
    assert any(args[1] == 'logs' for args in calls)


def test_provider_environment_not_inherited_by_disposable_compose(monkeypatch,tmp_path):
    module=launcher();calls=[]
    names=['MODEL_API_KEY','MODEL_CREDENTIALS_FILE','EMBEDDING_API_KEY','EMBEDDING_MODEL',
           'EMBEDDING_MODE','EMBEDDING_LOCATION','EMBEDDING_DIMENSION','EMBEDDING_CREDENTIALS_FILE','COMPOSE_FILE']
    for name in names:monkeypatch.setenv(name,'synthetic-unused-provider-value')
    monkeypatch.setattr(module,'verify_project',lambda output:{'project':'isolated-filter-test'})
    def subprocess_boundary(args,**kwargs):
        assert not set(names)&set(kwargs['env'])
        calls.append(args)
        return subprocess.CompletedProcess(args,0,'existing-owned-container\n' if args[1]=='ps' else 'Docker-version\n','')
    monkeypatch.setattr(module.subprocess,'run',subprocess_boundary)
    with pytest.raises(ValueError):module.run_project(tmp_path)
    assert len(calls)==2 and not any('build' in args or 'up' in args for args in calls)
