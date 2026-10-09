import json
import os
from pathlib import Path
import secrets
import subprocess
import pytest
import yaml
from nexloop_eios.compose_bootstrap import initialize_test_profile
from test_compose_bootstrap import settings

ROOT=Path(__file__).resolve().parents[1]


def test_container_check_job_uses_real_core_and_ignores_model_key(admin,pg,tmp_path,monkeypatch):
    import nexloop_eios.container_entrypoint as entry
    options=settings(pg,tmp_path);initialize_test_profile(admin,**options)
    real_path=Path
    def path(value):
        return {'/private/config':options['config_root'],'/var/lib/nexloop/artifacts':options['artifact_root']}.get(value,real_path(value))
    monkeypatch.setattr(entry,'Path',path)
    monkeypatch.setenv('MODEL_API_KEY','synthetic-unread-model-key')
    report=entry.check_container()
    assert report['passed'] and not report['product_ready'] and report['model_validation']=='not_run'
    assert report['doctor']['details']['model']['provider']=='test'
    assert 'synthetic-unread-model-key' not in json.dumps(report)
    assert admin.execute("select count(*) from runtime.nexloop_local_artifacts where world='test' and status='available'").fetchone()[0]==1
    assert admin.execute("select rolpassword like 'SCRAM-SHA-256$%' from pg_authid where rolname='nexloop_api'").fetchone()[0]


def test_compose_client_render_has_no_host_ports_admin_leak_or_env_loading(tmp_path):
    private=tmp_path/'inputs';private.mkdir(mode=0o700)
    for name in ['pg_bootstrap_password','bootstrap_dsn']:
        f=private/name;f.write_text('synthetic-contract-only');f.chmod(0o600)
    refs=tmp_path/'refs.env';refs.write_text('NEXLOOP_TEST_PROJECT=nexloop-contract-only\nPOSTGRES_IMAGE=docker.io/pgvector/pgvector@sha256:'+'a'*64+'\nVALKEY_IMAGE=docker.io/valkey/valkey@sha256:'+'b'*64+'\nBACKEND_IMAGE=nexloop-core-test:contract-only\nHOST_IMAGE=nexloop-host-test:contract-only\nWEB_PORT=38443\nSECRET_DIR='+str(private)+'\n')
    result=subprocess.run(['docker','compose','--env-file',str(refs),'-f',str(ROOT/'deploy/community/compose.test.yaml'),'config','--format','json'],
        capture_output=True,text=True,timeout=20,env=os.environ|{'MODEL_API_KEY':'synthetic-never-read'})
    assert result.returncode==0,'Compose client configuration validation failed'
    config=json.loads(result.stdout)
    assert config['networks']['core_test']['internal'] is True
    assert set(config['services']['api-test']['networks'])=={'core_test','local_web'}
    assert set(config['services']['postgres']['networks'])=={'core_test'}
    assert set(config['services']['valkey']['networks'])=={'core_test'}
    for name,service in config['services'].items():
        assert not service.get('privileged')
        if name!='api-test':assert not service.get('ports')
    assert config['services']['api-test']['ports'][0]['host_ip']=='127.0.0.1'
    assert config['services']['api-test']['ports'][0]['target']==8443
    assert not config['services']['check-test'].get('secrets')
    assert config['services']['check-test']['user']=='10001:10001'
    assert config['services']['bootstrap']['user']=='0:0'
    api=config['services']['api-test']
    assert api['user']=='10001:10001' and not api.get('secrets')
    assert api['command']==['api-test'] and api['read_only']
    assert config['services']['check-test']['network_mode']=='service:api-test'
    assert config['services']['check-test']['command']==['check-http-test']
    assert 'synthetic-never-read' not in result.stdout and 'synthetic-contract-only' not in result.stdout
    assert config['services']['postgres']['environment']['PGDATA']=='/var/lib/postgresql/18/docker'


def test_build_context_contains_only_wheel_lock_export_and_docker_files():
    import importlib.util
    spec=importlib.util.spec_from_file_location("prepare_community_build",ROOT/"scripts/prepare_community_build.py")
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    prepare=module.prepare
    output=ROOT/'.ci-results'/('community-contract-'+secrets.token_hex(12))
    report=prepare(output)
    assert not report['container_built']
    assert {p for p in report['sha256'] if not p.startswith('web/')}=={'.dockerignore','Dockerfile','requirements.txt','nexloop_eios_core-0.1.0-py3-none-any.whl'}
    assert 'web/index.html' in report['sha256'] and any(p.endswith('.js') for p in report['sha256'])
    assert '--hash=sha256:' in (output/'requirements.txt').read_text()
    assert 'MODEL_API_KEY' not in (output/'requirements.txt').read_text()
    with pytest.raises(ValueError):prepare(output)


def test_root_docker_context_is_fail_closed():
    assert (ROOT/'.dockerignore').read_text().splitlines()[1]=='**'
    source=yaml.safe_load((ROOT/'deploy/community/compose.test.yaml').read_text())
    assert set(source['services'])=={'postgres','bootstrap','api-test','check-test','cache-bootstrap','valkey','host-bootstrap','host-test','browser-bootstrap','worker-bootstrap','worker-test','outbound-recorder',
        'claim-extraction-scheduler','claim-extraction-worker','claim-matcher','recall-indexer'}
    assert source['services']['bootstrap']['secrets']==['bootstrap_dsn']
    assert 'pg_bootstrap_password' not in source['services']['check-test'].get('secrets',[])
    assert source['services']['valkey']['user']=='10001:10001'
    assert source['services']['cache-bootstrap']['secrets']==['cache-cert.pem','cache-key.pem','cache.acl','cache-credentials.json']
    assert not source['services']['valkey'].get('secrets')
    assert not any('cache_server' in v for v in source['services']['check-test']['volumes'])
    host=source['services']['host-test'];assert host['user']=='10001:10001' and host['network_mode']=='service:api-test'
    assert not host.get('secrets') and host['read_only']
    assert not any('core_config' in v or 'cache_' in v or 'artifacts' in v for v in host['volumes'])
    assert source['services']['host-bootstrap']['secrets']==['host-cert.pem','host-key.pem','host-control.key']

    assert source['services']['browser-bootstrap']['secrets']==['bootstrap_dsn','browser-input.json','browser-cert.pem','browser-key.pem']
    assert not any('browser_server' in v for v in source['services']['check-test']['volumes'])


def test_worker_is_separately_provisioned_and_not_mounted_by_api_or_host():
    source=yaml.safe_load((ROOT/'deploy/community/compose.test.yaml').read_text())
    worker=source['services']['worker-test']
    assert worker['user']=='10001:10001' and worker['networks']==['core_test']
    assert not worker.get('secrets') and worker['volumes']==['worker_config:/private/worker:ro','artifacts:/var/lib/nexloop/artifacts']
    assert source['services']['worker-bootstrap']['secrets']==['bootstrap_dsn']
    assert source['services']['check-test']['depends_on']['worker-test']['condition']=='service_completed_successfully'
    for name in ['api-test','host-test','check-test']:
        assert not any('worker_config' in value for value in source['services'][name]['volumes'])


def test_outbound_recorder_is_opt_in_restricted_and_not_mounted_elsewhere():
    """NX-047: opt-in profile; read-only private volume; never shares material with API/Host/worker."""
    source=yaml.safe_load((ROOT/'deploy/community/compose.test.yaml').read_text())
    recorder=source['services']['outbound-recorder']
    assert recorder['profiles']==['outbound'] and recorder['command']==['outbound-recorder'] and recorder['user']=='10001:10001'
    assert recorder['networks']==['core_test'] and not recorder.get('secrets') and not recorder.get('ports')
    assert recorder['volumes']==['outbound_config:/private/outbound:ro','artifacts:/var/lib/nexloop/artifacts']
    assert recorder['read_only'] is True and recorder['cap_drop']==['ALL'] and recorder['security_opt']==['no-new-privileges:true']
    for name,service in source['services'].items():
        if name!='outbound-recorder':assert not any('outbound_config' in v for v in service.get('volumes',[]))
    assert all(not service.get('profiles') for name,service in source['services'].items() if name not in ('outbound-recorder',)+BACKGROUND)


BACKGROUND=('claim-extraction-scheduler','claim-extraction-worker','claim-matcher','recall-indexer')


def test_background_services_are_opt_in_restricted_and_not_mounted_elsewhere():
    """NX-019/020/021: opt-in profile; read-only private volume; never shares material with API/Host/worker/recorder."""
    source=yaml.safe_load((ROOT/'deploy/community/compose.test.yaml').read_text())
    for name in BACKGROUND:
        service=source['services'][name]
        assert service['profiles']==['background'] and service['command']==[name] and service['user']=='10001:10001'
        assert service['networks']==['core_test'] and not service.get('secrets') and not service.get('ports')
        assert service['volumes']==['background_config:/private/background:ro','artifacts:/var/lib/nexloop/artifacts']
        assert service['read_only'] is True and service['cap_drop']==['ALL'] and service['security_opt']==['no-new-privileges:true']
    for name,service in source['services'].items():
        if name not in BACKGROUND:assert not any('background_config' in v for v in service.get('volumes',[]))
    assert 'background_config' in source['volumes']
