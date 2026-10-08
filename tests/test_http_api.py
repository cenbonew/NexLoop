from datetime import UTC,datetime,timedelta
from fastapi.testclient import TestClient
import pytest
from nexloop_eios.http_api import ApiConfiguration,create_app
from nexloop_eios.backend import open_backend
from nexloop_eios.compose_bootstrap import initialize_test_profile
from test_compose_bootstrap import settings

@pytest.fixture
def api(admin,pg,tmp_path):
    options=settings(pg,tmp_path);initialize_test_profile(admin,**options)
    root=options['config_root']
    config=ApiConfiguration(root/'api_dsn',root/'artifact_key',options['artifact_root'],'compose-test-v1')
    return config,root,options


def test_missing_private_configuration_keeps_liveness_but_not_readiness(tmp_path):
    config=ApiConfiguration(tmp_path/'missing-dsn',tmp_path/'missing-key',tmp_path/'never-create','absent')
    with TestClient(create_app(config)) as client:
        assert client.get('/health/live').status_code==200
        assert client.get('/health/ready').json()['missing_capabilities']==['browser_session','host_dispatch']
        ready=client.get('/health/ready');assert ready.status_code==503 and not ready.json()['foundation']['foundation_ready']
        assert client.get('/api/v1/artifacts/missing').status_code==401
    assert not config.artifact_root.exists()


def test_real_backend_health_rechecks_revocation(api,admin):
    config,root,options=api
    with TestClient(create_app(config)) as client:
        report=client.get('/health/ready').json()
        assert report['foundation']['foundation_ready'] and not report['ready']
        admin.execute("update authz.nexloop_authority_signing_keys set active=false")
        report=client.get('/health/ready').json()
        assert not report['foundation']['foundation_ready'] and not report['foundation']['checks']['signer']
        assert client.get('/health/live').status_code==200


def test_http_artifact_uses_actual_service_permission_and_revocation(api,admin):
    config,root,options=api
    token=(root/'service_credential').read_text()
    with open_backend(database_url=(root/'api_dsn').read_text(),artifact_root=config.artifact_root,
        signing_key_file=config.signing_key_file,signing_key_id=config.signing_key_id) as backend:
        ref=backend.authenticate(token,world='test').put_artifact(request_id='synthetic-http-artifact-001',
            payload=b'http-governed-test',media_type='text/plain',retention_until=datetime.now(UTC)+timedelta(hours=1))
    with TestClient(create_app(config)) as client:
        route='/api/v1/artifacts/'+ref.artifact_id
        assert client.get(route).status_code==401
        response=client.get(route,headers={'Authorization':'Bearer '+token})
        assert response.status_code==200 and response.content==b'http-governed-test'
        assert response.headers['cache-control']=='no-store'
        admin.execute("update authz.nexloop_service_credentials set status='revoked'")
        response=client.get(route,headers={'Authorization':'Bearer '+token})
        assert response.status_code==401 and token not in response.text
        assert client.get('/session').status_code==404


def test_artifact_directory_permission_drift_not_ready(api):
    config,root,options=api
    with TestClient(create_app(config)) as client:
        assert client.get('/health/ready').json()['foundation']['foundation_ready']
        config.artifact_root.chmod(0o755)
        try:assert not client.get('/health/ready').json()['foundation']['checks']['artifact_directory']
        finally:config.artifact_root.chmod(0o700)


def test_actual_local_http_process_and_owned_shutdown(api,admin):
    import httpx
    import sys
    import socket
    import subprocess
    import time
    config,root,options=api
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    child=subprocess.Popen([sys.executable,'-m','nexloop_eios.http_api','--mode','test',
        '--database-url-file',str(config.database_url_file),'--signing-key-file',str(config.signing_key_file),
        '--artifact-root',str(config.artifact_root),'--signing-key-id',config.signing_key_id,'--port',str(port)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+15
        while True:
            assert child.poll() is None,'owned API process exited before liveness'
            try:
                response=httpx.get(f'http://127.0.0.1:{port}/health/live',timeout=1,trust_env=False)
                if response.status_code==200:break
            except httpx.TransportError:pass
            assert time.monotonic()<deadline,'owned API failed to listen'
            time.sleep(.1)
        report=httpx.get(f'http://127.0.0.1:{port}/health/ready',timeout=5,trust_env=False)
        assert report.status_code==503 and report.json()['foundation']['foundation_ready']
    finally:
        if child.poll() is None:child.terminate()
        stdout,stderr=child.communicate(timeout=15)
    assert child.returncode in (0,-15)
    assert admin.execute("select count(*) from pg_stat_activity where usename='nexloop_api'").fetchone()[0]==0
    with pytest.raises(httpx.TransportError):httpx.get(f'http://127.0.0.1:{port}/health/live',timeout=1,trust_env=False)
    assert (root/'service_credential').read_text() not in stdout+stderr


def test_closed_backend_is_unavailable_without_false_authentication_error(api):
    config,root,options=api
    app=create_app(config)
    with TestClient(app) as client:
        app.state.backend._shutdown()
        response=client.get('/api/v1/artifacts/'+'a'*32,headers={'Authorization':'Bearer '+(root/'service_credential').read_text()})
        assert response.status_code==503 and response.json()['retryable']
        assert client.get('/health/live').status_code==200
