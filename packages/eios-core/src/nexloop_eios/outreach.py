"""Proactive outreach, slice 4: the governed send (nexloop.outreach.send:1) and its executor (docs/implementation/outreach-impl-plan.md).

The human owner asks through the NX-022 governed entry (``GoalGovernedActions.send_outreach``); acceptance sends nothing. The
executor admits each accepted intent through ``authz.nexloop_outreach_command``: the contact restriction first, the requester's
current authority, owner controls, then every outreach gate at dispatch time (purpose, marketing eligibility, test persona,
tenant quiet hours and frequency, consumer preferences, the three real dispatch layers, the model budget). A refused intent is
terminal and the provider receives nothing. The provider is called once per admitted intent; an unknown answer is queried by
intent id and never sent again. Only a loopback provider exists here: the real dispatch switch of this Action is closed by
default and nothing in this module opens a real channel."""
import hashlib
import json
import os
from pathlib import Path

from nexloop_eios.plan_reevaluation import _SignedPort

SEND_ACTION='eios:action:nexloop.outreach.send:1'
EXECUTE_ACTION='eios:action:nexloop.outreach.execute:1'


class OutreachPort(_SignedPort):
    """Executor port (eios:action:nexloop.outreach.execute:1, domain worker services only)."""
    PROTOCOL='nexloop-outreach-v1';ACTION=EXECUTE_ACTION;FUNCTION='nexloop_outreach_command';REAL_DISPATCH=True

    def due(self,limit=50):return self._signed({'verb':'due','limit':limit})['intents']
    def admit(self,intent_id):return self._signed({'verb':'admit','intent_id':str(intent_id)})
    def observe(self,intent_id,fence,outcome,provider_reference=None):
        payload={'verb':'observe','intent_id':str(intent_id),'fence':int(fence),'outcome':outcome}
        if provider_reference is not None:payload['provider_reference']=str(provider_reference)
        return self._signed(payload)
    def status(self,intent_id):return self._signed({'verb':'status','intent_id':str(intent_id)})


class ProviderUnknown(Exception):
    """The provider's answer is not known (timeout, broken connection): query before anything else."""


class LocalOutreachOutbox:
    """Loopback provider: one fsynced JSON record per intent in a private directory, queryable by intent id. The text is not
    kept, only its digest.

    ``fault`` (tests only): 'fail' answers a definite failure without writing; 'timeout_before_write' and
    'timeout_after_write' raise ProviderUnknown before or after the record is durable."""

    def __init__(self,directory,*,fault=None):
        self.directory=Path(directory);self.directory.mkdir(mode=0o700,parents=True,exist_ok=True);self.fault=fault;self.requests=[]

    def _path(self,intent_id):return self.directory/(hashlib.sha256(str(intent_id).encode()).hexdigest()+'.json')

    def send(self,request):
        self.requests.append(('send',request['intent_id']))
        if self.fault=='fail':return 'failed',None
        if self.fault=='timeout_before_write':raise ProviderUnknown()
        path=self._path(request['intent_id'])
        if not path.exists():
            record=json.dumps({'intent_id':request['intent_id'],'consumer_ref':request['consumer_ref'],'channel':request['channel'],'topic':request['topic'],
                'text_digest':hashlib.sha256(request['text'].encode()).hexdigest()},sort_keys=True,separators=(',',':'))
            temp=path.with_suffix('.tmp')
            with open(temp,'w',encoding='utf-8') as handle:
                handle.write(record);handle.flush();os.fsync(handle.fileno())
            os.replace(temp,path)
            directory=os.open(self.directory,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        if self.fault=='timeout_after_write':raise ProviderUnknown()
        return 'fulfilled','outbox:'+path.stem[:32]

    def query(self,intent_id):
        self.requests.append(('query',str(intent_id)))
        path=self._path(intent_id)
        return ('fulfilled','outbox:'+path.stem[:32]) if path.exists() else ('not_found',None)


class OutreachSender:
    """One tick: admit and send accepted intents once; query the unknown ones (and expired dispatching leases).

    A synchronous loopback outbox that answers 'not_found' to a query has not sent: that is a definite failure. Any other
    unanswered query leaves the intent unknown; it is never sent again."""

    def __init__(self,pool,session,signer,provider):
        self.port=OutreachPort(pool,session,signer);self.provider=provider

    def run_once(self):
        summary={'admitted':0,'refused':0,'fulfilled':0,'failed':0,'unknown':0}
        for item in self.port.due():
            if item['state']=='accepted':
                admitted=self.port.admit(item['intent_id'])
                if not admitted['admitted']:
                    summary['refused']+=admitted.get('state')=='refused';continue
                summary['admitted']+=1;fence=admitted['fence']
                try:outcome,reference=self.provider.send(admitted['request'])
                except ProviderUnknown:outcome,reference='unknown',None
            else:
                fence=self.port.status(item['intent_id'])['fence']
                try:answer,reference=self.provider.query(item['intent_id'])
                except ProviderUnknown:answer,reference='unknown',None
                outcome={'fulfilled':'fulfilled','not_found':'failed'}.get(answer,'unknown')
                if outcome=='unknown' and item['state']=='unknown':summary['unknown']+=1;continue
            self.port.observe(item['intent_id'],fence,outcome,reference);summary[outcome]+=1
        return summary


__all__=['OutreachSender','OutreachPort','LocalOutreachOutbox','ProviderUnknown','SEND_ACTION','EXECUTE_ACTION']
