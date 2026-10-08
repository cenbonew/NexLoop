"""Fresh test-only canonical browser identity configuration, with no Action grants.

This configures an explicitly synthetic login account. It never impersonates a
real person or uses Human identity to authorize formal business mutations.
"""
import json
import os
from pathlib import Path
import secrets
import ssl
import stat
from urllib.parse import urlsplit
from argon2 import PasswordHasher
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.identity.models import Subject,SubjectKind,TenantMembership,MembershipKind,LocalAccount,EncodedPasswordHash
from nexloop_eios.bootstrap import verify
from nexloop_eios.compose_bootstrap import _write

TENANT='nexloop-sandbox'
APPLICATION='nexloop-test-browser'
USERNAME='nexloop-test-user'
SUBJECT='nexloop-test-human'
PRINCIPAL='nexloop-test-human-principal'
TABLES=('nexloop_browser_applications','nexloop_browser_subjects','nexloop_browser_memberships',
        'nexloop_browser_accounts','nexloop_browser_evidence','nexloop_browser_sessions')


def initialize_browser_test_profile(connection,*,server_root,client_root,host,port,dbname,origin,password,service_uid=None):
    uid=os.geteuid() if service_uid is None else service_uid
    if type(uid) is not int or uid<0 or (uid!=os.geteuid() and os.geteuid()!=0):raise PermissionError('test service owner required')
    u=urlsplit(origin)
    if u.scheme!='https' or u.hostname!='localhost' or u.port is None or not 1024<=u.port<=65535 or origin!=f'https://localhost:{u.port}':raise PermissionError('explicit local HTTPS test realm required')
    if type(password) is not str or not 32<=len(password)<=128 or any(c.isspace() for c in password):raise PermissionError('fresh random test password required')
    if connection.execute('select current_user,session_user,current_database()').fetchone()!=('nexloop_bootstrap','nexloop_bootstrap',dbname):raise PermissionError('trusted test bootstrap session required')
    if (host,int(port),dbname)!=(connection.info.host,connection.info.port,connection.info.dbname):raise PermissionError('exact isolated test endpoint required')
    verify(connection)
    if connection.execute("select count(*) from control.nexloop_tenants where tenant_id=%s and status='active'",(TENANT,)).fetchone()[0]!=1:raise PermissionError('fresh core test tenant required')
    server=Path(server_root).absolute();client=Path(client_root).absolute()
    if server==client or server.is_relative_to(client) or client.is_relative_to(server):raise PermissionError('independent test identity volumes required')
    for root in (server,client):
        if root.is_symlink() or (root.exists() and (not root.is_dir() or root.stat().st_uid!=os.geteuid() or any(root.iterdir()))):raise PermissionError('fresh empty owned browser volumes required')
    with connection.transaction():
        connection.execute("select pg_advisory_xact_lock(hashtextextended('nexloop-browser-test-init-v1',0))")
        for table in TABLES:
            if connection.execute(sql.SQL('select count(*) from control.{}').format(sql.Identifier(table))).fetchone()[0]:raise PermissionError('fresh empty browser identity configuration required')
        connection.execute("set local log_statement='none'");connection.execute("set local log_min_error_statement='panic'")
        now=connection.execute('select clock_timestamp()').fetchone()[0]
        subject=Subject(subject_id=SUBJECT,kind=SubjectKind.HUMAN,status='active',created_at=now,updated_at=now,revision=1)
        member=TenantMembership(tenant_id=TENANT,principal_id=PRINCIPAL,subject_id=SUBJECT,kind=MembershipKind.HOME,status='active',valid_from=now,valid_until=None,revision=1)
        account=LocalAccount(local_account_id='nexloop-test-local-account',tenant_id=TENANT,subject_id=SUBJECT,username=USERNAME,
            verified_email='synthetic@nexloop.invalid',password_hash=EncodedPasswordHash(PasswordHasher().hash(password)),status='active',
            failed_attempts=0,lockout_level=0,locked_until=None,must_change_password=False,session_epoch=1,created_at=now,updated_at=now,revision=1)
        connection.execute('insert into control.nexloop_browser_applications values(%s,%s,1,true)',(TENANT,APPLICATION))
        connection.execute('insert into control.nexloop_browser_subjects values(%s,%s)',(SUBJECT,Jsonb(subject.model_dump(mode='json'))))
        connection.execute('insert into control.nexloop_browser_memberships values(%s,%s,%s,%s)',(TENANT,PRINCIPAL,SUBJECT,Jsonb(member.model_dump(mode='json'))))
        payload=account.model_dump(mode='json');payload.pop('password_hash');payload.pop('password_history')
        connection.execute('insert into control.nexloop_browser_accounts values(%s,%s,%s,%s,%s,%s,%s)',(TENANT,account.local_account_id,SUBJECT,USERNAME,account.password_hash.get_secret_value(),[],Jsonb(payload)))
        connection.execute("insert into control.nexloop_browser_rate_policies values(%s,'local_login','nexloop_identity',20,60,true)",(TENANT,))
        db_password=secrets.token_urlsafe(48)
        connection.execute("set local password_encryption='scram-sha-256'")
        connection.execute(sql.SQL('alter role {} password {}').format(sql.Identifier('nexloop_identity'),sql.Literal(db_password)))
        realm={'mode':'test','tenant_id':TENANT,'application_id':APPLICATION,'origin':origin,'synthetic_identity':True,'business_action_grants':False}
        values={server:{'identity_dsn':make_conninfo(host=host,port=port,dbname=dbname,user='nexloop_identity',password=db_password),
                        'rate.key':secrets.token_hex(32),'realm.json':json.dumps(realm)},
                client:{'credentials.json':json.dumps({'username':USERNAME,'password':password}),'realm.json':json.dumps(realm)}}
        for root,files in values.items():
            root.mkdir(mode=0o700,parents=True,exist_ok=True);root.chmod(0o700)
            for name,value in files.items():_write(root/name,value,uid)
            if uid!=os.geteuid():os.chown(root,uid,-1)
            fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(fd)
            finally:os.close(fd)
    return {'passed':True,'mode':'test','synthetic_identity':True,'business_action_grants':False,'product_ready':False}


def bootstrap_browser_container():
    """Only an explicitly delegated fresh test bootstrap job may provision IAM."""
    import psycopg
    if os.geteuid()!=0:raise PermissionError('privileged test initializer required')
    inputs={}
    for name in ['bootstrap_dsn','browser-input.json','browser-cert.pem','browser-key.pem']:
        fd=os.open(Path('/run/secrets')/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno());raw=stream.read(32769)
        if not stat.S_ISREG(info.st_mode) or not raw or len(raw)>32768:raise PermissionError('bounded test input required')
        inputs[name]=raw
    parameters=json.loads(inputs['browser-input.json'])
    if set(parameters)!={'origin','password'}:raise PermissionError('explicit test realm input required')
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain('/run/secrets/browser-cert.pem','/run/secrets/browser-key.pem')
    server=Path('/private/browser-server');client=Path('/private/browser-client')
    with psycopg.connect(inputs['bootstrap_dsn'].decode().strip(),autocommit=True) as connection:
        result=initialize_browser_test_profile(connection,server_root=server,client_root=client,
            host='postgres',port=5432,dbname='nexloop_test',service_uid=10001,**parameters)
    for root,name,raw in [(server,'server.crt',inputs['browser-cert.pem']),(server,'server.key',inputs['browser-key.pem']),(client,'ca.crt',inputs['browser-cert.pem'])]:
        _write(root/name,raw,10001)
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)
    return result


def probe_browser_test():
    import http.client
    from http.cookies import SimpleCookie
    import re
    from nexloop_eios.private_configuration import read_private_text
    from nexloop_eios.browser_http import COOKIE
    root=Path('/private/browser-client')
    realm=json.loads(read_private_text(root/'realm.json',maximum=8192))
    credentials=json.loads(read_private_text(root/'credentials.json',maximum=1024))
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=read_private_text(root/'ca.crt',maximum=32768))
    authority=urlsplit(realm['origin']).netloc
    def request(method,path,*,body=None,cookie=None,csrf=None,origin=True):
        connection=http.client.HTTPSConnection('127.0.0.1',8443,context=context,timeout=3)
        headers={'Host':authority}
        if origin:headers['Origin']=realm['origin']
        if body is not None:headers['Content-Type']='application/json';body=json.dumps(body)
        if cookie is not None:headers['Cookie']=COOKIE+'='+cookie
        if csrf is not None:headers['X-CSRF-Token']=csrf
        try:
            connection.request(method,path,body=body,headers=headers)
            response=connection.getresponse();raw=response.read(2097153)
            if len(raw)>2097152:raise ValueError('bounded web response required')
            return response.status,raw,response.getheader('Set-Cookie','')
        finally:connection.close()
    checks={}
    status,html,_=request('GET','/');checks['frontend_html']=status==200 and b'<div id="root"></div>' in html
    match=re.search(rb'src="(/assets/[^" ]+\.js)"',html)
    if match is None:raise ValueError('actual built frontend required')
    status,script,_=request('GET',match.group(1).decode())
    checks['frontend_asset']=status==200 and b'/api/v1/auth/login' in script
    checks['frontend_no_credentials']=credentials['password'].encode() not in html+script and b'MODEL_API_KEY' not in script
    checks['wrong_password_denied']=request('POST','/api/v1/auth/login',body={'username':USERNAME,'password':'incorrect-synthetic-password'})[0]==401
    checks['missing_origin_denied']=request('POST','/api/v1/auth/login',body=credentials,origin=False)[0]==403
    status,raw,header=request('POST','/api/v1/auth/login',body=credentials)
    login=json.loads(raw);checks['login']=status==200 and login.get('authenticated') is True and login.get('tenant_id')==TENANT
    cookies=SimpleCookie();cookies.load(header);token=cookies[COOKIE].value
    checks['secure_cookie']=bool(cookies[COOKIE]['secure'] and cookies[COOKIE]['httponly']) and cookies[COOKIE]['samesite'].lower()=='strict'
    checks['session_reload']=request('GET','/api/v1/auth/session',cookie=token)[0]==200
    checks['human_cookie_not_service_authority']=request('GET','/api/v1/artifacts/not-granted',cookie=token)[0]==401
    checks['wrong_csrf_denied']=request('POST','/api/v1/auth/logout',cookie=token,csrf='incorrect-synthetic-csrf')[0]==401
    checks['logout']=request('POST','/api/v1/auth/logout',cookie=token,csrf=login['csrf_token'])[0]==200
    checks['logged_out_session_denied']=request('GET','/api/v1/auth/session',cookie=token)[0]==401
    return {'passed':all(checks.values()),'mode':'test','synthetic_identity':True,'business_action_grants':False,'product_ready':False,'checks':checks}
