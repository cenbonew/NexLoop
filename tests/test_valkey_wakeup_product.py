"""Actual owned TLS Valkey and PG; only product performs SET."""
from contextlib import contextmanager
from pathlib import Path
import hashlib,json,secrets,subprocess,time,uuid
from fastapi.testclient import TestClient
from nexloop_eios.valkey_wakeup import WakeupConfiguration,ValkeyWakeup,AdvisoryQueueScheduler
from nexloop_eios.http_api import ApiConfiguration,create_app
from test_durable_queue import queues
from test_infrastructure_faults import docker

@contextmanager
def owned_tls_valkey(tmp):
    root=tmp/'cache-material';root.mkdir(mode=0o700)
    cert=root/'cert';key=root/'key';password=secrets.token_hex(32)
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],check=True,capture_output=True)
    acl=root/'acl';acl.write_text('user default off\nuser wakeup on #'+hashlib.sha256(password.encode()).hexdigest()+' resetkeys ~nexloop:wakeup:* -@all +ping +set +get\nuser owner on #'+hashlib.sha256(password.encode()).hexdigest()+' ~* -@all +ping +get +flushdb\n')
    for p in (cert,key,acl):p.chmod(0o444)
    credentials=root/'credentials';credentials.write_text(json.dumps({'username':'wakeup','password':password}));credentials.chmod(0o600)
    ca=root/'ca';ca.write_bytes(cert.read_bytes());ca.chmod(0o600)
    lock=json.loads((Path(__file__).resolve().parents[1]/'versions.lock.json').read_text())['images']['valkey'];image=lock['reference']+'@'+lock['digest']
    import socket
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1',0));fixed_port=reserved.getsockname()[1]
    owner=uuid.uuid4().hex;cid=None
    try:
        args=['run','--detach','--pull','never','--name','nexloop-wakeup-'+owner,'--label','nexloop.wakeup-owner='+owner,'--user','1000:1000','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','64m','--tmpfs','/data:rw,nosuid,nodev,size=8m','--publish',f'127.0.0.1:{fixed_port}:6379']
        for file in (cert,key,acl):args+=['--mount',f'type=bind,src={file.resolve()},dst=/private/{file.name},readonly']
        cid=docker(*args,image,'valkey-server','--port','0','--tls-port','6379','--tls-cert-file','/private/cert','--tls-key-file','/private/key','--tls-ca-cert-file','/private/cert','--tls-auth-clients','no','--aclfile','/private/acl','--save','','--appendonly','no')
        def verify(*,port=True):
            body=json.loads(docker('inspect',cid))[0]
            assert body['Id']==cid and body['Config']['Labels']['nexloop.wakeup-owner']==owner
            return int(body['NetworkSettings']['Ports']['6379/tcp'][0]['HostPort']) if port else None
        def adapter():return ValkeyWakeup(WakeupConfiguration('127.0.0.1',verify(),ca,credentials,'127.0.0.1',ttl_seconds=2))
        cache=adapter();deadline=time.monotonic()+10
        while not cache.health(probe=True)['available']:
            assert time.monotonic()<deadline,'owned TLS Valkey startup failed';time.sleep(.05)
        def owner_command(*args):
            client=adapter();client._credentials={'username':'owner','password':password}
            return client._command(*args)
        yield cache,owner_command,lambda:(verify(),docker('stop','--time','1',cid)),lambda:(verify(port=False),docker('start',cid),adapter())[-1]
    finally:
        if cid:verify(port=False);docker('rm','--force',cid)

def test_actual_product_tls_cache_stop_flush_rebuild_and_observable_degraded(queues,tmp_path,admin):
    api,scheduler,_=queues
    with owned_tls_valkey(tmp_path) as (cache,owner,stop,start):
        ingress=AdvisoryQueueScheduler(api,cache);worker=AdvisoryQueueScheduler(scheduler,cache)
        first=ingress.accept(source_id='synthetic',event_id='event-a',payload={'private_business_text':'never in cache'})
        assert cache.peek(scheduler)==(True,hashlib.sha256(first['task_id'].encode()).hexdigest())
        with pytest.raises(ValueError):worker.run_once(lease_seconds=1)
        assert scheduler.inspect(task_id=first['task_id'])['fence']==0
        config=ApiConfiguration(tmp_path/'absent-dsn',tmp_path/'absent-key',tmp_path/'absent-artifacts','synthetic',cache_wakeup=cache)
        with TestClient(create_app(config)) as http:
            assert http.get('/health/ready').json()['cache']['degraded'] is False
            assert owner('FLUSHDB')=='OK';assert cache.peek(scheduler)==(True,None)
            job=worker.run_once(lease_seconds=15);assert job['task_id']==first['task_id']
            assert cache.peek(scheduler)==(True,hashlib.sha256(first['task_id'].encode()).hexdigest())
            stop();assert http.get('/health/ready').json()['cache']['degraded'] is True
            second=ingress.accept(source_id='synthetic',event_id='event-b',payload={'text':'persist offline'})
            assert second['accepted'] is True
            assert ingress.accept(source_id='synthetic',event_id='event-b',payload={'text':'persist offline'})['created'] is False
            offline=worker.run_once(lease_seconds=15);assert offline['task_id']==second['task_id']
            scheduler.finish(task_id=job['task_id'],fence=job['fence'],status='succeeded',result={'ok':True})
            recovered=start()
            assert recovered.config.port==cache.config.port
            # Same endpoint/credentials/config: original product adapter reconnects
            # itself on its next tick, with no operator config mutation.
            assert cache.peek(scheduler)==(True,None)
            assert worker.run_once(lease_seconds=15) is None
            assert cache.peek(scheduler)==(True,hashlib.sha256(second['task_id'].encode()).hexdigest())
            assert http.get('/health/ready').json()['cache']['degraded'] is False
            assert owner('FLUSHDB')=='OK';assert worker.run_once(lease_seconds=15) is None
            assert cache.peek(scheduler)[1]==hashlib.sha256(second['task_id'].encode()).hexdigest()
            scheduler.finish(task_id=offline['task_id'],fence=offline['fence'],status='succeeded',result={'ok':True})
            assert admin.execute('select count(*) from runtime.jobs').fetchone()==(2,)
            assert admin.execute("select count(*) from runtime.jobs where status='succeeded'").fetchone()==(2,)
            assert admin.execute('select count(*) from ontology.objects').fetchone()==(0,)
            assert cache._attempt(lambda:cache._command('SET','foreign-key','denied'))[0] is False


def test_actual_tls_acl_and_secret_boundaries(queues,tmp_path):
    _,queue,_=queues
    with owned_tls_valkey(tmp_path) as (cache,owner,stop,start):
        original=cache.config
        cache.config=WakeupConfiguration(original.host,original.port,original.ca_file,original.credentials_file,'localhost')
        assert cache.peek(queue)==(False,None) # IP-only SAN: name verification mandatory
        assert cache.health()['degraded'] is True
        cache.config=original
        valid=dict(cache._credentials);cache._credentials={'username':'wakeup','password':secrets.token_hex(32)}
        assert cache.publish(queue,'opaque-task') is False
        cache._credentials=valid
        assert cache.publish(queue,'opaque-task') is True
        assert cache._attempt(lambda:cache._command('CONFIG','GET','*'))[0] is False
        assert cache._attempt(lambda:cache._command('FLUSHDB'))[0] is False
        assert cache._attempt(lambda:cache._command('SET','nexloop:foreign:secret','denied'))[0] is False
        original.credentials_file.chmod(0o644)
        try:
            import pytest
            with pytest.raises(PermissionError):ValkeyWakeup(original)
        finally:original.credentials_file.chmod(0o600)

from test_runtime_authority import authority
from test_runtime_worker import accepted_input,synthetic_credentials,stack,worker_process,assert_redacted
import sqlite3
import pytest

@pytest.mark.parametrize('mode',['disabled','available','stopped'])
def test_actual_runtime_worker_cli_cache_is_advisory(accepted_input,synthetic_credentials,pg,tmp_path,mode):
    api,worker,args,issued=accepted_input;receipt=api.accept_runtime_event(**args)
    with owned_tls_valkey(tmp_path) as (cache,owner,stop,start):
        c=cache.config
        config_file=tmp_path/'private-cache.json'
        config_file.write_text(json.dumps({'host':c.host,'port':c.port,'ca_file':str(c.ca_file),'credentials_file':str(c.credentials_file),'server_name':c.server_name,'timeout':c.timeout,'ttl_seconds':c.ttl_seconds}));config_file.chmod(0o600)
        if mode=='stopped':stop()
        with stack(pg,tmp_path,synthetic_credentials) as (runtime,node,arguments,guard_port):
            if mode!='disabled':arguments+=['--cache-configuration-file',str(config_file)]
            with worker_process(arguments+['--once']) as (process,out,err):assert process.wait(timeout=20)==0
            assert_redacted(out,err,args,synthetic_credentials,issued)
            result=json.loads(next(line for line in out if line.startswith('{')))
            assert result['claimed'] is True and result['status']=='succeeded'
            if mode=='disabled':assert 'cache' not in result
            else:
                assert result['cache']['degraded'] is (mode=='stopped')
                assert result['cache']['authoritative'] is False
            task=worker.inspect_task(queue='operations',task_id=receipt['task_id'])
            assert task['status']=='succeeded' and task['result']['business_action_success'] is False
            with sqlite3.connect(runtime/issued.run_id/'runtime.sqlite') as db:
                assert db.execute('select count(*) from submissions').fetchone()==(1,)

import socket,ssl,threading

def test_trickled_tls_reply_has_global_deadline_after_pg_commit(queues,tmp_path):
    api,_,_=queues
    with owned_tls_valkey(tmp_path) as (cache,owner,stop,start):
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(2)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cache.config.ca_file.parent/'cert',cache.config.ca_file.parent/'key')
        stopping=threading.Event()
        def slow():
            try:
                raw,_=listener.accept()
                with raw,context.wrap_socket(raw,server_side=True) as connection:
                    connection.recv(4096)
                    connection.sendall(b'+')
                    while not stopping.wait(.02):connection.sendall(b'x')
            except (OSError,ssl.SSLError):pass
        thread=threading.Thread(target=slow);thread.start()
        c=cache.config;cache.config=WakeupConfiguration('127.0.0.1',listener.getsockname()[1],c.ca_file,c.credentials_file,'127.0.0.1',timeout=.15)
        try:
            start_time=time.monotonic()
            receipt=AdvisoryQueueScheduler(api,cache).accept(source_id='synthetic',event_id='slow-cache-ack',payload={'text':'PG committed'})
            elapsed=time.monotonic()-start_time
            assert receipt['accepted'] is True and elapsed<.8
            assert cache.health()['degraded'] is True
            assert api.inspect(task_id=receipt['task_id'])['status']=='pending'
        finally:
            stopping.set();listener.close();thread.join(3);assert not thread.is_alive()

def test_actual_cli_cache_deadline_invalid_for_short_lease_rejected_before_claim(accepted_input,synthetic_credentials,pg,tmp_path,admin):
    api,worker,args,issued=accepted_input;receipt=api.accept_runtime_event(**args)
    with owned_tls_valkey(tmp_path) as (cache,owner,stop,start):
        c=cache.config;path=tmp_path/'oversized-cache-timeout.json'
        path.write_text(json.dumps({'host':c.host,'port':c.port,'ca_file':str(c.ca_file),'credentials_file':str(c.credentials_file),'server_name':c.server_name,'timeout':2,'ttl_seconds':10}));path.chmod(0o600)
        with stack(pg,tmp_path,synthetic_credentials) as (runtime,node,arguments,guard_port):
            with worker_process(arguments+['--cache-configuration-file',str(path),'--once'],ready=False) as (process,out,err):assert process.wait(timeout=20)!=0
            assert_redacted(out,err,args,synthetic_credentials,issued)
            task=worker.inspect_task(queue='operations',task_id=receipt['task_id'])
            assert task['status']=='pending' and task['fence']==0
            assert not (runtime/issued.run_id/'runtime.sqlite').exists()

@pytest.mark.parametrize('host',['valkey','localhost','multi-address.invalid','127.0.0.1,::1','[::1]','fe80::1%en0'])
def test_hostname_or_multiple_addresses_rejected_without_dns(host,monkeypatch,tmp_path):
    def forbidden(*args,**kwargs):raise AssertionError('DNS resolution must not run')
    monkeypatch.setattr(socket,'getaddrinfo',forbidden)
    with pytest.raises(ValueError):WakeupConfiguration(host,6379,tmp_path/'ca',tmp_path/'credentials','valkey')

def test_literal_ip_cache_does_not_call_getaddrinfo(queues,tmp_path,monkeypatch):
    _,queue,_=queues
    with owned_tls_valkey(tmp_path) as (cache,owner,stop,start):
        def forbidden(*args,**kwargs):raise AssertionError('DNS resolution must not run')
        monkeypatch.setattr(socket,'getaddrinfo',forbidden)
        assert cache.publish(queue,'owned-literal-hint') is True
        assert cache.health(probe=True)['available'] is True


def test_actual_owned_saturated_listener_connection_failure_preserves_pg_ack(queues,tmp_path):
    api,_,_=queues
    with owned_tls_valkey(tmp_path) as (cache,owner,stop,start):
        listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        listener.bind(('127.0.0.1',0));listener.listen(1)
        clients=[];saturated=False
        try:
            # Own listener never accepts. Fill its actual kernel backlog until
            # the next connect is actually unavailable (timeout or macOS reset).
            for _ in range(128):
                connection=socket.socket(socket.AF_INET,socket.SOCK_STREAM);connection.settimeout(.025)
                clients.append(connection)
                try:connection.connect(listener.getsockname())
                except (TimeoutError,ConnectionResetError,ConnectionRefusedError):saturated=True;break
            assert saturated,'owned listener must actually saturate'
            c=cache.config
            cache.config=WakeupConfiguration('127.0.0.1',listener.getsockname()[1],c.ca_file,c.credentials_file,'127.0.0.1',timeout=.15)
            started=time.monotonic()
            receipt=AdvisoryQueueScheduler(api,cache).accept(source_id='synthetic',event_id='owned-connect-deadline',payload={'text':'committed PG task'})
            assert time.monotonic()-started<.8 and receipt['accepted'] is True
            assert cache.health()['degraded'] is True
            assert api.inspect(task_id=receipt['task_id'])['status']=='pending'
        finally:
            for connection in clients:connection.close()
            listener.close()


def test_actual_owned_stalled_tls_connection_establishment_deadline(queues,tmp_path):
    api,_,_=queues
    with owned_tls_valkey(tmp_path) as (cache,owner,stop,start):
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(2)
        accepted=threading.Event();release=threading.Event()
        def stall():
            try:
                raw,_=listener.accept()
                with raw:
                    accepted.set()
                    release.wait(2) # TCP established; never supply TLS handshake
            except OSError:pass
        thread=threading.Thread(target=stall);thread.start()
        c=cache.config;cache.config=WakeupConfiguration('127.0.0.1',listener.getsockname()[1],c.ca_file,c.credentials_file,'127.0.0.1',timeout=.15)
        try:
            started=time.monotonic()
            receipt=AdvisoryQueueScheduler(api,cache).accept(source_id='synthetic',event_id='slow-owned-tls-establishment',payload={'text':'PG committed before cache'})
            assert accepted.is_set() and .1<=time.monotonic()-started<.8
            assert receipt['accepted'] is True and cache.health()['degraded'] is True
            assert api.inspect(task_id=receipt['task_id'])['status']=='pending'
        finally:
            release.set();listener.close();thread.join(3);assert not thread.is_alive()
