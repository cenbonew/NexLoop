import secrets
from fastapi.testclient import TestClient
import pytest
from nexloop_eios.http_api import ApiConfiguration,create_app
from nexloop_eios.browser_http import BrowserConfiguration,COOKIE
from test_browser_identity_reads import identity

ORIGIN='https://synthetic.example'
@pytest.fixture
def browser(identity,admin,tmp_path):
    dsn=tmp_path/'identity-dsn';dsn.write_text(identity[0]);dsn.chmod(0o600)
    key=tmp_path/'rate-key';key.write_text(secrets.token_hex(32));key.chmod(0o600)
    admin.execute("insert into control.nexloop_browser_rate_policies values('synthetic-a','local_login','nexloop_identity',3,60,true)")
    config=ApiConfiguration(tmp_path/'missing',tmp_path/'missing-key',tmp_path/'unused','missing',BrowserConfiguration(dsn,key,'synthetic-a','synthetic-browser-app',ORIGIN))
    with TestClient(create_app(config),base_url=ORIGIN) as client:yield client


def login(browser,identity,**changes):
    return browser.post('/api/v1/auth/login',headers={'Origin':ORIGIN},json={'username':identity[3].username,'password':identity[-1],**changes})


def test_real_http_cookie_session_logout_and_no_secret_echo(browser,identity):
    response=login(browser,identity);assert response.status_code==200
    cookie=response.headers['set-cookie'];assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie and 'Path=/' in cookie
    assert identity[-1] not in response.text
    assert browser.get('/api/v1/auth/session').json()['principal_id']==identity[2].principal_id
    csrf=response.json()['csrf_token']
    assert browser.post('/api/v1/auth/logout',headers={'Origin':ORIGIN,'X-CSRF-Token':'wrong'}).status_code==401
    assert browser.post('/api/v1/auth/logout',headers={'Origin':ORIGIN,'X-CSRF-Token':csrf}).status_code==200
    assert browser.get('/api/v1/auth/session').status_code==401
    assert browser.get('/health/ready').status_code==503


def test_origin_host_unknown_fields_and_rate_limit_denied(browser,identity,admin):
    body={'username':identity[3].username,'password':identity[-1]}
    assert browser.post('/api/v1/auth/login',json=body).status_code==403
    assert browser.post('/api/v1/auth/login',headers={'Origin':'https://other.example'},json=body).status_code==403
    assert browser.post('/api/v1/auth/login',headers={'Origin':ORIGIN,'Host':'other.example'},json=body).status_code==403
    assert admin.execute('select count(*) from control.nexloop_browser_evidence').fetchone()[0]==0
    assert login(browser,identity,tenant_id='other').status_code==400
    assert login(browser,identity,password='synthetic-wrong').status_code==401
    assert login(browser,identity).status_code==200
    assert login(browser,identity).status_code==429


def test_http_reauthentication_replaces_cookie_and_revoke_blocks_session(browser,identity,admin):
    assert login(browser,identity).status_code==200
    old=browser.cookies.get(COOKIE)
    assert login(browser,identity).status_code==200
    assert browser.cookies.get(COOKIE)!=old
    assert admin.execute("select count(*) from control.nexloop_browser_sessions where payload->'revoked_at' <> 'null'::jsonb").fetchone()[0]==1
    admin.execute('update control.nexloop_browser_applications set active=false')
    assert browser.get('/api/v1/auth/session').status_code==401


def test_reload_csrf_recovery_requires_origin_and_invalidates_old_csrf(browser,identity):
    response=login(browser,identity);old=response.json()['csrf_token'];cookie=browser.cookies.get(COOKIE)
    assert browser.post('/api/v1/auth/csrf').status_code==403
    assert browser.post('/api/v1/auth/csrf',headers={'Origin':'https://other.example'}).status_code==403
    refreshed=browser.post('/api/v1/auth/csrf',headers={'Origin':ORIGIN})
    assert refreshed.status_code==200 and refreshed.headers['cache-control']=='no-store'
    assert refreshed.json()['csrf_token']!=old and browser.cookies.get(COOKIE)==cookie
    assert browser.post('/api/v1/auth/logout',headers={'Origin':ORIGIN,'X-CSRF-Token':old}).status_code==401
    assert browser.post('/api/v1/auth/logout',headers={'Origin':ORIGIN,'X-CSRF-Token':refreshed.json()['csrf_token']}).status_code==200
    assert browser.post('/api/v1/auth/csrf',headers={'Origin':ORIGIN}).status_code==401


def test_disabled_policy_denies_login_without_authentication_writes(browser,identity,admin):
    admin.execute('update control.nexloop_browser_rate_policies set active=false')
    response=login(browser,identity)
    assert response.status_code==503 and identity[-1] not in response.text
    assert admin.execute('select count(*) from control.nexloop_browser_evidence').fetchone()[0]==0
    assert admin.execute('select count(*) from control.nexloop_browser_login_events').fetchone()[0]==0


def test_bounded_malformed_json_never_echoes_credentials(browser,identity):
    for body in ('{"password":"'+identity[-1]+'"', 'x'*8193):
        result=browser.post('/api/v1/auth/login',headers={'Origin':ORIGIN,'Content-Type':'application/json'},content=body)
        assert result.status_code==400 and identity[-1] not in result.text
    assert browser.post('/api/v1/auth/login',headers={'Origin':ORIGIN},content='plaintext').status_code==400


def test_invalid_private_key_keeps_browser_unavailable(identity,tmp_path):
    dsn=tmp_path/'identity-dsn';dsn.write_text(identity[0]);dsn.chmod(0o600)
    key=tmp_path/'invalid-key';key.write_text('not-a-key');key.chmod(0o600)
    config=ApiConfiguration(tmp_path/'missing',tmp_path/'missing-key',tmp_path/'unused','missing',BrowserConfiguration(dsn,key,'synthetic-a','synthetic-browser-app',ORIGIN))
    with TestClient(create_app(config),base_url=ORIGIN) as client:
        result=client.post('/api/v1/auth/login',headers={'Origin':ORIGIN},json={'username':identity[3].username,'password':identity[-1]})
        assert result.status_code==503 and identity[-1] not in result.text
        assert client.get('/health/live').status_code==200
    assert client.app.state.browser_store is None and client.app.state.browser_rate_key is None
