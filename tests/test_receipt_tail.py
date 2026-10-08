"""Normal governed POST/Role.end/GET chain, then actual PG tail fault boundaries."""
import hashlib,hmac,json,time,threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,UTC,timedelta
import pytest,psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from role_run_fixture import role_runtime_plan
from runtime_effect_fixture import runtime_effect_plan
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_execution import EffectExecutionPort,EffectExecutionUnavailable,QUERY
from nexloop_eios.effect_dispatch import EffectDispatcher,EffectDispatchUnavailable
from nexloop_eios.effect_provider import HttpEffectProvider,EffectProviderConfiguration
from nexloop_eios.postgres_artifacts import canonical_payload
from support.effect_provider import effect_provider


def _shorten(port,envelope,seconds):
    text,signature,payload=envelope;c=json.loads(text)
    c['expires_at']=(datetime.now(UTC)+timedelta(seconds=seconds)).isoformat()
    text=canonical_payload(c)
    signature=hmac.new(port.signer.material,('nexloop-effect-execution-v1:'+text).encode(),'sha256').hexdigest()
    return text,signature,payload


def _assert_unfinished(admin,intent):
    assert admin.execute('select state,governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==('observed_fulfilled',False)
    assert admin.execute("select claim->>'state' from runtime.nexloop_action_claims where action_name='nexloop.service.request' and intent_id=%s",(intent,)).fetchone()[0]!='terminal'
    assert admin.execute('select count(*) from runtime.nexloop_effect_recovery_audits where intent_id=%s',(intent,)).fetchone()==(0,)
    assert admin.execute("select count(*) from runtime.nexloop_action_claims where action_name='nexloop.service.receipt_reconcile'",()).fetchone()==(0,)


@pytest.mark.parametrize('fault',['mixed_action','query_rollover','nested_expiry','claim_expiry','self_terminal_missing'])
def test_current_recovery_definition_and_nested_tail(role_runtime_plan,admin,tmp_path,monkeypatch,fault):
    plan=role_runtime_plan
    receipt=plan['worker'].runtime_effect_tool(activation_ref=plan['activations'][0],command=plan['commands'][0],tool_operation='submit',parameters={'message':'real governed tail evidence'})
    intent=receipt['receipt']['intent_id']
    with open_backend(database_url=make_conninfo(plan['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'tail-worker',signing_key_file=plan['signing_key'],signing_key_id=plan['signing_key_id']) as backend:
        worker=backend.authenticate(plan['executor_token'],world='real')
        with effect_provider(tmp_path/'tail-provider.sqlite') as provider:
            http=HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1))
            dispatcher=EffectDispatcher(worker,http,lease_seconds=3)
            assert dispatcher.run_once()['provider_state']=='accepted'
            plan['end_role'](plan['runs'][0].run_id);provider.control('fulfill',intent_id=intent);time.sleep(3.1)
            # Get actual durable QUERY evidence with a long current effect lease.
            job=worker.claim_effect(lease_seconds=30);args=dict(intent_id=intent,fence=job['fence'])
            admission=worker.authorize_effect_query(**args,provider_profile_digest=http.profile_digest)
            query=http.query(intent_id=intent,payload_digest=admission['provider_payload_digest'])
            original_reconcile=EffectExecutionPort.reconcile_effect_receipt
            def withheld(*args,**kwargs):raise EffectExecutionUnavailable()
            monkeypatch.setattr(EffectExecutionPort,'reconcile_effect_receipt',withheld)
            observed=worker.record_effect_query_observation(**args,query_id=admission['query_id'],provider_profile_digest=http.profile_digest,
                provider_payload_digest=query.payload_digest,provider_state=query.state,provider_reference=query.provider_reference)
            assert observed['state']=='observed_fulfilled'
            monkeypatch.setattr(EffectExecutionPort,'reconcile_effect_receipt',original_reconcile)
            port=EffectExecutionPort(backend._pool,worker._session,backend._signer)
            metadata=port._resolve(dict(intent_id=intent,effect_fence=job['fence']))
            errors=[];execute=EffectExecutionPort._recovery_execute
            def capture(self,*args,**kwargs):
                try:return execute(self,*args,**kwargs)
                except psycopg.Error as error:errors.append(error.diag.message_primary);raise
            monkeypatch.setattr(EffectExecutionPort,'_recovery_execute',capture)
            if fault=='mixed_action':
                envelope=EffectExecutionPort._action_envelope
                def mixed(self,command,verb):
                    if command.key.action_stable_name=='nexloop.service.receipt_reconcile':
                        # Another genuinely currently granted Action; no fabricated actor or Source.
                        command=command.model_copy(update={'key':command.key.model_copy(update={'action_stable_name':'nexloop.service.query'}),
                            'binding':command.binding.model_copy(update={'action_reference':command.binding.action_reference.model_copy(update={'stable_name':'nexloop.service.query'})})})
                    return envelope(self,command,verb)
                monkeypatch.setattr(EffectExecutionPort,'_action_envelope',mixed)
            elif fault=='query_rollover':
                tenant=worker._session.authentication.tenant_id
                definition,capability=admin.execute("select definition,capability from control.nexloop_action_definitions where tenant_id=%s and resource_id='eios:action:nexloop.service.query:1'",(tenant,)).fetchone()
                historic_definition=json.loads(json.dumps(definition))
                definition['version']=2;definition['governance']['risk_level']='high'
                from eios.ontology.definitions import ActionDefinition
                definition.pop('contract_digest',None)
                definition=ActionDefinition.model_validate_json(json.dumps(definition)).model_dump(mode='json')
                admin.execute("insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,'real','eios:action:nexloop.service.query:2',%s,%s)",(tenant,Jsonb(definition),Jsonb(capability)))
                admin.execute("update control.nexloop_action_definitions set active=false where tenant_id=%s and resource_id='eios:action:nexloop.service.query:1'",(tenant,))
                worker=backend.authenticate(plan['executor_token'],world='real')
                monkeypatch.setattr(EffectExecutionPort,'_resolve',lambda self,identity:metadata)
                signed=EffectExecutionPort._signed
                def historical_query_binding(self,verb,*,target,**parameters):
                    if verb!='query_authority':return signed(self,verb,target=target,**parameters)
                    # Genuine CURRENT QUERY authority; historical immutable v1 definition is deliberately
                    # supplied as caller metadata to verify SQL rejects its now-inactive publication.
                    proof,_=self._proof(self.session,QUERY);payload=canonical_payload({'verb':verb,**parameters})
                    c={'protocol':'nexloop-effect-execution-v1','key_id':self.signer.key_id,**proof,
                        'definition':historic_definition,'capability':capability,'parameters_digest':hashlib.sha256(payload.encode()).hexdigest()}
                    text=canonical_payload(c);signature=hmac.new(self.signer.material,('nexloop-effect-execution-v1:'+text).encode(),'sha256').hexdigest()
                    return text,signature,payload
                monkeypatch.setattr(EffectExecutionPort,'_signed',historical_query_binding)
            elif fault in ('nested_expiry','claim_expiry'):
                # Technical fault hook after actual nested reserve and both terminal writes.
                admin.execute("create function runtime.synthetic_tail_delay() returns trigger language plpgsql as $$begin perform pg_sleep(1.2);return new;end$$")
                admin.execute('create trigger synthetic_tail_delay before insert on runtime.nexloop_effect_recovery_audits for each row execute function runtime.synthetic_tail_delay()')
                envelope=EffectExecutionPort._action_envelope
                def short(self,command,verb):
                    if fault=='claim_expiry' and command.key.action_stable_name=='nexloop.service.receipt_reconcile':
                        command=command.model_copy(update={'lease_expires_at':datetime.now(UTC)+timedelta(seconds=.5)})
                    env=envelope(self,command,verb)
                    if fault=='nested_expiry' and command.key.action_stable_name=='nexloop.service.receipt_reconcile':
                        c=json.loads(env['text']);c['expires_at']=(datetime.now(UTC)+timedelta(seconds=.5)).isoformat()
                        env['text']=canonical_payload(c);env['signature']=hmac.new(self.signer.material,('nexloop-action-command-v1:'+env['text']).encode(),'sha256').hexdigest()
                    return env
                monkeypatch.setattr(EffectExecutionPort,'_action_envelope',short)
            elif fault=='self_terminal_missing':
                admin.execute("create function runtime.synthetic_terminal_skip() returns trigger language plpgsql as $$begin if new.action_name='nexloop.service.receipt_reconcile' then return null;end if;return new;end$$")
                admin.execute('create trigger synthetic_terminal_skip before update on runtime.nexloop_action_claims for each row execute function runtime.synthetic_terminal_skip()')

            with pytest.raises(EffectExecutionUnavailable):worker.reconcile_effect_receipt(**args,query_id=admission['query_id'])
            if fault=='mixed_action':assert 'recovery action binding unavailable' in errors
            if fault=='query_rollover':assert 'current query contract required' in errors
            if fault=='nested_expiry':assert any(error in ('action authority binding stale or invalid','action authority epoch changed','recovery authority expired') for error in errors)
            if fault=='claim_expiry':assert 'recovery authority expired' in errors
            if fault=='self_terminal_missing':assert 'recovery terminal row unavailable' in errors
            _assert_unfinished(admin,intent)
            assert provider.control('snapshot')['requests']==[('POST',intent,202),('GET',intent,200)]


def test_query_admission_expiry_after_actual_original_claim_lock_wait(role_runtime_plan,admin,tmp_path,monkeypatch):
    plan=role_runtime_plan
    receipt=plan['worker'].runtime_effect_tool(activation_ref=plan['activations'][0],command=plan['commands'][0],tool_operation='submit',parameters={'message':'real query lock wait'})
    intent=receipt['receipt']['intent_id']
    with open_backend(database_url=make_conninfo(plan['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'lock-worker',signing_key_file=plan['signing_key'],signing_key_id=plan['signing_key_id']) as backend:
        worker=backend.authenticate(plan['executor_token'],world='real')
        with effect_provider(tmp_path/'lock-provider.sqlite') as provider:
            http=HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1))
            assert EffectDispatcher(worker,http,lease_seconds=3).run_once()['provider_state']=='accepted'
            plan['end_role'](plan['runs'][0].run_id);provider.control('fulfill',intent_id=intent);time.sleep(3.1)
            job=worker.claim_effect(lease_seconds=30);identity=dict(intent_id=intent,effect_fence=job['fence'])
            port=EffectExecutionPort(backend._pool,worker._session,backend._signer)
            signed=EffectExecutionPort._signed
            def short_admission(self,verb,*,target,**parameters):
                value=signed(self,verb,target=target,**parameters)
                return _shorten(self,value,1.5) if verb=='admit_query' else value
            monkeypatch.setattr(EffectExecutionPort,'_signed',short_admission)
            admission=worker.authorize_effect_query(intent_id=intent,fence=job['fence'],provider_profile_digest=http.profile_digest)
            query=http.query(intent_id=intent,payload_digest=admission['provider_payload_digest'])
            revision=admin.execute('select attempt_revision from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()[0]
            text,signature,payload=port._signed('observe',target=QUERY,**identity,attempt_revision=revision,provider_profile_digest=http.profile_digest,
                provider_payload_digest=query.payload_digest,provider_state=query.state,provider_reference=query.provider_reference)
            def observe():
                with backend._pool.connection() as db,db.transaction():
                    return port._recovery_execute(db,'observe_query',QUERY,**identity,query_id=admission['query_id'],
                        observation_envelope=dict(text=text,signature=signature,payload=payload))
            with psycopg.connect(plan['pg']) as blocker,ThreadPoolExecutor(max_workers=1) as executor:
                blocker.execute("select claim from runtime.nexloop_action_claims where action_name='nexloop.service.request' and intent_id=%s for update",(intent,))
                future=executor.submit(observe)
                deadline=time.monotonic()+1
                while time.monotonic()<deadline:
                    waiting=admin.execute("select count(*) from pg_stat_activity where usename='nexloop_action_worker' and wait_event_type='Lock' and query like 'select authz.nexloop_receipt_reconcile_command%'").fetchone()[0]
                    if waiting:break
                    time.sleep(.02)
                assert waiting
                time.sleep(1.6);blocker.commit()
                with pytest.raises(psycopg.errors.InsufficientPrivilege):future.result(timeout=5)
            assert admin.execute('select observation_id from runtime.nexloop_effect_query_admissions where query_id=%s',(admission['query_id'],)).fetchone()==(None,)
            assert admin.execute("select count(*) from runtime.nexloop_effect_observations where intent_id=%s and provider_state='fulfilled'",(intent,)).fetchone()==(0,)
            assert provider.control('snapshot')['requests']==[('POST',intent,202),('GET',intent,200)]


@pytest.mark.parametrize('constraint',['risk','approval','precondition'])
def test_initial_constrained_query_publication_rejects_entry_without_fake_lineage(request,monkeypatch,admin,tmp_path,constraint):
    import runtime_effect_fixture as fixture
    from nexloop_eios.effect_execution import EffectExecutionPort
    config={'governance':{'risk_level':'high'}} if constraint=='risk' else ({'governance':{'approval_mode':'required'}} if constraint=='approval' else {'definition':{'preconditions':[{'name':'synthetic.query.guard','expression':{'current_guard':True},'property_dependencies':[]}]}})
    monkeypatch.setattr(fixture,'_INITIAL_QUERY_CONSTRAINTS',config,raising=False)
    plan=request.getfixturevalue('role_runtime_plan')
    submitted=plan['worker'].runtime_effect_tool(activation_ref=plan['activations'][0],command=plan['commands'][0],tool_operation='submit',parameters={'message':'constrained query publication entry'})
    intent=submitted['receipt']['intent_id']
    with open_backend(database_url=make_conninfo(plan['pg'],user='nexloop_action_worker'),artifact_root=tmp_path/'constrained-worker',signing_key_file=plan['signing_key'],signing_key_id=plan['signing_key_id']) as backend:
        worker=backend.authenticate(plan['executor_token'],world='real');errors=[]
        execute=EffectExecutionPort._execute
        def capture(self,*args,**kwargs):
            try:return execute(self,*args,**kwargs)
            except psycopg.Error as error:errors.append(error.diag.message_primary);raise
        monkeypatch.setattr(EffectExecutionPort,'_execute',capture)
        with effect_provider(tmp_path/'constrained-provider.sqlite') as provider:
            with pytest.raises(EffectDispatchUnavailable):EffectDispatcher(worker,HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1)),lease_seconds=3).run_once()
            assert ('effect published Action stale' if constraint=='precondition' else 'effect published query unavailable') in errors
            assert provider.control('snapshot')['requests']==[]
            assert admin.execute('select count(*) from runtime.nexloop_effect_query_admissions where intent_id=%s',(intent,)).fetchone()==(0,)
            assert admin.execute('select count(*) from runtime.nexloop_effect_recovery_audits where intent_id=%s',(intent,)).fetchone()==(0,)
