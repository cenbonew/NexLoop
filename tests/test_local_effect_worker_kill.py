"""Exact NX017 window: real export fsync, before HTTP ACK, Worker SIGKILL.

Production EffectWorker CLI + actual local51 service + genuine50 configuration
and governed business setup. No permission callback, provider fake or business
SQL seed. Uses only independently owned disposable PG fixtures.
"""
from contextlib import contextmanager
import json,select,signal,subprocess,sys,threading,time
import pytest
from psycopg.conninfo import make_conninfo
from nexloop_eios.local_json_delivery import make_server
from test_local_json_delivery_pg import delivery_plan,submit
from test_business_setup_pg import business_plan
from test_trusted_configuration_pg import configured,private


@contextmanager
def original_service_response_barrier(f,monkeypatch):
    """The service survives. Only real POST file publication is paused."""
    product_fsynced=threading.Event();release_response=threading.Event()
    real_publish=f['store']._immutable
    def publish(name,body):
        real_publish(name,body)
        if name.endswith('.export.json'):
            product_fsynced.set()
            assert release_response.wait(2.5),'owned response barrier not released'
    monkeypatch.setattr(f['store'],'_immutable',publish)
    server=make_server(f['server_config'],f['authority'],f['store'])
    actual_handler=server.RequestHandlerClass;requests=[];lock=threading.Lock()
    class CountActualTransport(actual_handler):
        def do_POST(self):
            with lock:requests.append(('POST',self.path))
            return super().do_POST()
        def do_GET(self):
            with lock:requests.append(('GET',self.path))
            return super().do_GET()
    server.RequestHandlerClass=CountActualTransport
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05});thread.start()
    try:yield product_fsynced,release_response,requests
    finally:
        release_response.set();server.shutdown();server.server_close();thread.join(5)
        assert not thread.is_alive()


def independent_cli(f,tmp_path):
    cfg=f['provider'].configuration
    path=private(tmp_path,'real-provider-config',json.dumps({'origin':cfg.origin,'credential_file':cfg.credential_file,
        'ca_file':cfg.ca_file,'timeout':1}))
    dsn=private(tmp_path,'real-effect-dsn',make_conninfo(f['manifest_pg'],user='nexloop_action_worker'))
    return [sys.executable,'-m','nexloop_eios.effect_worker','--database-url-file',str(dsn),
        '--service-credential-file',str(f['credential_file']),'--signing-key-file',str(f['publication_paths']['backend_signing']),
        '--signing-key-id','explicit-configuration','--artifact-root',str(tmp_path/'independent-real-effect-artifacts'),
        '--provider-config-file',str(path),'--world','real','--lease-seconds','3','--once']


def test_actual_fulfilled_file_before_reply_independent_worker_sigkill_recovers_only_get(delivery_plan,admin,tmp_path,monkeypatch):
    f=delivery_plan;intent=submit(f)['intent_id'];args=independent_cli(f,tmp_path)
    with original_service_response_barrier(f,monkeypatch) as (fsynced,release,requests):
        old=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            assert select.select([old.stdout],[],[],15)[0]
            assert old.stdout.readline()=='Effect Worker ready\n'
            assert fsynced.wait(5),'actual51 export did not reach fsync window'
            product=f['root']/(intent+'.export.json');snapshot=product.read_bytes();inode=product.stat().st_ino
            assert json.loads(snapshot)['intent_id']==intent
            # This is precisely external fulfilled but caller has not received
            # its HTTP response and no local observation was committed.
            assert admin.execute('select governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==(False,)
            assert admin.execute('select count(*) from runtime.nexloop_effect_observations where intent_id=%s',(intent,)).fetchone()==(0,)
            old.kill();stdout,stderr=old.communicate(timeout=10)
            assert old.returncode==-signal.SIGKILL and stdout==stderr==''
        finally:
            if old.poll() is None:old.kill();old.communicate(timeout=10)
            release.set()  # Live service completes its original guard, no restart.
        deadline=time.monotonic()+5
        while True:
            expired=admin.execute('select lease_until<=clock_timestamp() from runtime.nexloop_effect_outbox where intent_id=%s',(intent,)).fetchone()[0]
            if expired:break
            assert time.monotonic()<deadline;time.sleep(.05)
        new=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            stdout,stderr=new.communicate(timeout=15)
            assert new.returncode==0 and stderr==''
            assert stdout=='Effect Worker ready\n{"claimed":true,"status":"fulfilled"}\n'
        finally:
            if new.poll() is None:new.kill();new.communicate(timeout=10)
        assert requests==[('POST','/v1/effects'),('GET','/v1/effects/'+intent)]
        assert product.read_bytes()==snapshot and product.stat().st_ino==inode
        receipt=f['executor'].read_effect_receipt(intent_id=intent)
        assert receipt['business_action_success'] is True and receipt['governed_claim_finalized'] is True
    assert admin.execute('select count(*) from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()==(1,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_control_reservations where intent_id=%s',(intent,)).fetchone()==(1,)
    assert admin.execute('select budget_units,reserved_units from control.nexloop_effect_control_ledger where control_id=%s',(f['control'],)).fetchone()==(1,1)
    claim=admin.execute('select claim from runtime.nexloop_action_claims where intent_id=%s',(intent,)).fetchone()[0]
    assert claim['terminal_outcome']['status']=='succeeded' and claim['terminal_outcome']['outcome_id']==receipt['receipt_id']
