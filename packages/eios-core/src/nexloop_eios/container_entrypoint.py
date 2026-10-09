"""Explicit community test container jobs; no model or environment credential read."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import stat
import ssl
import psycopg
from nexloop_eios.compose_bootstrap import initialize_test_profile,KEY_ID
from nexloop_eios.artifact_smoke import run_smoke
from nexloop_eios.doctor import diagnose
from nexloop_eios.private_configuration import read_private_text


def bootstrap_container():
    if os.geteuid()!=0:raise PermissionError('bootstrap container requires its privileged job identity')
    # Compose secrets are explicitly mounted inputs, not ordinary host config files.
    # Bind mounts retain source ownership; only this privileged job reads the granted input.
    path=Path('/run/secrets/bootstrap_dsn')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):raise PermissionError('bootstrap secret must be regular')
        raw=stream.read(16385)
    if not raw or len(raw)>16384:raise ValueError('bootstrap secret length invalid')
    dsn=raw.decode().strip()
    if not dsn or '\0' in dsn:raise ValueError('bootstrap secret invalid')
    with psycopg.connect(dsn,autocommit=True) as connection:
        return initialize_test_profile(connection,config_root=Path('/private/config'),artifact_root=Path('/var/lib/nexloop/artifacts'),
            host='postgres',port=5432,dbname='nexloop_test',service_uid=10001)


def check_container():
    config=Path('/private/config');artifacts=Path('/var/lib/nexloop/artifacts')
    report=run_smoke(database_url_file=config/'api_dsn',service_credential_file=config/'service_credential',
        signing_key_file=config/'artifact_key',signing_key_id=KEY_ID,artifact_root=artifacts)
    doctor=diagnose(database_url=read_private_text(config/'api_dsn',maximum=16384),artifact_root=artifacts,environment={'MODEL_PROVIDER':'test'})
    return {'passed':report['passed'] and doctor['foundation_checks_passed'],'mode':'test','world':'test',
        'product_ready':False,'model_validation':'not_run','smoke':report,'doctor':doctor}


def serve_test_api():
    """HTTPS API in the explicit disposable namespace; host publication is loopback-only."""
    from nexloop_eios.http_api import ApiConfiguration, create_app
    import uvicorn
    config=Path('/private/config')
    profile=json.loads(read_private_text(config/'profile.json',maximum=8192))
    if profile.get('mode')!='test' or profile.get('world')!='test' or profile.get('service_uid')!=os.geteuid():
        raise PermissionError('explicit service-owned test profile required')
    from nexloop_eios.host_test_profile import test_control_configuration
    from nexloop_eios.browser_http import BrowserConfiguration
    browser_root=Path('/private/browser-server')
    realm=json.loads(read_private_text(browser_root/'realm.json',maximum=8192))
    if realm.get('mode')!='test' or realm.get('synthetic_identity') is not True or realm.get('business_action_grants') is not False:raise PermissionError('explicit browser test realm required')
    browser=BrowserConfiguration(browser_root/'identity_dsn',browser_root/'rate.key',realm['tenant_id'],realm['application_id'],realm['origin'])
    read_private_text(browser_root/'server.crt',maximum=32768);read_private_text(browser_root/'server.key',maximum=32768)
    app=create_app(ApiConfiguration(config/'api_dsn',config/'artifact_key',
        Path('/var/lib/nexloop/artifacts'),KEY_ID,browser=browser,web_root=Path('/opt/nexloop/web'),host_control=test_control_configuration()))
    uvicorn.run(app,host='0.0.0.0',port=8443,access_log=False,log_level='warning',ssl_certfile=str(browser_root/'server.crt'),ssl_keyfile=str(browser_root/'server.key'))
    return {'passed':True,'mode':'test','product_ready':False,'stopped':True}


def check_http_container():
    """Exercise the actual API using governed Artifact output; no admin input."""
    result=check_container()
    if not result['passed']:return result
    token=read_private_text(Path('/private/config')/'service_credential',maximum=512)
    artifact=result['smoke']['artifact']
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=read_private_text(Path('/private/browser-client')/'ca.crt',maximum=32768))
    def request(path,authorization=None):
        connection=http.client.HTTPSConnection('127.0.0.1',8443,context=context,timeout=3)
        try:
            headers={} if authorization is None else {'Authorization':'Bearer '+authorization}
            connection.request('GET',path,headers=headers)
            response=connection.getresponse();body=response.read(65537)
            if len(body)>65536:raise ValueError('bounded API response required')
            return response.status,body
        finally:connection.close()
    status,body=request('/health/live')
    live=status==200 and json.loads(body).get('alive') is True
    status,body=request('/health/ready');ready=json.loads(body)
    foundation=status==503 and ready.get('product_ready') is False and ready.get('foundation',{}).get('foundation_ready') is True
    path='/api/v1/artifacts/'+artifact['artifact_id']
    unauthorized=request(path)[0]==401
    wrong_credential=request(path,'not-a-valid-service-credential')[0]==401
    status,payload=request(path,token)
    read=status==200 and len(payload)==artifact['size_bytes'] and hashlib.sha256(payload).hexdigest()==artifact['sha256']
    checks={'liveness':live,'foundation_ready_product_unready':foundation,
            'anonymous_denied':unauthorized,'wrong_service_denied':wrong_credential,'authorized_artifact_read':read,
            'api_host_control':all(ready.get('host',{}).get(key) is True for key in ['reachable','authenticated','owner_lock']) and ready.get('host',{}).get('product_ready') is False}
    from nexloop_eios.cache_test_profile import probe_cache_test
    result['cache']=probe_cache_test()
    from nexloop_eios.host_test_profile import probe_host_test
    result['host']=probe_host_test()
    from nexloop_eios.browser_test_profile import probe_browser_test
    result['browser']=probe_browser_test()
    result['http_checks']=checks;result['passed']=result['browser']['passed'] and all(checks.values()) and result['cache']['passed'] and result['host']['passed']
    return result


def main():
    parser=argparse.ArgumentParser(description='Community test bootstrap/PG-Artifact diagnostic jobs')
    parser.add_argument('job',choices=['bootstrap','check-test','api-test','check-http-test','cache-bootstrap','host-bootstrap','browser-bootstrap','worker-bootstrap','worker-test','outbound-recorder',
        'claim-extraction-scheduler','claim-extraction-worker','claim-matcher','recall-indexer'])
    args=parser.parse_args()
    if args.job in ('claim-extraction-scheduler','claim-extraction-worker','claim-matcher','recall-indexer'):
        # Long-running restricted background services (opt-in compose profile "background"). Their private
        # files are provisioned by trusted configuration (service-grants), never here; optional files are
        # passed only when present.
        from nexloop_eios.background_services import main_for
        root=Path('/private/background')/args.job
        argv=['--database-url-file',str(root/'database_url'),'--signing-key-file',str(root/'artifact_key'),'--signing-key-id',
            read_private_text(root/'signing_key_id',maximum=64).strip(),'--service-credential-file',str(root/'service_credential'),
            '--artifact-root','/var/lib/nexloop/artifacts','--world','real']
        for option,name in (('--model-env-file','model.env'),('--embedding-env-file','embedding.env'),('--match-config-file','match-config.json'),('--types-file','types.json')):
            if (root/name).exists():argv+=[option,str(root/name)]
        return main_for(args.job,argv)
    if args.job=='outbound-recorder':
        # Long-running restricted service (opt-in compose profile). Its private files are
        # provisioned by trusted configuration (service-grants + business-actions), never here.
        from nexloop_eios.outbound_messages import main as recorder
        root=Path('/private/outbound')
        return recorder(['--database-url-file',str(root/'api_dsn'),'--signing-key-file',str(root/'artifact_key'),'--signing-key-id',
            read_private_text(root/'signing_key_id',maximum=64).strip(),'--service-credential-file',str(root/'service_credential'),
            '--artifact-root','/var/lib/nexloop/artifacts','--world','real'])
    from nexloop_eios.cache_test_profile import initialize_cache_test_profile
    from nexloop_eios.host_test_profile import initialize_host_test_profile
    from nexloop_eios.browser_test_profile import bootstrap_browser_container
    from nexloop_eios.worker_test_profile import bootstrap_worker_container,check_worker_container
    try:result={'worker-bootstrap':bootstrap_worker_container,'worker-test':check_worker_container,'browser-bootstrap':bootstrap_browser_container,'host-bootstrap':initialize_host_test_profile,'cache-bootstrap':initialize_cache_test_profile,'bootstrap':bootstrap_container,'check-test':check_container,'api-test':serve_test_api,'check-http-test':check_http_container}[args.job]()
    except Exception:result={'passed':False,'mode':'test','product_ready':False,'error':'container test job refused or unavailable'}
    print(json.dumps(result,indent=2));return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
