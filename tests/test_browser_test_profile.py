import json
import os
import secrets
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict
from nexloop_eios.compose_bootstrap import initialize_test_profile,KEY_ID
from nexloop_eios.browser_test_profile import initialize_browser_test_profile,TENANT,APPLICATION,USERNAME
from nexloop_eios.browser_http import BrowserConfiguration,COOKIE
from nexloop_eios.http_api import ApiConfiguration,create_app
from test_compose_bootstrap import settings


def options(pg,tmp_path):
    fields=conninfo_to_dict(pg)
    return dict(server_root=tmp_path/'browser-server',client_root=tmp_path/'browser-client',
                host=fields['host'],port=int(fields['port']),dbname=fields['dbname'],
                origin='https://localhost:8443',password=secrets.token_urlsafe(40))


def test_fresh_canonical_test_identity_actual_pg_login_no_business_grants(admin,pg,tmp_path):
    core=settings(pg,tmp_path);initialize_test_profile(admin,**core)
    opt=options(pg,tmp_path);report=initialize_browser_test_profile(admin,**opt)
    assert report['passed'] and report['synthetic_identity'] and not report['business_action_grants']
    server=opt['server_root'];client=opt['client_root']
    for root in [server,client]:
        assert root.stat().st_mode&0o777==0o700
        assert all(p.stat().st_mode&0o777==0o600 and p.stat().st_uid==os.getuid() for p in root.iterdir())
    assert set(p.name for p in client.iterdir())=={'realm.json','credentials.json'}
    assert 'identity_dsn' not in json.dumps(report) and opt['password'] not in json.dumps(report)
    conf=ApiConfiguration(core['config_root']/'api_dsn',core['config_root']/'artifact_key',core['artifact_root'],KEY_ID,
        BrowserConfiguration(server/'identity_dsn',server/'rate.key',TENANT,APPLICATION,opt['origin']))
    with TestClient(create_app(conf),base_url=opt['origin']) as http:
        ready=http.get('/health/ready')
        assert ready.status_code==503 and ready.json()['browser_session_available']
        assert ready.json()['missing_capabilities']==['host_dispatch']
        headers={'Origin':opt['origin']}
        assert http.post('/api/v1/auth/login',headers=headers,json={'username':USERNAME,'password':'incorrect-synthetic-password'}).status_code==401
        login=http.post('/api/v1/auth/login',headers=headers,json={'username':USERNAME,'password':opt['password']})
        assert login.status_code==200 and http.get('/api/v1/auth/session').status_code==200
        assert http.cookies.get(COOKIE)
        assert http.get('/api/v1/artifacts/not-granted').status_code==401 # Human cookie is not service authority.
        assert http.post('/api/v1/auth/logout',headers={**headers,'X-CSRF-Token':login.json()['csrf_token']}).status_code==200
        assert http.get('/api/v1/auth/session').status_code==401
        http.app.state.browser_store.pool.close()
        degraded=http.get('/health/ready').json()
        assert not degraded['browser_session_available']
        assert degraded['missing_capabilities']==['browser_session','host_dispatch']
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0
    assert admin.execute('select count(*) from control.nexloop_browser_sessions').fetchone()[0]==1
    assert admin.execute("select rolpassword like 'SCRAM-SHA-256$%' from pg_authid where rolname='nexloop_identity'").fetchone()[0]
    before={p.name:p.read_bytes() for p in server.iterdir()}
    with pytest.raises(PermissionError):initialize_browser_test_profile(admin,**opt)
    assert before=={p.name:p.read_bytes() for p in server.iterdir()}


@pytest.mark.parametrize('mutation',['foreign-origin','wrong-endpoint','foreign-directory'])
def test_bootstrap_refuses_foreign_configuration_before_auth_writes(admin,pg,tmp_path,mutation):
    initialize_test_profile(admin,**settings(pg,tmp_path));opt=options(pg,tmp_path)
    if mutation=='foreign-origin':opt['origin']='https://example.invalid:8443'
    elif mutation=='wrong-endpoint':opt['dbname']='foreign'
    else:opt['server_root'].mkdir();(opt['server_root']/'existing').write_text('preserve')
    with pytest.raises(PermissionError):initialize_browser_test_profile(admin,**opt)
    assert admin.execute('select count(*) from control.nexloop_browser_accounts').fetchone()[0]==0
    assert not opt['client_root'].exists()
