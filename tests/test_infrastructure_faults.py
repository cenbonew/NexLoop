"""Owned disposable Valkey/PG stop and recovery; no persistent/production target."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import uuid

import psycopg
from psycopg.conninfo import conninfo_to_dict
import pytest

REPOSITORY_ROOT=Path(__file__).resolve().parents[1]


def docker(*arguments):
    result=subprocess.run(['docker',*arguments],capture_output=True,text=True,timeout=20)
    if result.returncode:raise RuntimeError('owned test container command failed')
    return result.stdout.strip()


class OwnedValkey:
    def __init__(self,cid,owner,port):self.cid,self.owner,self.port=cid,owner,port
    def verify(self):
        body=json.loads(docker('inspect',self.cid))[0]
        assert body['Id']==self.cid and body['Config']['Labels']['nexloop.fault-owner']==self.owner
    def stop(self):self.verify();docker('stop','--time','1',self.cid)
    def start(self):
        self.verify();docker('start',self.cid)
        self.port=int(json.loads(docker('inspect',self.cid))[0]['NetworkSettings']['Ports']['6379/tcp'][0]['HostPort'])
        self.ready()
    def ready(self):
        deadline=time.monotonic()+5
        while True:
            try:
                assert self.command('PING')=='PONG';return
            except (OSError,AssertionError):
                if time.monotonic()>=deadline:raise
                time.sleep(.05)
    def command(self,*values):
        encoded=[v.encode() for v in values]
        body=b'*'+str(len(encoded)).encode()+b'\r\n'+b''.join(b'$'+str(len(v)).encode()+b'\r\n'+v+b'\r\n' for v in encoded)
        with socket.create_connection(('127.0.0.1',self.port),timeout=1) as connection:
            connection.sendall(body)
            with connection.makefile('rb') as stream:
                line=stream.readline(512)
                if not line:raise ConnectionError('owned cache transport closed')
                assert line.endswith(b'\r\n')
                if line.startswith(b'+'):return line[1:-2].decode()
                if line.startswith(b':'):return int(line[1:-2])
                if line==b'$-1\r\n':return None
                assert line.startswith(b'$');size=int(line[1:-2]);assert 0<=size<=4096
                value=stream.read(size+2);assert value.endswith(b'\r\n');return value[:-2].decode()


@contextmanager
def owned_valkey():
    lock=json.loads((REPOSITORY_ROOT/'versions.lock.json').read_text())['images']['valkey']
    image=lock['reference']+'@'+lock['digest']
    assert docker('image','inspect',image,'--format','{{.Id}}')==lock['digest']
    owner=uuid.uuid4().hex;cid=None
    try:
        cid=docker('run','--detach','--pull','never','--name','nexloop-fault-'+owner,
            '--label','nexloop.fault-owner='+owner,'--user','1000:1000','--read-only',
            '--cap-drop','ALL','--security-opt','no-new-privileges','--memory','64m','--cpus','0.25',
            '--tmpfs','/data:rw,nosuid,nodev,size=8m','--tmpfs','/tmp:rw,nosuid,nodev,size=8m',
            '--publish','127.0.0.1::6379',image,'valkey-server','--save','','--appendonly','no','--protected-mode','no')
        body=json.loads(docker('inspect',cid))[0];port=int(body['NetworkSettings']['Ports']['6379/tcp'][0]['HostPort'])
        cache=OwnedValkey(cid,owner,port);cache.ready();yield cache
    finally:
        if cid:
            cache.verify();docker('rm','--force',cid)


def test_actual_owned_valkey_flush_stop_restart_is_disposable():
    with owned_valkey() as cache:
        assert cache.command('SET','nexloop:test:notification','synthetic hint')=='OK'
        assert cache.command('GET','nexloop:test:notification')=='synthetic hint'
        assert cache.command('FLUSHDB')=='OK'
        assert cache.command('GET','nexloop:test:notification') is None
        cache.stop()
        with pytest.raises(OSError):cache.command('PING')
        cache.start();assert cache.command('GET','nexloop:test:notification') is None


@contextmanager
def stopped_owned_pg(pg):
    parameters=conninfo_to_dict(pg);sock=Path(parameters['host']).resolve();root=sock.parent;data=root/'data'
    # These are the exact paths produced by this session's pg fixture, not an
    # arbitrary administrator DSN. Verify before the actual destructive stop.
    assert root.name.startswith('nexloop-pg-') and root.parent==Path(tempfile.gettempdir()).resolve()
    assert sock.name=='socket' and data.is_dir() and (data/'PG_VERSION').read_text().strip()=='18'
    bindir=Path(os.environ.get('NEXLOOP_TEST_PG_BIN','/opt/homebrew/opt/postgresql@18/bin'))
    ctl=[str(bindir/'pg_ctl'),'-D',str(data)]
    subprocess.run([*ctl,'status'],check=True,capture_output=True,timeout=5)
    stopped=False
    try:
        subprocess.run([*ctl,'-m','immediate','-w','stop'],check=True,capture_output=True,timeout=15);stopped=True
        yield
    finally:
        if stopped:
            subprocess.run([*ctl,'-l',str(root/'postgres.log'),'-o',f"-k {sock} -p {parameters['port']} -c listen_addresses=''",'-w','start'],check=True,capture_output=True,timeout=15)


# Actual fixtures/modules resolve from the repository under test.
from effect_execution_fixture import governed_effect_executor,execution_plan
from test_effect_dispatch import independent_worker,accepted
from support.effect_provider import effect_provider
from nexloop_eios.effect_dispatch import EffectDispatcher
from nexloop_eios.effect_provider import HttpEffectProvider,EffectProviderConfiguration


def test_actual_pg_crash_after_provider_accept_recovers_original_intent_get_only(governed_effect_executor,pg,tmp_path):
    fixture=governed_effect_executor;receipt=accepted(fixture);intent=receipt['intent_id']
    with effect_provider(tmp_path/'pg-crash-provider.sqlite') as provider:
        provider.control('pause')
        with independent_worker(fixture,provider.origin) as (worker,pipe):
            assert provider.wait('accepted_committed')['intent_id']==intent
            with stopped_owned_pg(pg):
                with pytest.raises(psycopg.OperationalError):psycopg.connect(pg,connect_timeout=1)
                provider.control('release')
                assert pipe.poll(40),'actual Worker must fail closed during the owned PG outage'
                result=pipe.recv()
                assert result['event']=='settled' and result['business_action_success'] is False
                assert result['status'] in {'record_unavailable','unknown'}
                worker.join(10);assert worker.exitcode==0
                assert provider.control('snapshot')['requests']==[('POST',intent,202)]
        # The same fixture PGDATA is crash-recovered, not replaced by a Memory
        # store or a new database. Provider stayed alive independently throughout.
        with psycopg.connect(pg,autocommit=True) as recovered:
            assert recovered.execute('select count(*) from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()==(1,)
            assert recovered.execute('select governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==(False,)
            assert recovered.execute('select reserved_units from control.nexloop_effect_control_ledger').fetchone()==(1,)
            lease=recovered.execute('select lease_until from runtime.nexloop_effect_outbox where intent_id=%s',(intent,)).fetchone()[0]
            now=recovered.execute('select clock_timestamp()').fetchone()[0]
            if lease is not None and lease>now:time.sleep((lease-now).total_seconds()+.05)
        provider.control('fulfill',intent_id=intent)
        with independent_worker(fixture,provider.origin) as (restarted,pipe):
            assert pipe.poll(20);result=pipe.recv();restarted.join(10)
            assert restarted.exitcode==0 and result['status']=='fulfilled' and result['business_action_success'] is True
        assert provider.control('snapshot')['effects']==1
        assert provider.control('snapshot')['requests']==[('POST',intent,202),('GET',intent,200)]
        with psycopg.connect(pg,autocommit=True) as recovered:
            assert recovered.execute('select reserved_units from control.nexloop_effect_control_ledger').fetchone()==(1,)
            assert recovered.execute('select count(*) from runtime.nexloop_effect_control_reservations where intent_id=%s',(intent,)).fetchone()==(1,)
            assert recovered.execute('select governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==(True,)


def test_actual_valkey_stop_and_flush_do_not_replace_pg_effect_authority(governed_effect_executor,admin,tmp_path):
    fixture=governed_effect_executor;receipt=accepted(fixture);intent=receipt['intent_id']
    with owned_valkey() as cache,effect_provider(tmp_path/'cache-loss-provider.sqlite') as provider:
        assert cache.command('SET','nexloop:test:notification',intent)=='OK'
        with fixture.current_executor() as executor:
            result=EffectDispatcher(executor,HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1)),lease_seconds=3).run_once()
            assert result['status']=='dispatching' and result['business_action_success'] is False
        assert cache.command('FLUSHDB')=='OK';cache.stop()
        with pytest.raises(OSError):cache.command('GET','nexloop:test:notification')
        provider.control('fulfill',intent_id=intent);time.sleep(1.1)
        # Real Worker polls its actual protected PG outbox without this cache.
        with fixture.current_executor() as executor:
            result=EffectDispatcher(executor,HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1)),lease_seconds=3).run_once()
            assert result['status']=='fulfilled' and result['business_action_success'] is True
        cache.start();assert cache.command('GET','nexloop:test:notification') is None
        assert provider.control('snapshot')['requests']==[('POST',intent,202),('GET',intent,200)]
        assert admin.execute('select reserved_units from control.nexloop_effect_control_ledger').fetchone()==(1,)
        assert admin.execute('select count(*) from runtime.nexloop_effect_control_reservations where intent_id=%s',(intent,)).fetchone()==(1,)
