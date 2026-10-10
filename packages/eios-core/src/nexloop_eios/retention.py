"""NX-029 slice 1: retention settings and the retention keeper (docs/implementation/NX-029-design.md §3, §7, §8; 0141).

Settings come from deploy/configuration/retention.v1.json (tenant level, D9); the migration seeds the same canonical
text. ``RetentionKeeper`` (service principal retention_keeper, nexloop_domain_worker) asks the signed port which classes
are due and runs bounded sweep passes. Each pass is one transaction in SQL: message text, Claim quotes, prompt text and
job inputs are redacted in place, receipts, metric observations, financial records and cost entries are deleted, and a
tombstone without content records it. The keeper holds no table privilege, never sees content and cannot widen the
erasure scope: append-only rows are opened only inside the owner's sweep transaction (control.nexloop_erasure_scope).
"""
import hashlib
import json
from pathlib import Path
import uuid

from nexloop_eios.plan_reevaluation import _SignedPort

ACTION='eios:action:nexloop.retention.execute:1'
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


class RetentionKeeper:
    """One tick: every due class runs bounded passes until it has nothing left or max_passes is reached."""

    def __init__(self,pool,session,signer):
        self.pool,self.session,self.signer=pool,session,signer
        self.port=RetentionPort(pool,session,signer)

    def run_once(self):
        due=self.port.due();ref='sweep:'+str(uuid.uuid4())
        summary={'classes':0,'passes':0,'processed':0,'incomplete':0}
        for item_class in due['due']:
            summary['classes']+=1
            for _ in range(due['max_passes']):
                result=self.port.sweep(item_class,ref);summary['passes']+=1;summary['processed']+=result['processed']
                if not result['more']:break
            else:
                summary['incomplete']+=1  # the class stays due; the next tick continues
        return summary


__all__=['RetentionKeeper','RetentionPort','load_settings','canonical_settings']
