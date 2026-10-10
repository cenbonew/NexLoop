"""NX-026 commitments and value fulfilment (M18): docs/implementation/NX-026-design.md, migration 0111.

An enterprise commitment Claim (ADR-020 §2: only from a delivered outbound Message) is marked in the
'commitment-register' work feed in the transaction that recorded it. ``CommitmentKeeper`` (service principal
commitment_keeper, nexloop_domain_worker) drains two feeds, one tick per ``run_once()``:

* register: the SQL port prepares the registration from the Claim and the outbound record only (no model, no
  caller-chosen value); the Commitment object is then created through the governed Commitment.create Action and
  the SQL object guard accepts exactly the prepared properties; ``registered`` records it (Claim resolved,
  supersession, exceptions, plan marking when no due date can be determined).
* monitor: the SQL port evaluates the one transition the evidence ledger justifies now; it is applied through the
  governed Commitment.edit Action (the guard re-derives it and refuses anything else); ``settle`` records status
  events, raises owner-visible exceptions (breach, blocked by a contact restriction, conditional at due, no due
  date, no active plan), marks the Consumer's active plans before and at the due date (NX-024 T7) and schedules
  the next stage.

Human-only Actions (cancel, extend, attest, condition met, communication marking) are
``GoalGovernedActions.*_commitment`` (NX-022 governed entry). ``CommitmentReadPort`` is the query port before
NX-028. Settings come from deploy/configuration/commitments.v1.json (the migration seeds the same v1).
"""
import json
from pathlib import Path

from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.authorization import authority_request_scoped
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.object_edits import GovernedObjectEditor
from nexloop_eios.plan_reevaluation import PlanUnavailable,_SignedPort
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.work_feed import WorkFeed,WorkFeedDenied,retry_delay

KEEP_ACTION='eios:action:nexloop.commitment.keep:1'
READ_ACTION='eios:action:nexloop.commitment.read:1'
REGISTER_FEED='commitment-register'
MONITOR_FEED='commitment-monitor'
TYPE='Commitment'
CREATE=('Commitment.create',1)
EDIT=('Commitment.edit',1)
_WORKER={'batch':(1,50),'lease_seconds':(10,600),'max_attempts':(1,20),'retry_base_seconds':(1,3600)}


def load_settings(path):
    """Deployment configuration; every value is bounded, nothing defaults silently."""
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    if type(value) is not dict or set(value)!={'schema','version','decision','lead_seconds','unspecified_max_open_seconds','worker'}:
        raise ValueError('commitment settings shape')
    if value['schema']!='nexloop-commitments/1' or type(value['version']) is not int or value['version']<1:raise ValueError('commitment settings version')
    if type(value['decision']) is not str or not value['decision'].strip():raise ValueError('commitment settings decision')
    if type(value['lead_seconds']) is not int or not 0<=value['lead_seconds']<=2592000:raise ValueError('lead_seconds')
    if type(value['unspecified_max_open_seconds']) is not int or not 60<=value['unspecified_max_open_seconds']<=31536000:
        raise ValueError('unspecified_max_open_seconds')
    worker=value['worker']
    if type(worker) is not dict or set(worker)!=set(_WORKER):raise ValueError('commitment worker settings')
    for key,(low,high) in _WORKER.items():
        if type(worker[key]) is not int or not low<=worker[key]<=high:raise ValueError('commitment worker '+key)
    return value


class CommitmentPort(_SignedPort):
    """Keeper port (eios:action:nexloop.commitment.keep:1)."""
    PROTOCOL='nexloop-commitment-keep-v1';ACTION=KEEP_ACTION;FUNCTION='nexloop_commitment_command'

    def prepare_claim(self,claim_id):return self._signed({'verb':'prepare','claim_id':str(claim_id)})
    def prepare(self,commitment_id):return self._signed({'verb':'prepare','commitment_id':str(commitment_id)})
    def registered(self,commitment_id):return self._signed({'verb':'registered','commitment_id':str(commitment_id)})
    def evaluate(self,commitment_id):return self._signed({'verb':'evaluate','commitment_id':str(commitment_id)})
    def settle(self,commitment_id,fence=None):
        payload={'verb':'settle','commitment_id':str(commitment_id)}
        if fence is not None:payload['fence']=int(fence)
        return self._signed(payload)
    def exception(self,commitment_id,reason,detail):
        return self._signed({'verb':'exception','commitment_id':str(commitment_id),'reason':reason,'detail':dict(detail)})


class CommitmentReadPort(_SignedPort):
    """Commitments with their four evidence columns, events and exceptions (eios:action:nexloop.commitment.read:1)."""
    PROTOCOL='nexloop-commitment-read-v1';ACTION=READ_ACTION;FUNCTION='nexloop_commitment_read'

    def commitments(self,consumer_id=None,*,include_closed=False):
        payload={'verb':'commitments','all':bool(include_closed)}
        if consumer_id is not None:payload['consumer_id']=str(consumer_id)
        return self._signed(payload)['commitments']

    def commitment(self,commitment_id):return self._signed({'verb':'commitment','commitment_id':str(commitment_id)})['commitment']
    def exceptions(self):return self._signed({'verb':'exceptions'})['exceptions']


def _code(error):
    if isinstance(error,PlanUnavailable):return 'port_unavailable'
    if isinstance(error,(PermissionError,F.AuthorizationFactDenied,AuthorizationUnavailable)):return 'denied'
    return 'commitment_failed'


class CommitmentKeeper:
    """One tick over both feeds: register commitment Claims, then move commitments on their evidence."""

    def __init__(self,pool,session,signer,*,settings):
        self.pool,self.session,self.signer=pool,session,signer
        self.settings=settings;self.worker=settings['worker']
        self.port=CommitmentPort(pool,session,signer)
        self.register_feed=WorkFeed(pool,session,signer,feed=REGISTER_FEED)
        self.monitor_feed=WorkFeed(pool,session,signer,feed=MONITOR_FEED)

    def _retry(self,feed,item,code,summary):
        status=feed.retry(item_key=item['item_key'],fence=item['fence'],code=code,
            delay_seconds=retry_delay(item['attempts'],self.worker['retry_base_seconds']),max_attempts=self.worker['max_attempts'])
        summary['retry' if status=='pending' else status]+=1

    def _create(self,prepared):
        try:
            _,_,schemas=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get_with_schemas(*CREATE)
            if not any(schema.type_name==TYPE for schema in schemas):raise ActionAuthorizationDenied('Commitment type outside the Action')
        except Exception as error:
            # No published Commitment type / Commitment.create: the Claim stays unresolved and the owner sees the gap.
            self.port.exception(prepared['commitment_id'],'schema_gap',{'error':type(error).__name__})
            raise
        GovernedObjectCreator(self.pool,self.session,self.signer).create(action_name=CREATE[0],action_version=CREATE[1],
            intent_id=prepared['intent_id'],type_name=TYPE,properties=prepared['properties'])
        return self.port.registered(prepared['commitment_id'])

    def register(self,item):
        payload=item['payload']
        prepared=self.port.prepare_claim(payload['claim_id']) if 'claim_id' in payload else self.port.prepare(payload['commitment_id'])
        if prepared['action']!='create':return prepared['action']
        self._create(prepared)
        return 'registered'

    def monitor(self,item):
        commitment=item['payload']['commitment_id']
        evaluated=self.port.evaluate(commitment)
        applied=None
        if evaluated['patch']:
            revision=evaluated['revision']
            GovernedObjectEditor(self.pool,self.session,self.signer).edit(action_name=EDIT[0],action_version=EDIT[1],
                intent_id=f'commitment-{commitment}-r{revision}',type_name=TYPE,object_id=commitment,expected_revision=revision,properties=evaluated['patch'])
            applied=evaluated['patch'].get('status') or 'basis'
        settled=self.port.settle(commitment,item['fence'])
        return applied,settled

    @authority_request_scoped
    def run_once(self):
        summary={'registered':0,'reaffirmed':0,'skipped':0,'transitions':0,'settled':0,'retry':0,'dead_lettered':0,'changed':0,'lease_lost':0}
        for item in self.register_feed.claim(limit=self.worker['batch'],lease_seconds=self.worker['lease_seconds']):
            try:outcome=self.register(item)
            except WorkFeedDenied:raise
            except Exception as error:self._retry(self.register_feed,item,_code(error),summary);continue
            status=self.register_feed.complete(item_key=item['item_key'],fence=item['fence'])
            key={'registered':'registered','reaffirmed':'reaffirmed'}.get(outcome,'skipped')
            summary[key if status=='completed' else status]+=1
        for item in self.monitor_feed.claim(limit=self.worker['batch'],lease_seconds=self.worker['lease_seconds']):
            try:applied,_=self.monitor(item)
            except WorkFeedDenied:raise
            except Exception as error:self._retry(self.monitor_feed,item,_code(error),summary);continue
            if applied:summary['transitions']+=1
            status=self.monitor_feed.complete(item_key=item['item_key'],fence=item['fence'])
            summary['settled' if status in ('completed','changed') else status]+=1
        return summary


__all__=['CommitmentKeeper','CommitmentPort','CommitmentReadPort','load_settings']
