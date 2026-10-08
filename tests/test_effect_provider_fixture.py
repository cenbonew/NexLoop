"""Actual synthetic HTTP provider persistence/fault protocol, no real channel."""
from concurrent.futures import ThreadPoolExecutor
import json
import signal
import subprocess
import sys

import httpx
from support.effect_provider import effect_provider,payload_digest


def body(intent='synthetic-intent',parameters=None):
    parameters={'operation':'synthetic fulfillment'} if parameters is None else parameters
    return {'intent_id':intent,'parameters':parameters,'payload_digest':payload_digest(parameters)}


def dispatch(provider,arguments):
    return httpx.post(provider.origin+'/v1/effects',json=arguments,
        headers={'Idempotency-Key':arguments['intent_id']},trust_env=False,timeout=5)


def query(provider,intent):return httpx.get(provider.origin+'/v1/effects/'+intent,trust_env=False,timeout=5)


def test_provider_receipt_conflict_and_full_storage_reopen(tmp_path):
    path=tmp_path/'synthetic-provider.sqlite';arguments=body()
    with effect_provider(path) as provider:
        accepted=dispatch(provider,arguments);assert accepted.status_code==202
        receipt=accepted.json();assert receipt['state']=='accepted' and receipt['provider_reference'].startswith('synthetic:')
        assert dispatch(provider,arguments).json()==receipt
        conflict=dispatch(provider,body(parameters={'operation':'different fulfillment'}))
        assert conflict.status_code==409 and conflict.json()==receipt
        provider.control('fulfill',intent_id=arguments['intent_id'])
        snapshot=provider.control('snapshot')
        assert snapshot['effects']==1 and snapshot['persistence']=={'journal_mode':'wal','synchronous':2}
    assert path.stat().st_mode&0o777==0o600
    with effect_provider(path) as provider:
        restored=query(provider,arguments['intent_id']);assert restored.status_code==200
        assert restored.json()=={**receipt,'state':'fulfilled'}
        assert query(provider,'synthetic-absent').json()=={'intent_id':'synthetic-absent','payload_digest':None,'state':'not_found','provider_reference':None}
        snapshot=provider.control('snapshot')
        assert snapshot['effects']==1
        assert snapshot['requests']==[('POST','synthetic-intent',202),('POST','synthetic-intent',202),('POST','synthetic-intent',409),('GET','synthetic-intent',200),('GET','synthetic-absent',404)]


def test_provider_concurrent_same_key_has_one_actual_effect(tmp_path):
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        with ThreadPoolExecutor(max_workers=4) as threads:
            responses=list(threads.map(lambda _:dispatch(provider,body()),range(8)))
        assert all(response.status_code==202 for response in responses)
        assert len({response.json()['provider_reference'] for response in responses})==1
        snapshot=provider.control('snapshot')
        assert snapshot['effects']==1 and len(snapshot['requests'])==8


def test_provider_committed_accept_survives_client_sigkill_then_query_without_send(tmp_path):
    arguments=body('synthetic-kill-window')
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        provider.control('pause')
        program="""
import json,sys,httpx
p=json.loads(sys.stdin.readline())
r=httpx.post(p['origin']+'/v1/effects',json=p['body'],headers={'Idempotency-Key':p['body']['intent_id']},trust_env=False,timeout=30)
print('response_received',flush=True)
"""
        child=subprocess.Popen([sys.executable,'-c',program],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            child.stdin.write(json.dumps({'origin':provider.origin,'body':arguments})+'\n');child.stdin.flush();child.stdin.close()
            barrier=provider.wait('accepted_committed')
            assert barrier['intent_id']==arguments['intent_id'] and child.poll() is None
            child.kill();assert child.wait(timeout=5)==-signal.SIGKILL
            assert child.stdout.read()=='' and child.stderr.read()==''
            before=provider.control('snapshot');assert before['effects']==1 and before['requests']==[('POST',arguments['intent_id'],202)]
            # Separate HTTP connection queries while the first response remains
            # paused: no replay POST is required to find the committed effect.
            recovered=query(provider,arguments['intent_id'])
            assert recovered.status_code==200 and recovered.json()['state']=='accepted'
            assert recovered.json()['payload_digest']==arguments['payload_digest']
            after=provider.control('snapshot')
            assert after['effects']==1 and after['requests']==before['requests']+[('GET',arguments['intent_id'],200)]
            provider.control('release')
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)
            child.stdout.close();child.stderr.close()


def test_provider_invalid_key_or_digest_never_creates_effect(tmp_path):
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        arguments=body();arguments['payload_digest']='0'*64
        assert dispatch(provider,arguments).status_code==400
        assert httpx.post(provider.origin+'/v1/effects',json=body(),headers={'Idempotency-Key':'other'},trust_env=False).status_code==400
        assert provider.control('snapshot')['effects']==0
