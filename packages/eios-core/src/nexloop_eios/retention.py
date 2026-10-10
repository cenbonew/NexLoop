"""NX-029 slices 1–2: retention settings, the retention keeper and erasure execution (docs/implementation/NX-029-design.md §3, §7, §8; 0141).

Settings come from deploy/configuration/retention.v1.json (tenant level, D9); the migration seeds the same canonical
text. ``RetentionKeeper`` (service principal retention_keeper, nexloop_domain_worker) asks the signed port which classes
are due and runs bounded sweep passes. Each pass is one transaction in SQL: message text, Claim quotes, prompt text and
job inputs are redacted in place, receipts, metric observations, financial records and cost entries are deleted, and a
tombstone without content records it. The keeper holds no table privilege, never sees content and cannot widen the
erasure scope: append-only rows are opened only inside the owner's sweep transaction (control.nexloop_erasure_scope).
"""
import hashlib
import http.client
import json
from pathlib import Path
import secrets
import ssl
import uuid

from nexloop_eios.plan_reevaluation import _SignedPort

ACTION='eios:action:nexloop.retention.execute:1'
ERASURE_ACTION='eios:action:nexloop.erasure.execute:1'
CLASSES=('message_text','claim_quote','prompt_text','job_input','commercial_receipt','metric_observation')
SETTINGS_KEYS={'schema','version','decision','classes','sweep'}
_SWEEP={'batch':(1,1000),'max_passes':(1,100),'interval_seconds':(60,86400)}


def load_settings(path):
    """Bounded deployment configuration; a class with a source never outlives it (NX-023-B, D2)."""
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    if type(value) is not dict or set(value)!=SETTINGS_KEYS:raise ValueError('retention settings shape')
    if value['schema']!='nexloop-retention/1' or type(value['version']) is not int or value['version']<1:raise ValueError('retention settings version')
    if type(value['decision']) is not str or not value['decision'].strip():raise ValueError('retention settings decision')
    classes=value['classes']
    if type(classes) is not dict or not classes or not set(classes)<=set(CLASSES):raise ValueError('retention classes')
    for name,entry in classes.items():
        if (type(entry) is not dict or set(entry)!={'days','source'} or type(entry['days']) is not int or not 1<=entry['days']<=36500
                or (entry['source'] is not None and (entry['source'] not in classes or entry['days']>classes[entry['source']]['days']))):
            raise ValueError('retention class '+name)
    sweep=value['sweep']
    if type(sweep) is not dict or set(sweep)!=set(_SWEEP):raise ValueError('retention sweep settings')
    for key,(low,high) in _SWEEP.items():
        if type(sweep[key]) is not int or not low<=sweep[key]<=high:raise ValueError('retention sweep '+key)
    return value


def canonical_settings(path):
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    text=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
    return text,hashlib.sha256(text.encode()).hexdigest()


class RetentionPort(_SignedPort):
    """Keeper port (eios:action:nexloop.retention.execute:1)."""
    PROTOCOL='nexloop-retention-v1';ACTION=ACTION;FUNCTION='nexloop_retention_command'

    def due(self):return self._signed({'verb':'due'})
    def sweep(self,item_class,sweep_ref):return self._signed({'verb':'sweep','item_class':str(item_class),'sweep_ref':str(sweep_ref)})
    def status(self):return self._signed({'verb':'status'})


class ErasurePort(_SignedPort):
    """Erasure port (eios:action:nexloop.erasure.execute:1; trusted configuration grants it to the keeper only)."""
    PROTOCOL='nexloop-erasure-v1';ACTION=ERASURE_ACTION;FUNCTION='nexloop_erasure_command'

    def requests(self,limit=50):return self._signed({'verb':'requests','limit':limit})['requests']
    def step(self,request_id):return self._signed({'verb':'step','request_id':str(request_id)})
    def run_purges(self,limit=50):return self._signed({'verb':'run_purges','limit':limit})['runs']
    def run_purged(self,run_id,outcome):return self._signed({'verb':'run_purged','run_id':str(run_id),'outcome':outcome})
    def artifact_candidates(self,limit=50):return self._signed({'verb':'artifact_candidates','limit':limit})['artifacts']
    def artifact_claim(self,artifact_id,worker):return self._signed({'verb':'artifact_claim','artifact_id':artifact_id,'worker':worker})
    def artifact_deleted(self,artifact_id,worker,fence):
        return self._signed({'verb':'artifact_deleted','artifact_id':artifact_id,'worker':worker,'fence':fence})
    def status(self):return self._signed({'verb':'status'})


class HostPurger:
    """The Agent Host's loopback runs/purge (D8): TLS to 127.0.0.1 with the Host's internal key; private files only."""

    def __init__(self,*,port,key_file,ca_file):
        from nexloop_eios.private_configuration import read_private_text
        if type(port) is not int or not 1024<=port<=65535:raise ValueError('host port')
        self.port=port;self.key=read_private_text(key_file,maximum=128).strip()
        self.context=ssl.create_default_context(cafile=str(ca_file))

    def __call__(self,run_id):
        connection=http.client.HTTPSConnection('127.0.0.1',self.port,context=self.context,timeout=10)
        try:
            connection.request('POST','/internal/v1/runs/purge',body=json.dumps({'run_id':str(run_id)}),
                headers={'Authorization':'Bearer '+self.key,'Content-Type':'application/json','Host':f'127.0.0.1:{self.port}'})
            response=connection.getresponse();body=response.read()
            if response.status!=200:return 'failed'
            outcome=json.loads(body).get('outcome')
            return outcome if outcome in ('purged','absent') else 'failed'
        except (OSError,ValueError,http.client.HTTPException):
            return 'failed'
        finally:
            connection.close()


class RetentionKeeper:
    """One tick: due expiry classes (bounded passes), confirmed erasures (bounded steps), then Run files and Artifacts.

    ``host`` purges one Run directory (HostPurger; None: Run files stay queued); ``artifact_store`` is the service's
    LocalBlobStore (None: Artifacts stay queued). Nothing is reported as deleted that was not deleted."""

    def __init__(self,pool,session,signer,*,host=None,artifact_store=None,max_steps=50):
        self.pool,self.session,self.signer=pool,session,signer
        self.port=RetentionPort(pool,session,signer);self.erasure=ErasurePort(pool,session,signer)
        self.host,self.artifact_store,self.max_steps=host,artifact_store,max_steps

    def run_once(self):
        due=self.port.due();ref='sweep:'+str(uuid.uuid4())
        summary={'classes':0,'passes':0,'processed':0,'incomplete':0,'erasure_steps':0,'runs_purged':0,'runs_failed':0,
            'artifacts_deleted':0,'artifacts_failed':0}
        for item_class in due['due']:
            summary['classes']+=1
            for _ in range(due['max_passes']):
                result=self.port.sweep(item_class,ref);summary['passes']+=1;summary['processed']+=result['processed']
                if not result['more']:break
            else:
                summary['incomplete']+=1  # the class stays due; the next tick continues
        for request_id in self.erasure.requests():
            for _ in range(self.max_steps):
                step=self.erasure.step(request_id)
                if not step.get('progress'):break
                summary['erasure_steps']+=1
        if self.host is not None:
            for run_id in self.erasure.run_purges():
                outcome=self.host(run_id)
                self.erasure.run_purged(run_id,outcome)
                summary['runs_failed' if outcome=='failed' else 'runs_purged']+=1
        if self.artifact_store is not None:
            from nexloop_eios.local_artifacts import BlobReference
            for artifact_id in self.erasure.artifact_candidates():
                worker=secrets.token_hex(16)
                try:
                    claim=self.erasure.artifact_claim(artifact_id,worker)
                    ref=BlobReference(self.session.authentication.tenant_id,self.session.world,artifact_id,claim['sha256'],claim['size_bytes'],claim['media_type'])
                    self.artifact_store.remove(ref)
                except Exception:
                    # The file may still exist: no tombstone; the lease expires and the next tick retries.
                    summary['artifacts_failed']+=1
                    continue
                self.erasure.artifact_deleted(artifact_id,worker,claim['fence']);summary['artifacts_deleted']+=1
        return summary


__all__=['ErasurePort','HostPurger','RetentionKeeper','RetentionPort','load_settings','canonical_settings']
