"""NX-026 commitment fixture on clean catalog PostgreSQL (synthetic data only).

Built on the actual governed effect executor (effect_execution_fixture, parameter 'commitment-service': a non-message
service Action that may name the commitment it fulfils): real Consumer, real Run submissions, real independent
effect Worker and loopback provider. On top of it:

* the Commitment type v1 from deploy/ontology/business-object-types.v1.json and the Commitment Actions compiled from
  deploy/configuration/business-actions.v1.json (published by admin as configuration fixtures, the way the goal and
  effect fixtures publish theirs);
* the commitment_keeper service on the domain-worker role, holding the manifest's grants (seeded) and the
  commitment_lifecycle property_access_rule (fact fixture, as in test_property_grant_derivation_pg);
* delivered outbound Agent messages (NX-047 ledger rows: intent, outbound record, conversation stream entry and the
  Message object) and enterprise commitment Claims, seeded by admin. The actual NX-047 chain and the actual
  extraction of such a Claim are covered end to end by test_commitment_e2e_pg.
Every commitment write after that goes through the keeper, the governed Actions and the SQL guards.
"""
from contextlib import ExitStack
from datetime import UTC,datetime,timedelta
import hashlib,json,secrets,uuid
from pathlib import Path

import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios import business_actions,business_object_types
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.commitments import CommitmentKeeper,CommitmentReadPort,load_settings
from nexloop_eios.effect_contexts import EFFECT
from nexloop_eios.property_access import PropertyAccessRule,PropertyGroupRestriction
from multi_authority_fixture import seed_multi_authority
from test_postgres_action_claims import governance_inputs

ROOT=Path(__file__).resolve().parents[1]
TENANT='synthetic-a'
SETTINGS=ROOT/'deploy/configuration/commitments.v1.json'
HUMAN_ACTIONS=('Commitment.cancel','Commitment.extend','Commitment.attest','Commitment.condition_met','Commitment.mark_communication')
KEEPER_TARGETS=[(r,ResourceType.ACTION,Operation.EXECUTE) for r in ('eios:action:NexLoop.feed.commitment-register:1','eios:action:NexLoop.feed.commitment-monitor:1',
    'eios:action:nexloop.commitment.keep:1','eios:action:nexloop.commitment.read:1','eios:action:Commitment.create:1','eios:action:Commitment.edit:1')]+[
    ('eios:object_type:Commitment',ResourceType.OBJECT_TYPE,Operation.READ)]


def ts(value):
    return value.astimezone(UTC).isoformat()


def publish_commitment_type(admin,tenant=TENANT,*,actions=True):
    rows=business_object_types.trusted_object_types(ROOT/'deploy/ontology/business-object-types.v1.json')
    for row in rows:
        admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,%s,%s) on conflict do nothing',
            (tenant,row['type_name'],row['version'],Jsonb(row)))
    if not actions:return rows
    base=governance_inputs()['capability_snapshot']
    caps={name:base.model_copy(update={'capability_name':name,'has_side_effects':True}) for name in
        ('ontology.object.create','ontology.object.edit','commitment.cancel','commitment.extend','commitment.attest','commitment.condition_met','commitment.mark_communication')}
    compiled=business_actions.compile_actions(business_actions.load(ROOT/'deploy/configuration/business-actions.v1.json'),tenant=tenant,
        created_by='synthetic-configuration',created_at=datetime.now(UTC),object_types=rows,capabilities=caps,
        select=('Commitment.create','Commitment.edit')+HUMAN_ACTIONS)
    for row in compiled:
        d=row['definition']
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
            (tenant,'real',f"eios:action:{d['stable_name']}:{d['version']}",Jsonb(d),Jsonb(row['capability'])))
    return rows


def lifecycle_rule(admin,principal,tenant=TENANT,**changes):
    value=dict(tenant_id=tenant,principal_id=principal,type_name='Commitment',operations=('edit','read'),property_groups=('commitment_lifecycle',),
        include_review_published=False,basis_schema_version=1,active=True,valid_until=datetime.now(UTC)+timedelta(days=1))|changes
    admin.execute('''insert into authz.nexloop_authority_facts values(%s,%s,%s,%s)
        on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload''',
        (tenant,'property_access_rule',[principal,'Commitment'],Jsonb(PropertyAccessRule(**value).model_dump(mode='json'))))
    # Derivation needs the owner's restriction decision for the type (0084). Deployment: owner-property-restrictions.json
    # is the owner's file and has no Commitment entry yet; here a synthetic owner decision restricts no group.
    admin.execute('''insert into authz.nexloop_authority_facts values(%s,%s,%s,%s)
        on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload''',
        (tenant,'property_group_restriction',['Commitment'],Jsonb(PropertyGroupRestriction(tenant_id=tenant,type_name='Commitment',
            restricted_groups=(),decision='synthetic owner decision').model_dump(mode='json'))))


class Commitments(dict):
    def __repr__(self):return '<synthetic NX-026 commitment configuration>'
    __str__=__repr__

    # -- services --------------------------------------------------------------------------------
    def keeper_session(self):return authenticate_service(self['worker'],self['keeper_token'],world='real')
    def keeper(self):return CommitmentKeeper(self['worker'],self.keeper_session(),self['signer'],settings=self['settings'])
    def reader(self):return CommitmentReadPort(self['worker'],self.keeper_session(),self['signer'])

    def tick(self,times=3):
        """Drain both feeds (a transition may schedule the next stage of the same item)."""
        out=[]
        for _ in range(times):
            out.append(self.keeper().run_once())
            if not any(v for k,v in out[-1].items()):break
        return out

    # -- effects bound to a commitment (actual Run submission and independent effect Worker) ----------
    def configure_categories(self,category='non_contact_service',notification=('message','notice')):
        """Trusted configuration (nexloop_configurator) of the fixture's service Action category (ADR-023 §3, 0112)."""
        from nexloop_eios.effect_categories import apply
        a=self['admin'];a.execute('alter role nexloop_configurator login')
        dsn=self['tmp_path']/'configurator-dsn';dsn.write_text(make_conninfo(self['pg'],user='nexloop_configurator'));dsn.chmod(0o600)
        manifest={'schema_version':'nexloop-action-effect-categories/1','manifest_version':1,'decision':'synthetic declaration',
            'actions':[{'stable_name':EFFECT,'version':1,'category':category,'notification_parameters':sorted(notification) if category=='non_contact_service' else [],
                'purpose':'synthetic service delivery'}]}
        return apply(manifest,TENANT,database_url_file=dsn)

    def submit(self,parameters):
        """A new plan step and Run submit one governed service.request intent (actual NX-015 path).

        Seeding authority moves the directory hash, so the planner and submitter re-authenticate first."""
        plan=self['plan'];backend=plan['backend'];self['steps']=self.get('steps',1)+1
        planner=backend.authenticate(plan['planner_token'],world='real');submitter=backend.authenticate(plan['submitter_token'],world='real')
        base=self['admin'].execute("select properties from ontology.objects where type_name='PlanStep' and object_id=%s",(plan['step'],)).fetchone()[0]
        step=planner.create_object(action_name='PlanStep.create',action_version=1,intent_id='commitment-plan-step-'+str(self['steps']),type_name='PlanStep',
            properties=base)['object_id']
        run=submitter.issue_run_credential(action_resources=['eios:action:'+EFFECT+':1'])
        planner.bind_effect_context(step_id=step,step_revision=1,goal_revision=1,consumer_revision=1,control_revision=1,
            run_id=run.run_id,run_token=run.token,executor_token=plan['executor_token'])
        return backend.authenticate_run(run.token,world='real',run_id=run.run_id).submit_effect_intent(parameters=parameters)

    def issue_run(self):
        plan=self['plan'];submitter=plan['backend'].authenticate(plan['submitter_token'],world='real')
        return submitter.issue_run_credential(action_resources=['eios:action:'+EFFECT+':1'])

    def dispatch(self,provider):
        from test_nx022_dispatch_e2e import dispatch_once
        return dispatch_once(self['fixture'],provider)

    def deliver(self,provider,intent_id):
        """Independent effect Worker: POST (provider accepted), the provider completes the service, then the next Worker
        observes it after the 3 s Action/effect lease and finalizes the receipt (fulfilled)."""
        import time
        first=self.dispatch(provider)
        assert first['claimed'] is True and first['status']!='admission_unavailable',first
        provider.control('fulfill',intent_id=intent_id);time.sleep(3.1)
        second=self.dispatch(provider)
        assert second['status']=='fulfilled',second
        return second

    def owner(self,identity,uow):
        """The human owner's governed commitment Actions (actual browser HUMAN session, NX-022 governed entry)."""
        from goal_fixture import authenticate_human,seed_human_owner
        from nexloop_eios.goal_controls import GoalGovernedActions
        pool=self['plan']['backend']._pool
        if 'browser' not in self:self['browser']=seed_human_owner(self['admin'],pool,list(HUMAN_ACTIONS),identity,uow)
        human=authenticate_human(pool,self['browser'])
        return GoalGovernedActions(pool,human,self['signer']),human

    def service_actions(self,suffix='-commitment-service'):
        """A service principal holding the very same human commitment Action grants (must be refused)."""
        from goal_fixture import seed_service
        from nexloop_eios.goal_controls import GoalGovernedActions
        pool=self['plan']['backend']._pool
        token=seed_service(self['admin'],pool,list(HUMAN_ACTIONS),suffix=suffix)
        actions=GoalGovernedActions(pool,authenticate_service(pool,token,world='real'),self['signer']);actions.token=token
        return actions

    def service_actions_session(self,actions):
        """The same service principal re-authenticated after later seeding."""
        from nexloop_eios.goal_controls import GoalGovernedActions
        pool=self['plan']['backend']._pool
        again=GoalGovernedActions(pool,authenticate_service(pool,actions.token,world='real'),self['signer']);again.token=actions.token
        return again

    # -- NX-047 ledger seeds ------------------------------------------------------------------------
    def outbound(self,body,*,accepted_at=None,consumer=None):
        """A delivered Agent reply: effect intent, outbound record, stream entry, Message object (admin seeds)."""
        a=self['admin'];consumer=consumer or self['consumer'];self['n']+=1;n=self['n']
        accepted_at=accepted_at or datetime.now(UTC)
        intent=uuid.uuid4();message=hashlib.sha256(f'outbound-{n}-{secrets.token_hex(4)}'.encode()).hexdigest()
        conversation=self.conversation(consumer)
        with a.transaction():
            a.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
            a.execute('''insert into runtime.nexloop_effect_intents(intent_id,receipt_id,context_id,tenant_id,world,consumer_id,goal_identity,plan_step_identity,slot_identity,
                action_name,action_version,business_digest,provider_payload_digest,frozen_request,action_definition,capability,executor_principal,control_revision,created_at,state,governed_claim_finalized)
                values(%s,%s,%s,%s,'real',%s,'synthetic-goal','synthetic-step',%s,'nexloop.service.request',1,%s,%s,%s,'{}','{}','synthetic-executor',1,clock_timestamp(),'fulfilled',true)''',
                (intent,uuid.uuid4(),self['context_id'],TENANT,consumer,f'outbound-{n}','a'*64,'b'*64,Jsonb({'parameters':{'message':body}})))
            seq=a.execute('update runtime.nexloop_conversations set last_sequence=last_sequence+1 where tenant_id=%s and conversation_id=%s returning last_sequence',(TENANT,conversation)).fetchone()[0]
            record={'direction':'outbound','sender_kind':'agent','intent_id':str(intent),'trigger_message_id':'c'*64,'body':body,'accepted_at':ts(accepted_at),
                'conversation_id':conversation,'sequence':seq,'actor':'synthetic-agent-principal','status':'accepted'}
            a.execute('''insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
                values(%s,'real',%s,%s,%s,%s,%s,%s)''',(TENANT,conversation,seq,message,'agent-'+str(intent),'d'*64,Jsonb(record)))
            a.execute('''insert into runtime.nexloop_outbound_messages(tenant_id,world,intent_id,receipt_id,conversation_id,trigger_message_id,run_id,sender_kind,sender_principal,
                role_ref,body_digest,delivery_state,message_id,sequence,materialized_at) values(%s,'real',%s,%s,%s,%s,%s,'agent','synthetic-agent-principal','role:synthetic',%s,'delivered',%s,%s,clock_timestamp())''',
                (TENANT,intent,uuid.uuid4(),conversation,'c'*64,uuid.uuid4(),hashlib.sha256(body.encode()).hexdigest(),message,seq))
            a.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
                values(%s,'real','Message',%s,1,%s,'nexloop-action',%s,clock_timestamp(),clock_timestamp())''',
                (TENANT,message,Jsonb({'conversation_id':conversation,'sequence':seq,'actor':'synthetic-agent-principal','body':body,'accepted_at':ts(accepted_at)}),'agent-message-'+str(intent)))
        return {'message_id':message,'conversation_id':conversation,'intent_id':str(intent),'body':body,'accepted_at':accepted_at}

    def inbound(self,body,*,consumer=None):
        """An accepted inbound consumer message in the stream (admin seed of the 0046 write); the 0109 trigger runs on it."""
        a=self['admin'];consumer=consumer or self['consumer'];conversation=self.conversation(consumer);self['n']+=1
        message=hashlib.sha256(f'inbound-{self["n"]}-{secrets.token_hex(4)}'.encode()).hexdigest()
        with a.transaction():
            a.execute("select set_config('eios.tenant_id',%s,true)",(TENANT,))
            seq=a.execute('update runtime.nexloop_conversations set last_sequence=last_sequence+1 where tenant_id=%s and conversation_id=%s returning last_sequence',(TENANT,conversation)).fetchone()[0]
            a.execute('''insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
                values(%s,'real',%s,%s,%s,%s,%s,%s)''',(TENANT,conversation,seq,message,'synthetic-inbound-'+str(self['n']),'d'*64,
                Jsonb({'body':body,'accepted_at':ts(datetime.now(UTC)),'conversation_id':conversation,'sequence':seq,'actor':'synthetic-human-principal'})))
        return message

    def conversation(self,consumer):
        key=self.setdefault('conversations',{})
        if consumer not in key:
            cid=hashlib.sha256(('conversation-'+consumer).encode()).hexdigest()
            self['admin'].execute('''insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key)
                values(%s,'real',%s,%s,'synthetic-human-principal',%s)''',(TENANT,cid,consumer,'conversation-'+consumer[:16]))
            key[consumer]=cid
        return key[consumer]

    def claim(self,message,quote,*,predicate='处理进展反馈',modality='asserted',condition='',valid_time=None,speaker='agent',polarity='affirmed',
              kind='commitment',correlation=None,resolution='unresolved',consumer=None):
        """An extracted Claim row (admin seed of NX-019's output); the claim trigger marks the register feed."""
        consumer=consumer or self['consumer'];body=message['body']
        start=body.index(quote);claim_id=secrets.token_hex(32)
        valid_time=valid_time or {'expression':'','kind':'none','status':'absent','start':None,'end':None,'timezone':'Asia/Shanghai','anchor':ts(message['accepted_at'])}
        correlation=correlation or hashlib.sha256(('|'.join([consumer,kind,predicate,polarity])).encode()).hexdigest()
        self['admin'].execute('''insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,subject_text,
            predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,source_content_hash,quote,
            extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags)
            values(%s,'real',%s,%s,%s,%s,'topic','enterprise','','',%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,'synthetic-extractor',0.9,%s,%s,'[]',%s,'[]')''',
            (TENANT,claim_id,message['conversation_id'],consumer,'e'*64,predicate,Jsonb({'type':'string','value':quote}),speaker,polarity,modality,condition,
             valid_time.get('expression',''),Jsonb(valid_time),message['message_id'],start,start+len(quote),hashlib.sha256(body.encode()).hexdigest(),quote,
             kind,resolution,correlation))
        return claim_id

    # -- probes ----------------------------------------------------------------------------------
    def commitment(self,commitment_id):
        row=self['admin'].execute("select properties,nexloop_revision from ontology.objects where tenant_id=%s and type_name='Commitment' and object_id=%s",
            (TENANT,commitment_id)).fetchone()
        return None if row is None else {'properties':row[0],'revision':row[1]}

    def by_claim(self,claim_id):
        row=self['admin'].execute('select commitment_id from runtime.nexloop_commitments where tenant_id=%s and claim_id=%s',(TENANT,claim_id)).fetchone()
        return row and row[0]

    def exceptions(self,commitment_id=None):
        rows=self['admin'].execute('select subject_ref,reason,detail from runtime.nexloop_commitment_exceptions where tenant_id=%s order by raised_at,reason',(TENANT,)).fetchall()
        return [r for r in rows if commitment_id is None or r[0]=='commitment:'+commitment_id]

    def due_now(self,commitment_id,*,delta=timedelta(seconds=-1)):
        """Explicit time injection: the monitor item of this commitment is due now (documented in each test)."""
        self['admin'].execute("update runtime.nexloop_work_feed set available_at=clock_timestamp()+%s where feed='commitment-monitor' and item_key=%s",
            (delta,'commitment:'+commitment_id))


def deadline(message,end,*,status='resolved',window=None):
    vt={'expression':'明天下午前','kind':'deadline','status':status,'granularity':'part_of_day','start':None,'end':ts(end),'timezone':'Asia/Shanghai',
        'anchor':ts(message['accepted_at'])}
    if window is not None:vt['latest_bound_window']=[ts(window[0]),ts(window[1])]
    return vt


@pytest.fixture
def commitments(governed_effect_executor,admin,pg,tmp_path):
    """Requires @pytest.mark.parametrize('execution_plan',['commitment-service'],indirect=True) on the test."""
    fixture=governed_effect_executor;plan=fixture.plan
    publish_commitment_type(admin)
    with ExitStack() as stack:
        worker=stack.enter_context(open_core(make_conninfo(pg,user='nexloop_domain_worker')))
        _,keeper_token=seed_multi_authority(admin,worker,KEEPER_TARGETS,identity_suffix='-commitment-keeper',tenant=TENANT)
        principal=admin.execute("select entity_key[1] from authz.nexloop_authority_facts where fact_kind='grants' and entity_key[2]='eios:action:nexloop.commitment.keep:1'").fetchone()[0]
        lifecycle_rule(admin,principal)
        c=Commitments(admin=admin,pg=pg,tmp_path=tmp_path,fixture=fixture,plan=plan,worker=worker,signer=plan['backend']._signer,
            consumer=plan['consumer'],context_id=fixture.context_id,keeper_token=keeper_token,keeper_principal=principal,
            settings=load_settings(SETTINGS),n=0)
        yield c
