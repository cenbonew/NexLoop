"""AT-043: governed incentive issue (nexloop.incentive.issue:1), budgeted at dispatch (0145).

The human owner asks through the NX-022 governed entry (``GoalControls.issue_incentive``); the executor service admits
each accepted intent through ``authz.nexloop_incentive_command`` (controls, contact restriction with a notification,
catalog, per-Consumer limit and the tenant incentive budget, all at dispatch), calls the provider once, and records the
provider's answer. An unknown answer is queried by intent id and never sent again. Only loopback providers exist here
(design D9: no real incentive channel)."""
import hashlib
import json
import os
from pathlib import Path

from nexloop_eios.plan_reevaluation import _SignedPort

ISSUE_ACTION='eios:action:nexloop.incentive.issue:1'
EXECUTE_ACTION='eios:action:nexloop.incentive.execute:1'
RUN_SUBMIT_ACTION='eios:action:nexloop.incentive.run_submit:1'
OFFERING_FIELDS=('active','currency','eligibility','incentive_code','kind','max_amount_minor','per_consumer_period_limit_minor','title','valid_until')
BINDING_FIELDS=('consumer_id','offering_id','offering_revision','source_principal','active')


def offering_schemas():
    """Formal catalog types, written only by the existing governed object Actions."""
    from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
    kinds={'IncentiveOffering':{field:('boolean' if field=='active' else 'string') for field in OFFERING_FIELDS},
     'ConsumerIncentiveOffering':{field:('integer' if field=='offering_revision' else 'boolean' if field=='active' else 'string') for field in BINDING_FIELDS}}
    return tuple(ObjectTypeDefinition(type_name=name,version=1,only_edit_via_actions=True,
      properties=tuple(PropertyDefinition(property_name=key,value_type=PropertyValueType(kind)) for key,kind in fields.items())) for name,fields in kinds.items())


def fixed_amount_example(*,code,max_amount_minor,per_consumer_period_limit_minor,currency,valid_until):
    """An explicit example offering, not a default: every amount is the deployment owner's decision."""
    return {'incentive_code':code,'kind':'fixed_amount','max_amount_minor':str(max_amount_minor),
     'per_consumer_period_limit_minor':str(per_consumer_period_limit_minor),'currency':currency,'eligibility':'bound_consumer',
     'title':'测试优惠','active':True,'valid_until':valid_until}


class IncentivePort(_SignedPort):
    """Executor port (eios:action:nexloop.incentive.execute:1; trusted configuration grants it to the issuer service)."""
    PROTOCOL='nexloop-incentive-v1';ACTION=EXECUTE_ACTION;FUNCTION='nexloop_incentive_command';REAL_DISPATCH=True

    def due(self,limit=50):return self._signed({'verb':'due','limit':limit})['intents']
    def admit(self,intent_id):return self._signed({'verb':'admit','intent_id':str(intent_id)})
    def observe(self,intent_id,fence,outcome,provider_reference=None):
        payload={'verb':'observe','intent_id':str(intent_id),'fence':int(fence),'outcome':outcome}
        if provider_reference is not None:payload['provider_reference']=str(provider_reference)
        return self._signed(payload)
    def status(self,intent_id):return self._signed({'verb':'status','intent_id':str(intent_id)})


class RunIncentivePort(_SignedPort):
    """Runtime worker side of the Agent's incentive tool (eios:action:nexloop.incentive.run_submit:1). The guard has
    already re-authorized the Run's live activation; SQL checks the lease, the exact command, the Run's resources, the
    Role ceiling and the catalog, and derives the Consumer, the origin and the business key itself."""
    PROTOCOL='nexloop-run-incentive-v1';ACTION=RUN_SUBMIT_ACTION;FUNCTION='nexloop_run_incentive_command'

    def submit(self,*,activation_ref,command,parameters):
        import re
        from nexloop_eios.postgres_artifacts import canonical_payload
        if not isinstance(activation_ref,str) or re.fullmatch(r'activation_[a-f0-9-]{36}',activation_ref) is None:raise ValueError('activation reference')
        if type(parameters) is not dict or set(parameters)!={'incentive_offering_id','offering_revision','binding_id','amount_minor','currency','notification_text'}:
            raise ValueError('incentive parameters')
        return self._signed({'activation_ref':activation_ref,'command_text':canonical_payload(command),'parameters':parameters})


class ProviderUnknown(Exception):
    """The provider's answer is not known (timeout, broken connection): query before anything else."""


class LocalIncentiveLedger:
    """Loopback provider: one fsynced JSON record per intent in a private directory, queryable by intent id.

    ``fault`` (tests only): 'fail' answers a definite failure without writing; 'timeout_before_write' and
    'timeout_after_write' raise ProviderUnknown before or after the record is durable."""

    def __init__(self,directory,*,fault=None):
        self.directory=Path(directory);self.directory.mkdir(mode=0o700,parents=True,exist_ok=True);self.fault=fault;self.requests=[]

    def _path(self,intent_id):return self.directory/(hashlib.sha256(str(intent_id).encode()).hexdigest()+'.json')

    def issue(self,request):
        self.requests.append(('issue',request['intent_id']))
        if self.fault=='fail':return 'failed',None
        if self.fault=='timeout_before_write':raise ProviderUnknown()
        path=self._path(request['intent_id'])
        if not path.exists():
            record=json.dumps({k:request[k] for k in ('intent_id','incentive_code','amount_minor','currency','consumer_ref')},sort_keys=True,separators=(',',':'))
            temp=path.with_suffix('.tmp')
            with open(temp,'w',encoding='utf-8') as handle:
                handle.write(record);handle.flush();os.fsync(handle.fileno())
            os.replace(temp,path)
            directory=os.open(self.directory,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        if self.fault=='timeout_after_write':raise ProviderUnknown()
        return 'fulfilled','ledger:'+path.stem[:32]

    def query(self,intent_id):
        self.requests.append(('query',str(intent_id)))
        path=self._path(intent_id)
        return ('fulfilled','ledger:'+path.stem[:32]) if path.exists() else ('not_found',None)


class IncentiveIssuer:
    """One tick: admit and issue accepted intents once; query the unknown ones (and expired dispatching leases).

    A synchronous loopback ledger that answers 'not_found' to a query has not issued: that is a definite failure and the
    reservation is released (D6). Any other unanswered query leaves the intent unknown and reserved."""

    def __init__(self,pool,session,signer,provider):
        self.port=IncentivePort(pool,session,signer);self.provider=provider

    def run_once(self):
        summary={'admitted':0,'refused':0,'fulfilled':0,'failed':0,'unknown':0}
        for item in self.port.due():
            if item['state']=='accepted':
                admitted=self.port.admit(item['intent_id'])
                if not admitted['admitted']:
                    summary['refused']+=admitted.get('state')=='refused';continue
                summary['admitted']+=1;fence=admitted['fence']
                try:outcome,reference=self.provider.issue(admitted['request'])
                except ProviderUnknown:outcome,reference='unknown',None
            else:
                fence=self.port.status(item['intent_id'])['fence']
                try:answer,reference=self.provider.query(item['intent_id'])
                except ProviderUnknown:answer,reference='unknown',None
                outcome={'fulfilled':'fulfilled','not_found':'failed'}.get(answer,'unknown')
                if outcome=='unknown' and item['state']=='unknown':summary['unknown']+=1;continue
            self.port.observe(item['intent_id'],fence,outcome,reference);summary[outcome]+=1
        return summary


__all__=['IncentiveIssuer','IncentivePort','RunIncentivePort','LocalIncentiveLedger','ProviderUnknown','offering_schemas','fixed_amount_example']
