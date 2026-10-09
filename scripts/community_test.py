"""Prepare/run a fresh disposable core Compose project without reading .env."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import ssl
import socket
import subprocess

from prepare_community_build import prepare, ROOT
from prepare_host_build import prepare_host,FILES as HOST_FILES

PURPOSE='disposable-community-test'


def write_private(path, data):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb',closefd=False) as f:f.write(data);f.flush();os.fsync(fd)
    finally:os.close(fd)


def image(lock,name):
    entry=lock['images'][name]
    digest=entry['digest']
    if not digest.startswith('sha256:') or len(digest)!=71 or any(c not in '0123456789abcdef' for c in digest[7:]):
        raise ValueError('unresolved image digest')
    reference=entry['reference'].split('@')[0].rsplit(':',1)[0]
    allowed={'postgres':'docker.io/pgvector/pgvector','python-build-base':'docker.io/library/python','valkey':'docker.io/valkey/valkey','node-host-base':'docker.io/library/node'}
    if reference!=allowed.get(name):raise ValueError('unexpected image source')
    return reference+'@'+digest


def prepare_project(output):
    output=Path(output).absolute();base=ROOT/'.ci-results'
    if output.parent!=base or output.exists() or output.is_symlink() or base.is_symlink():
        raise ValueError('new direct child of ignored results directory required')
    base.mkdir(exist_ok=True)
    if base.stat().st_uid!=os.getuid():raise ValueError('unowned results directory')
    if any(c in str(output) for c in ('\n','\r','$','"',"'",'#',' ','\t')):raise ValueError('unsupported output path')
    lock=json.loads((ROOT/'versions.lock.json').read_text())
    postgres=image(lock,'postgres');python=image(lock,'python-build-base');valkey=image(lock,'valkey');node=image(lock,'node-host-base')
    project='nexloop-test-'+secrets.token_hex(12)
    output.mkdir(mode=0o700)
    private=output/'secrets';private.mkdir(mode=0o700)
    password=secrets.token_hex(32)
    write_private(private/'pg_bootstrap_password',password.encode())
    write_private(private/'bootstrap_dsn',('host=postgres port=5432 dbname=nexloop_test user=nexloop_bootstrap password='+password).encode())
    cache_password=secrets.token_hex(32)
    write_private(private/'cache-credentials.json',json.dumps({'username':'nexloop_cache_test','password':cache_password}).encode())
    acl='user default off\nuser nexloop_cache_test on #'+hashlib.sha256(cache_password.encode()).hexdigest()+' resetkeys ~nexloop:test:cache:* resetchannels -@all +ping +get +set +del\n'
    write_private(private/'cache.acl',acl.encode())
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(private/'cache-key.pem'),'-out',str(private/'cache-cert.pem'),'-days','1','-subj','/CN=valkey','-addext','subjectAltName=DNS:valkey'],check=True,capture_output=True,timeout=30)
    for name in ('cache-key.pem','cache-cert.pem'):(private/name).chmod(0o600)
    cache_names=('cache-key.pem','cache-cert.pem','cache.acl','cache-credentials.json')
    cache_sha256={name:hashlib.sha256((private/name).read_bytes()).hexdigest() for name in cache_names}
    write_private(private/'host-control.key',secrets.token_hex(32).encode())
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(private/'host-key.pem'),'-out',str(private/'host-cert.pem'),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],check=True,capture_output=True,timeout=30)
    for name in ('host-key.pem','host-cert.pem'):(private/name).chmod(0o600)
    host_names=('host-control.key','host-key.pem','host-cert.pem')
    host_sha256={name:hashlib.sha256((private/name).read_bytes()).hexdigest() for name in host_names}
    host_build=prepare_host(output/'host-build')
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));web_port=probe.getsockname()[1]
    write_private(private/'browser-input.json',json.dumps({'origin':f'https://localhost:{web_port}','password':secrets.token_urlsafe(40)}).encode())
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(private/'browser-key.pem'),'-out',str(private/'browser-cert.pem'),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],check=True,capture_output=True,timeout=30)
    for name in ('browser-key.pem','browser-cert.pem'):(private/name).chmod(0o600)
    browser_sha256={name:hashlib.sha256((private/name).read_bytes()).hexdigest() for name in ['browser-input.json','browser-key.pem','browser-cert.pem']}
    build=prepare(output/'build')
    backend=project+':core-test';host_image=project+':host-test'
    refs=('NEXLOOP_TEST_PROJECT='+project+'\nPOSTGRES_IMAGE='+postgres+'\nVALKEY_IMAGE='+valkey+'\nBACKEND_IMAGE='+backend+'\nHOST_IMAGE='+host_image+'\nWEB_PORT='+str(web_port)+'\nSECRET_DIR='+str(private)+'\n')
    # Compose env-file paths containing whitespace or interpolation need escaping;
    # refuse them rather than silently selecting another secret path.
    if any(c in str(output) for c in ('\n','\r','$','"',"'",'#',' ','\t')):raise ValueError('unsupported output path')
    write_private(output/'compose.env',refs.encode())
    manifest={'purpose':PURPOSE,'project':project,'postgres_image':postgres,'python_image':python,'valkey_image':valkey,'node_image':node,'host_image':host_image,'host_inputs_sha256':host_sha256,'host_build_sha256':host_build['sha256'],'web_port':web_port,'browser_inputs_sha256':browser_sha256,'cache_inputs_sha256':cache_sha256,'backend_image':backend,
        'build_sha256':build['sha256'],'compose_sha256':hashlib.sha256((ROOT/'deploy/community/compose.test.yaml').read_bytes()).hexdigest(),
        'prepared_only':True,'container_built':False,'runtime_verified':False}
    write_private(output/'manifest.json',(json.dumps(manifest,indent=2)+'\n').encode())
    fd=os.open(output,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
    return manifest


def verify_project(output):
    output=Path(output).absolute();base=ROOT/'.ci-results'
    if output.parent!=base or output.is_symlink() or base.is_symlink():raise ValueError('invalid project directory')
    for path,mode in [(output,0o700),(output/'secrets',0o700)]:
        st=path.lstat()
        if not stat.S_ISDIR(st.st_mode) or stat.S_IMODE(st.st_mode)!=mode or st.st_uid!=os.getuid():raise ValueError('private directory refused')
    for path in [output/'manifest.json',output/'compose.env',output/'secrets/pg_bootstrap_password',output/'secrets/bootstrap_dsn']:
        st=path.lstat()
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode)!=0o600 or st.st_uid!=os.getuid() or st.st_size>16384:raise ValueError('private file refused')
    m=json.loads((output/'manifest.json').read_text());project=m['project']
    suffix=project.removeprefix('nexloop-test-')
    if len(suffix)!=24 or any(c not in '0123456789abcdef' for c in suffix) or m['purpose']!=PURPOSE:raise ValueError('project identity refused')
    lock=json.loads((ROOT/'versions.lock.json').read_text())
    if m['host_image']!=project+':host-test' or m['node_image']!=image(lock,'node-host-base') or m['valkey_image']!=image(lock,'valkey') or m['postgres_image']!=image(lock,'postgres') or m['python_image']!=image(lock,'python-build-base') or m['backend_image']!=project+':core-test':raise ValueError('image pin drift')
    expected=('NEXLOOP_TEST_PROJECT='+project+'\nPOSTGRES_IMAGE='+m['postgres_image']+'\nVALKEY_IMAGE='+m['valkey_image']+'\nBACKEND_IMAGE='+m['backend_image']+'\nHOST_IMAGE='+m['host_image']+'\nWEB_PORT='+str(m['web_port'])+'\nSECRET_DIR='+str(output/'secrets')+'\n')
    if (output/'compose.env').read_text()!=expected:raise ValueError('Compose references drift')
    if hashlib.sha256((ROOT/'deploy/community/compose.test.yaml').read_bytes()).hexdigest()!=m['compose_sha256']:raise ValueError('Compose graph drift')
    if (output/'build').is_symlink():raise ValueError('build directory refused')
    password=(output/'secrets/pg_bootstrap_password').read_text()
    if len(password)!=64 or any(c not in '0123456789abcdef' for c in password):raise ValueError('test password refused')
    if (output/'secrets/bootstrap_dsn').read_text()!='host=postgres port=5432 dbname=nexloop_test user=nexloop_bootstrap password='+password:raise ValueError('bootstrap endpoint refused')
    if set(m['cache_inputs_sha256'])!={'cache-key.pem','cache-cert.pem','cache.acl','cache-credentials.json'}:raise ValueError('cache input set drift')
    if set(p.name for p in (output/'secrets').iterdir())!=set(m['cache_inputs_sha256'])|set(m['host_inputs_sha256'])|set(m['browser_inputs_sha256'])|{'pg_bootstrap_password','bootstrap_dsn'}:raise ValueError('unexpected private inputs')
    for name,digest in m['cache_inputs_sha256'].items():
        path=output/'secrets'/name;st=path.lstat()
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode)!=0o600 or st.st_uid!=os.getuid() or st.st_size>32768 or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('cache input drift')
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(output/'secrets/cache-cert.pem',output/'secrets/cache-key.pem')
    if set(m['host_inputs_sha256'])!={'host-control.key','host-key.pem','host-cert.pem'}:raise ValueError('Host input set drift')
    for name,digest in m['host_inputs_sha256'].items():
        path=output/'secrets'/name;st=path.lstat()
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode)!=0o600 or st.st_uid!=os.getuid() or st.st_size>32768 or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('Host input drift')
    tls.load_cert_chain(output/'secrets/host-cert.pem',output/'secrets/host-key.pem')
    host_dir=output/'host-build'
    if host_dir.is_symlink() or set(m['host_build_sha256'])!=HOST_FILES or set(p.name for p in host_dir.iterdir())!=HOST_FILES:raise ValueError('Host context refused')
    for name,digest in m['host_build_sha256'].items():
        path=host_dir/name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('Host context drift')
    if type(m['web_port']) is not int or not 1024<=m['web_port']<=65535 or set(m['browser_inputs_sha256'])!={'browser-input.json','browser-key.pem','browser-cert.pem'}:raise ValueError('browser test input refused')
    for name,digest in m['browser_inputs_sha256'].items():
        path=output/'secrets'/name;st=path.lstat()
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode)!=0o600 or st.st_uid!=os.getuid() or st.st_size>32768 or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('browser input drift')
    realm=json.loads((output/'secrets/browser-input.json').read_text())
    if set(realm)!={'origin','password'} or realm['origin']!=f'https://localhost:{m['web_port']}':raise ValueError('browser realm drift')
    tls.load_cert_chain(output/'secrets/browser-cert.pem',output/'secrets/browser-key.pem')
    for name,digest in m['build_sha256'].items():
        if name not in ('.dockerignore','Dockerfile','requirements.txt','nexloop_eios_core-0.1.0-py3-none-any.whl') and not (name.startswith('web/') and '..' not in Path(name).parts and Path(name).suffix in {'.html','.css','.js'}):raise ValueError('unexpected build entry')
        p=output/'build'/name
        if p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise ValueError('build context drift')
    if any(p.is_symlink() for p in (output/'build').rglob('*')) or set(str(p.relative_to(output/'build')) for p in (output/'build').rglob('*') if p.is_file())!=set(m['build_sha256']):raise ValueError('unexpected build content')
    return m


def run_project(output):
    output=Path(output).absolute();m=verify_project(output)
    env={k:v for k,v in os.environ.items() if not k.startswith(('MODEL_','EMBEDDING_','COMPOSE_')) and k not in {'NEXLOOP_TEST_PROJECT','POSTGRES_IMAGE','VALKEY_IMAGE','BACKEND_IMAGE','HOST_IMAGE','WEB_PORT','SECRET_DIR'}}
    def docker(args,timeout=30):
        result=subprocess.run(['docker',*args],cwd=ROOT,env=env,capture_output=True,text=True,timeout=timeout)
        if result.returncode:raise RuntimeError('Docker operation unavailable or failed')
        return result.stdout
    docker(['info','--format','{{.ServerVersion}}'])
    # Never reuse an existing project's containers or data volumes, even stopped.
    if docker(['ps','-aq','--filter','label=com.docker.compose.project='+m['project']]).strip():raise ValueError('existing project containers refused')
    names=docker(['volume','ls','--format','{{.Name}}']).splitlines()
    if any(name.startswith(m['project']+'_') for name in names):raise ValueError('existing project volumes refused')
    compose=['compose','--env-file',str(output/'compose.env'),'-f',str(ROOT/'deploy/community/compose.test.yaml')]
    docker(compose+['config','--quiet'])
    docker(['build','--build-arg','PYTHON_IMAGE='+m['python_image'],'--tag',m['backend_image'],str(output/'build')],timeout=1200)
    docker(['build','--build-arg','PYTHON_IMAGE='+m['python_image'],'--build-arg','NODE_IMAGE='+m['node_image'],'--tag',m['host_image'],str(output/'host-build')],timeout=1200)
    docker(compose+['up','-d','check-test'],timeout=300)
    docker(compose+['wait','check-test'],timeout=180)
    cid=docker(compose+['ps','-aq','check-test']).strip()
    if not cid or '\n' in cid:raise RuntimeError('missing check job')
    if docker(['inspect','--format','{{.State.ExitCode}}',cid]).strip()!='0':raise RuntimeError('check job failed')
    report=json.loads(docker(['logs',cid]))
    if not report.get('passed') or report.get('product_ready') or report.get('model_validation')!='not_run':raise RuntimeError('unexpected check evidence')
    expected={'liveness','foundation_ready_product_unready','anonymous_denied','wrong_service_denied','authorized_artifact_read','api_host_control'}
    checks=report.get('http_checks',{})
    if set(checks)!=expected or any(value is not True for value in checks.values()):raise RuntimeError('actual API checks missing or failed')
    cache=report.get('cache',{})
    expected_cache={'default_user_denied','authenticated','ping','cache_write','cache_read','foreign_key_denied','admin_command_denied','wrong_password_denied','unknown_ca_denied','wrong_hostname_denied'}
    if cache.get('passed') is not True or cache.get('cache_authoritative') is not False or set(cache.get('checks',{}))!=expected_cache or any(value is not True for value in cache['checks'].values()):raise RuntimeError('actual cache TLS/ACL checks missing or failed')
    host=report.get('host',{})
    expected_host={'reachable','authenticated','owner_lock','product_unready','anonymous_denied','wrong_key_denied','browser_origin_denied'}
    if host.get('passed') is not True or host.get('business_authority') is not False or set(host.get('checks',{}))!=expected_host or any(value is not True for value in host['checks'].values()):raise RuntimeError('actual Host checks missing or failed')
    browser=report.get('browser',{})
    expected_browser={'frontend_html','frontend_asset','frontend_no_credentials','wrong_password_denied','missing_origin_denied','login','secure_cookie','session_reload','human_cookie_not_service_authority','wrong_csrf_denied','logout','logged_out_session_denied'}
    if browser.get('passed') is not True or browser.get('business_action_grants') is not False or browser.get('synthetic_identity') is not True or set(browser.get('checks',{}))!=expected_browser or any(v is not True for v in browser['checks'].values()):raise RuntimeError('actual browser HTTPS checks missing or failed')
    worker_id=docker(compose+['ps','-aq','worker-test']).strip()
    if not worker_id or '\n' in worker_id:raise RuntimeError('missing worker job')
    if docker(['inspect','--format','{{.State.ExitCode}}',worker_id]).strip()!='0':raise RuntimeError('worker job failed')
    worker=json.loads(docker(['logs',worker_id]))
    expected_worker={'restricted_role','governed_orphan_removed','durable_resume_idempotent'}
    if worker.get('passed') is not True or worker.get('mode')!='test' or worker.get('world')!='test' or worker.get('product_ready') is not False or worker.get('business_action_grants') is not False or set(worker.get('checks',{}))!=expected_worker or any(v is not True for v in worker['checks'].values()):raise RuntimeError('actual governed Worker checks missing or failed')
    return {'passed':True,'purpose':PURPOSE,'project':m['project'],'container_built':True,'core_test_runtime_verified':True,'actual_api_http_verified':True,'actual_cache_tls_acl_verified':True,'actual_host_control_verified':True,'actual_https_frontend_login_verified':True,'actual_governed_worker_verified':True,'web_origin':f'https://localhost:{m['web_port']}','product_ready':False,'model_validation':'not_run','resources_retained':True}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('operation',choices=['prepare','verify','run']);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    try:report={'prepare':prepare_project,'verify':verify_project,'run':run_project}[a.operation](a.output)
    except Exception:
        print(json.dumps({'passed':False,'error':'disposable community project refused or unavailable','secrets_logged':False}));return 1
    print(json.dumps(report,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
