"""NX-054 P3 (docs/design/relation-backfill-p3.md): the relation backfill of stock objects, run by its own service principal through
the signed port authz.nexloop_relation_backfill_command (eios:action:nexloop.relation.backfill:1, nexloop_domain_worker).

``RelationBackfillPort`` signs each call anew (20 s TTL) and sets the call's transaction limits first: statement_timeout 1 s for
the verbs that hold locks or write (batch, rescan, revert_batch, complete, abort, start), 30 s for the read-only ones (preflight,
verify, status, sample); the port sets lock_timeout 200 ms itself and the engine refuses a batch outside (0, 1000 ms].
``RelationBackfill.run`` walks every cursor of a run: one connection, one call at a time, paced (design §3.2: batch 200, target
300 ms, halved above it, raised after 5 fast ones; 100 and 150 ms for source types whose relations reach Consumer; 200 ms between
batches); 55P03 / 40001 / 40P01 give the batch up and retry it, 57014 halves it (a new intent, design N-6); then the rescan with
backoff and complete. It pauses (no state change, design I-3) while the background shows table lock waits or 2 of the last 5
batches had more than 3 waiters. A lost connection (OperationalError: the batch's result is unknown) sends the same intent again,
which replays the stored answer if the batch had committed (design N-6). SIGTERM / SIGINT stop it between calls: one
event=interrupted line, exit code 3 (the run stays running; run again to go on). Output: one JSON line per batch, counts and ids
only.
"""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import signal
import sys
import threading
import time

import psycopg

from nexloop_eios.plan_reevaluation import _SignedPort

ACTION='eios:action:nexloop.relation.backfill:1'
LOCKING=('batch','rescan','revert_batch','complete','abort','start')
RETRY=(psycopg.errors.LockNotAvailable,psycopg.errors.SerializationFailure,psycopg.errors.DeadlockDetected)
INTERRUPTED=3
UNKNOWN_RESULT_RETRIES=5


class RelationBackfillPort(_SignedPort):
    PROTOCOL='nexloop-relation-backfill-v1';ACTION=ACTION;FUNCTION='nexloop_relation_backfill_command'

    def _signed(self,payload):
        self._verb=payload['verb']  # read by _prepare, which the base calls in the call's transaction
        return super()._signed(payload)

    def _prepare(self,db):
        db.execute("select set_config('statement_timeout',%s,true)",('1s' if self._verb in LOCKING else '30s',))

    def preflight(self):return self._signed({'verb':'preflight'})
    def verify(self):return self._signed({'verb':'verify'})
    def sample(self):return self._signed({'verb':'sample'})
    def start(self,run_id,digest):return self._signed({'verb':'start','run_id':run_id,'digest':digest})
    def status(self,run_id):return self._signed({'verb':'status','run_id':run_id})
    def complete(self,run_id):return self._signed({'verb':'complete','run_id':run_id})
    def abort(self,run_id,reason='operator'):return self._signed({'verb':'abort','run_id':run_id,'reason':reason})
    def rescan(self,run_id,limit=100):return self._signed({'verb':'rescan','run_id':run_id,'limit':limit})
    def revert_batch(self,run_id,limit=500):return self._signed({'verb':'revert_batch','run_id':run_id,'limit':limit})

    def batch(self,run_id,source_type,world,after,batch):
        return self._signed({'verb':'batch','run_id':run_id,'source_type':source_type,'world':world,'after':after,'batch':batch})


def consumer_target_types():
    from nexloop_eios.relation_catalog import FIRST_BATCH
    return frozenset(s.source_type for s in FIRST_BATCH if s.target_type=='Consumer')


class RelationBackfill:
    def __init__(self,port,*,out=sys.stdout,sleep=time.sleep,clock=time.monotonic,batch=200,target_ms=300,pause_ms=200,
                 consumer_batch=100,consumer_target_ms=150,max_batches=None,rescan_rounds=10,sample_seconds=30,stop=None):
        if not 50<=batch<=1000 or not 100<=target_ms<=2000 or not 50<=pause_ms<=10000:raise ValueError('relation backfill pacing')
        self.port,self.out,self.sleep,self.clock=port,out,sleep,clock
        self.batch,self.target_ms,self.pause_ms=batch,target_ms,pause_ms
        self.consumer_batch,self.consumer_target_ms=min(consumer_batch,batch),min(consumer_target_ms,target_ms)
        self.max_batches,self.rescan_rounds,self.sample_seconds=max_batches,rescan_rounds,sample_seconds
        self.consumer_types=consumer_target_types();self.batches=0;self.waiters=[];self.sampled=None
        self.stop=stop or threading.Event()

    def emit(self,**fields):print(json.dumps(fields,sort_keys=True,separators=(',',':')),file=self.out,flush=True)

    def quiet(self):
        """Pause while the background is not quiet: table lock waits, or 2 of the last 5 batches waited on by more than 3."""
        while not self.stop.is_set():
            now=self.clock()
            if self.sampled is None or now-self.sampled>=self.sample_seconds:
                self.background=self.port.sample();self.sampled=now
            busy=self.background['table_lock_waits']>0 or sum(1 for w in self.waiters[-5:] if w>3)>=2
            if not busy:return
            self.emit(event='paused',table_lock_waits=self.background['table_lock_waits'],waiters=self.waiters[-5:])
            self.sleep(60);self.sampled=None;self.waiters=self.waiters[-4:]+[0]

    def cursor(self,run_id,source_type,world,after):
        limit,target=(self.consumer_batch,self.consumer_target_ms) if source_type in self.consumer_types else (self.batch,self.target_ms)
        size,fast,unknown=limit,0,0
        while True:
            if self.stop.is_set():return 'interrupted'
            if self.max_batches is not None and self.batches>=self.max_batches:return 'limit'
            self.quiet()
            try:
                got=self.port.batch(run_id,source_type,world,after,size)
            except RETRY as error:
                self.emit(event='retry',source_type=source_type,world=world,code=error.sqlstate);self.sleep(2*self.pause_ms/1000);continue
            except psycopg.errors.QueryCanceled:
                size=max(50 if limit>=50 else limit,size//2);self.emit(event='retry',source_type=source_type,world=world,code='57014',batch=size)
                self.sleep(2*self.pause_ms/1000);continue
            except psycopg.OperationalError as error:
                # S-10, after the subclasses above (L6 C-2: 55P03, 40001, 40P01 and 57014 are OperationalErrors too): the result
                # is unknown; the same intent again replays it if the batch committed, or runs it if it did not.
                unknown+=1
                if unknown>UNKNOWN_RESULT_RETRIES:raise
                self.emit(event='retry',source_type=source_type,world=world,code=getattr(error,'sqlstate',None) or 'connection');self.sleep(2*self.pause_ms/1000);continue
            if 'stopped' in got:
                self.emit(event='stopped',state=got['stopped'],reason=got.get('reason'));return 'stopped'
            unknown=0
            if got.get('done'):return 'done'
            self.batches+=1;self.waiters.append(got['waiters'])
            self.emit(event='batch',source_type=source_type,world=world,batch=size,locked=got['locked'],skipped=got['skipped'],
                      target_locked=got['target_locked'],early=got['early'],waiters=got['waiters'],elapsed_ms=got['elapsed_ms'],counts=got['counts'])
            after=got['after']
            if got['elapsed_ms']>target:size,fast=max(50 if limit>=50 else limit,size//2),0
            elif got['elapsed_ms']<target/2:
                fast+=1
                if fast>=5:size,fast=min(limit,size+size//2),0
            else:fast=0
            self.sleep(self.pause_ms/1000)

    def run(self,run_id):
        status=self.port.status(run_id)
        if status is None or status['state']!='running':self.emit(event='stopped',state=None if status is None else status['state']);return 1
        for c in status['cursors']:
            if c['done']:continue
            ended=self.cursor(run_id,c['source_type'],c['world'],c['after'])
            if ended=='interrupted':self.emit(event='interrupted');return INTERRUPTED
            if ended!='done':return 0 if ended=='limit' else 1
        delay=1
        for _ in range(self.rescan_rounds):
            if self.stop.is_set():self.emit(event='interrupted');return INTERRUPTED
            got=self.port.rescan(run_id)
            if 'stopped' in got:self.emit(event='stopped',state=got['stopped'],reason=got.get('reason'));return 1
            self.emit(event='rescan',cleared=got['cleared'],kept=got['kept'],remaining=got['remaining'])
            if not got['remaining']:break
            self.sleep(delay);delay=min(60,delay*2)
        done=self.port.complete(run_id)
        self.emit(event='complete',**{k:v for k,v in done.items() if k!='stopped'})
        return 0 if done.get('completed') else 1

    def revert(self,run_id):
        while True:
            if self.stop.is_set():self.emit(event='interrupted');return INTERRUPTED
            try:got=self.port.revert_batch(run_id)
            except RETRY:self.sleep(2*self.pause_ms/1000);continue
            self.emit(event='revert',**got)
            if got['state']=='reverted':return 0
            self.sleep(self.pause_ms/1000)


def main(argv=None):
    from nexloop_eios.assembly import verify_application_role
    from nexloop_eios.backend import open_backend
    from nexloop_eios.private_configuration import read_private_text

    class _Parser(argparse.ArgumentParser):
        def error(self,message):self.exit(2,'relation backfill configuration unavailable\n')

    p=_Parser(description='NX-054 P3 relation backfill (signed port; private configuration, no migrations)')
    for name in ('database-url-file','signing-key-file','service-credential-file','artifact-root'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--signing-key-id',default='active')
    p.add_argument('command',choices=['preflight','start','run','status','verify','abort','revert'])
    p.add_argument('--run-id');p.add_argument('--batch',type=int,default=200);p.add_argument('--max-batches',type=int)
    a=p.parse_args(argv)
    if a.command not in ('preflight','verify') and not a.run_id:p.error('run id')
    with ExitStack() as stack:
        backend=stack.enter_context(open_backend(database_url=read_private_text(a.database_url_file,maximum=16384),artifact_root=a.artifact_root,
            signing_key_file=a.signing_key_file,signing_key_id=a.signing_key_id,pool_max_size=1))
        with backend._pool.connection() as connection:
            if verify_application_role(connection)!='nexloop_domain_worker':raise SystemExit('restricted service role required')
        def port():
            # Re-read the credential and re-authenticate for each call's port: revocation applies at once.
            current=backend.authenticate(read_private_text(a.service_credential_file,maximum=16384),world='real')
            return RelationBackfillPort(current._backend._pool,current._session,current._backend._signer)

        class Fresh:
            def __getattr__(self,name):return lambda *args,**kw:getattr(port(),name)(*args,**kw)

        fresh=Fresh();show=lambda v:print(json.dumps(v,sort_keys=True,separators=(',',':')),flush=True)
        if a.command=='preflight':show(fresh.preflight());return 0
        if a.command=='verify':show(fresh.verify());return 0
        if a.command=='status':show(fresh.status(a.run_id));return 0
        if a.command=='abort':show(fresh.abort(a.run_id));return 0
        if a.command=='start':
            pre=fresh.preflight()
            if pre['blocking']:show({'blocking':pre['blocking']});return 1
            show(fresh.start(a.run_id,pre['digest']));return 0
        stop=threading.Event();previous={}
        for signum in (signal.SIGTERM,signal.SIGINT):previous[signum]=signal.signal(signum,lambda *_:stop.set())
        try:
            runner=RelationBackfill(fresh,batch=a.batch,max_batches=a.max_batches,stop=stop,sleep=lambda s:stop.wait(s))
            return runner.run(a.run_id) if a.command=='run' else runner.revert(a.run_id)
        finally:
            for signum,handler in previous.items():signal.signal(signum,handler)


if __name__=='__main__':
    raise SystemExit(main())
