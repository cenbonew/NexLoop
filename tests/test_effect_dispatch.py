"""Real restricted Backend + governed41 plan + protected42 ledger + durable HTTP.

All identities/configuration are synthetic and isolated. Every business object
uses actual governed Actions. No authorization stub or production access.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import signal
import threading
import time

import pytest
from nexloop_eios.backend import open_backend
from nexloop_eios.effect_provider import HttpEffectProvider,EffectProviderConfiguration
from support.effect_provider import effect_provider
from effect_execution_fixture import governed_effect_executor,execution_plan


def _run_worker(configuration,origin,pipe,hold_terminal_ack=False,pause_before_admit=False,start_barrier=False):
    # Import must fail if production executor is absent. There is intentionally
    # no fallback, no Memory port and no object that manufactures success.
    from nexloop_eios.effect_dispatch import EffectDispatcher
    private=dict(configuration);token=private.pop('service_token');world=private.pop('world')
    stage='backend_open'
    try:
        with open_backend(**private) as backend:
            stage='authenticate'
            executor=backend.authenticate(token,world=world)
            stage='provider_configuration'
            provider=HttpEffectProvider(EffectProviderConfiguration(origin,test_loopback_http=True,timeout=1))
            pipe.send({'event':'worker_ready'})
            stage='dispatch'
            if start_barrier:pipe.recv()
            class AdmissionBarrier:
                def __getattr__(self,name):return getattr(executor,name)
                def prepare_effect_dispatch(self,**arguments):
                    pipe.send({'event':'before_admit'});pipe.recv()
                    return executor.prepare_effect_dispatch(**arguments)
            actual=AdmissionBarrier() if pause_before_admit else executor
            result=EffectDispatcher(actual,provider,lease_seconds=3).run_once()
            if hold_terminal_ack and result.get('business_action_success') is True:
                pipe.send({'event':'terminal_committed'});pipe.recv()
            # Allowlist only technical IDs/status; no parameters/raw error text.
            pipe.send({'event':'settled','claimed':result['claimed'],
                       'status':result.get('status'),
                       'business_action_success':result.get('business_action_success'),
                       'provider_state':result.get('provider_state')})
    except Exception:
        pipe.send({'event':'worker_unavailable','stage':stage})
        raise RuntimeError('effect_worker_unavailable') from None
    finally:pipe.close()


@contextmanager
def independent_worker(fixture,origin,*,hold_terminal_ack=False,pause_before_admit=False,start_barrier=False):
    context=multiprocessing.get_context('spawn');parent,child=context.Pipe()
    process=context.Process(target=_run_worker,args=(fixture.executor_configuration(),origin,child,hold_terminal_ack,pause_before_admit,start_barrier))
    process.start();child.close()
    try:
        assert parent.poll(15)
        event=parent.recv()
        if event!={'event':'worker_ready'}:process.join(5)
        assert event=={'event':'worker_ready'}
        yield process,parent
    finally:
        if process.is_alive():process.kill()
        process.join(10);parent.close();assert not process.is_alive()


def accepted(fixture):
    first=fixture.ports[0].submit_effect_intent(parameters={'message':'one governed service'})
    replay=fixture.ports[1].submit_effect_intent(parameters={'message':'one governed service'})
    assert first==replay and first['state']=='accepted'
    return first


def test_two_principals_concurrent_submit_and_independent_workers_single_effect(governed_effect_executor,admin,tmp_path):
    fixture=governed_effect_executor;barrier=threading.Barrier(2)
    def submit(index):
        with fixture.current_submitter(index) as service:
            principal=service._session.authentication.subject_principal_id
            barrier.wait(timeout=10)
            return principal,service.submit_effect_intent(parameters={'message':'one governed service'})
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(submit,range(2)))
    assert results[0][0]!=results[1][0]
    receipt=results[0][1];intent=receipt['intent_id']
    assert results[1][1]==receipt and receipt['state']=='accepted'
    assert admin.execute('select count(distinct principal_id),count(*) from runtime.nexloop_effect_submissions where intent_id=%s',(intent,)).fetchone()==(2,2)
    assert admin.execute('select budget_units,reserved_units from control.nexloop_effect_control_ledger where control_id=%s',(fixture.plan['control'],)).fetchone()==(1,1)
    assert admin.execute('select count(*) from runtime.nexloop_effect_control_reservations where intent_id=%s',(intent,)).fetchone()==(1,)
    with effect_provider(tmp_path/'concurrent-provider.sqlite') as provider:
        provider.control('pause')
        with independent_worker(fixture,provider.origin,start_barrier=True) as (first,first_pipe), independent_worker(fixture,provider.origin,start_barrier=True) as (second,second_pipe):
            first_pipe.send('start');second_pipe.send('start')
            assert provider.wait('accepted_committed')['intent_id']==intent
            provider.control('release')
            settled=[]
            for process,pipe in ((first,first_pipe),(second,second_pipe)):
                assert pipe.poll(15);settled.append(pipe.recv());process.join(10)
                assert process.exitcode==0
            assert sum(result['claimed'] is True for result in settled)==1
            winner=next(result for result in settled if result['claimed'])
            assert winner['status']=='dispatching' and winner['provider_state']=='accepted'
            assert winner['business_action_success'] is False
        snapshot=provider.control('snapshot')
        assert snapshot['effects']==1 and snapshot['requests']==[('POST',intent,202)]
    for port in fixture.ports:
        current=port.find_effect_receipt(intent_id=intent)
        assert current['receipt_id']==receipt['receipt_id'] and current['intent_id']==intent
    assert admin.execute('select budget_units,reserved_units from control.nexloop_effect_control_ledger where control_id=%s',(fixture.plan['control'],)).fetchone()==(1,1)
    assert admin.execute('select count(*) from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()==(1,)


def test_provider_commit_then_worker_sigkill_recovers_query_first(governed_effect_executor,admin,tmp_path):
    fixture=governed_effect_executor;receipt=accepted(fixture);intent=receipt['intent_id']
    with effect_provider(tmp_path/'independent-provider.sqlite') as provider:
        provider.control('pause')
        with independent_worker(fixture,provider.origin) as (old,pipe):
            assert provider.wait('accepted_committed')['intent_id']==intent
            # Provider barrier alone is insufficient: actual PG must show a
            # committed attempt before sending and no terminal local outcome.
            intent_row=admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()
            lease=admin.execute('select state,fence from runtime.nexloop_effect_outbox where intent_id=%s',(intent,)).fetchone()
            assert intent_row==('dispatching',) and lease[0]=='leased'
            # Actual protected PG attempt is committed before network dispatch.
            attempt=admin.execute('select state,provider_key,action_fencing_token from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()
            assert attempt[0]=='dispatching' and attempt[1]==intent and attempt[2]
            assert not pipe.poll(), 'local outcome escaped paused provider response'
            old.kill();old.join(10);assert old.exitcode==-signal.SIGKILL
        before=provider.control('snapshot');assert before['effects']==1
        assert before['requests']==[('POST',intent,202)]
        provider.control('release')
        time.sleep(3.1)  # actual Action lease expiry, not barrier timing guess
        with independent_worker(fixture,provider.origin) as (new,pipe):
            assert pipe.poll(15);settled=pipe.recv();new.join(10)
            assert new.exitcode==0 and settled['claimed'] is True
            assert settled['status']=='dispatching' and settled['provider_state']=='accepted'
            assert settled['business_action_success'] is False
        after=provider.control('snapshot')
        assert after['effects']==1 and after['requests']==before['requests']+[('GET',intent,200)]
        current=fixture.ports[0].find_effect_receipt(intent_id=intent)
        assert current['intent_id']==intent and current['receipt_id']==receipt['receipt_id']
        # Provider accepted is known; business fulfillment remains unconfirmed.
        observed=admin.execute('select provider_state from runtime.nexloop_effect_observations where intent_id=%s order by observed_at desc limit 1',(intent,)).fetchone()
        assert observed==('accepted',)
        assert current['state'] not in {'fulfilled','confirmed'}
        assert admin.execute('select fence from runtime.nexloop_effect_outbox where intent_id=%s',(intent,)).fetchone()[0]>lease[1]


@pytest.mark.parametrize('revocation',['source_run','source_grant','control'])
def test_dispatch_rechecks_current_source_and_control_zero_send(governed_effect_executor,admin,tmp_path,revocation):
    fixture=governed_effect_executor;receipt=accepted(fixture)
    with effect_provider(tmp_path/'provider.sqlite') as provider:
        with independent_worker(fixture,provider.origin,pause_before_admit=True) as (worker,pipe):
            assert pipe.poll(15) and pipe.recv()=={'event':'before_admit'}
            if revocation=='source_run':
                for run in fixture.runs:
                    admin.execute("update authz.nexloop_run_credentials set status='revoked' where run_id=%s",(run.run_id,))
            elif revocation=='source_grant':fixture.revoke_all_source_grants()
            else:fixture.revoke_control()
            pipe.send('release')
            assert pipe.poll(15);result=pipe.recv();worker.join(10)
            assert worker.exitcode==0 and result['status']=='admission_unavailable'
            assert result['business_action_success'] is False
        snapshot=provider.control('snapshot');assert snapshot['effects']==0 and snapshot['requests']==[]
        state=admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s',(receipt['intent_id'],)).fetchone()[0]
        assert state not in {'fulfilled','confirmed'}


def test_query_only_after_revoke_observes_fulfillment_without_claim_success(governed_effect_executor,admin,tmp_path):
    fixture=governed_effect_executor;receipt=accepted(fixture);intent=receipt['intent_id']
    with effect_provider(tmp_path/'provider-query-only.sqlite') as provider:
        provider.control('pause')
        with independent_worker(fixture,provider.origin) as (old,pipe):
            assert provider.wait('accepted_committed')['intent_id']==intent
            old.kill();old.join(10);assert old.exitcode==-signal.SIGKILL
        provider.control('release');provider.control('fulfill',intent_id=intent)
        # Actual controlled query READ authority remains available to executor;
        # revoke EXECUTE/source/control permission before B can finalize claim.
        # The fixture must establish this independent READ scope in real EIOS,
        # not reuse the old execution grant or return a fake query permit.
        fixture.revoke_all_source_grants()
        # 0065: an executor holding independent current recovery authority may still
        # finalize; this case keeps the no-recovery-authority meaning explicitly.
        fixture.revoke_executor_recovery_grant()
        before=provider.control('snapshot');time.sleep(3.1)
        with independent_worker(fixture,provider.origin) as (new,pipe):
            assert pipe.poll(15);result=pipe.recv();new.join(10)
            assert new.exitcode==0 and result['claimed'] is True
            assert result['status']=='observed_fulfilled'
            assert result['business_action_success'] is False
        after=provider.control('snapshot')
        assert after['effects']==1 and after['requests']==before['requests']+[('GET',intent,200)]
        state=admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()[0]
        assert state=='observed_fulfilled'  # future append-only SQL: head0040 lacks this state
        # New record_observation must persist READ evidence atomically but not
        # create a succeeded terminal Action claim under revoked EXECUTE.
        claim=admin.execute("select claim->>'state',claim->'terminal_outcome' from runtime.nexloop_action_claims where action_name=%s and intent_id=%s",
            ('nexloop.service.request',intent)).fetchone()
        assert claim is not None and claim[0]!='terminal'
        assert claim[1] is None


def test_fulfilled_query_finalizes_actual_claim_and_receipt_atomically(governed_effect_executor,admin,tmp_path):
    fixture=governed_effect_executor;receipt=accepted(fixture);intent=receipt['intent_id']
    with effect_provider(tmp_path/'provider-complete.sqlite') as provider:
        provider.control('pause')
        with independent_worker(fixture,provider.origin) as (old,pipe):
            assert provider.wait('accepted_committed')['intent_id']==intent
            old.kill();old.join(10);assert old.exitcode==-signal.SIGKILL
        provider.control('release');provider.control('fulfill',intent_id=intent)
        time.sleep(3.1)  # initial actual Action/effect lease has expired
        with independent_worker(fixture,provider.origin) as (new,pipe):
            assert pipe.poll(15);result=pipe.recv();new.join(10)
            assert new.exitcode==0 and result['status']=='fulfilled'
            assert result['business_action_success'] is True
        # Success requires actual terminal original stable Action binding,
        # authoritative fulfilled provider evidence and same atomic PG outcome.
        row=admin.execute("select principal_id,claim from runtime.nexloop_action_claims where tenant_id=%s and world='real' and action_name=%s and intent_id=%s",
            (fixture.tenant,'nexloop.service.request',intent)).fetchone()
        assert row[0]==fixture.executor_principal and row[1]['state']=='terminal'
        assert row[1]['binding']['invocation_id']==intent
        assert row[1]['terminal_outcome']['status']=='succeeded'
        assert row[1]['terminal_outcome']['outcome_id']==receipt['receipt_id']
        assert row[1]['claim_revision']>=2  # true current reserve after expiry
        assert admin.execute('select state,governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==('fulfilled',True)
        assert provider.control('snapshot')['requests']==[('POST',intent,202),('GET',intent,200)]


def test_fulfillment_transaction_fault_does_not_leave_terminal_claim(governed_effect_executor,admin,tmp_path):
    fixture=governed_effect_executor;receipt=accepted(fixture);intent=receipt['intent_id']
    with effect_provider(tmp_path/'provider-finalize-fault.sqlite') as provider:
        provider.control('pause')
        with independent_worker(fixture,provider.origin) as (old,pipe):
            assert provider.wait('accepted_committed')['intent_id']==intent
            old.kill();old.join(10);assert old.exitcode==-signal.SIGKILL
        provider.control('release');provider.control('fulfill',intent_id=intent)
        # Failure injection is a disposable technical ledger trigger, not a
        # mock authorization/receipt and not SQL business-object mutation.
        admin.execute("create function public.synthetic_finalization_fault() returns trigger language plpgsql as $$begin if new.state='fulfilled' then raise exception 'synthetic finalization rollback';end if;return new;end$$")
        admin.execute('create trigger synthetic_finalization_fault before update on runtime.nexloop_effect_intents for each row execute function public.synthetic_finalization_fault()')
        before=admin.execute("select claim from runtime.nexloop_action_claims where tenant_id=%s and action_name=%s and intent_id=%s",
            (fixture.tenant,'nexloop.service.request',intent)).fetchone()[0]
        time.sleep(3.1)
        with independent_worker(fixture,provider.origin) as (new,pipe):
            assert pipe.poll(15);result=pipe.recv();new.join(10)
            # 0065 two-step: the fulfilled observation commits on its own; the injected
            # fault rolls back only the independent recovery finalization.
            assert new.exitcode==0 and result['status']=='observed_fulfilled'
            assert result['business_action_success'] is False
        after=admin.execute("select claim from runtime.nexloop_action_claims where tenant_id=%s and action_name=%s and intent_id=%s",
            (fixture.tenant,'nexloop.service.request',intent)).fetchone()[0]
        assert after==before  # reserve and terminal finalize rolled back together
        assert admin.execute('select state,governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==('observed_fulfilled',False)
        assert provider.control('snapshot')['effects']==1


def test_old_effect_fence_cannot_overwrite_new_query_observation(governed_effect_executor,admin,tmp_path):
    from nexloop_eios.effect_execution import EffectExecutionUnavailable
    fixture=governed_effect_executor;receipt=accepted(fixture);intent=receipt['intent_id']
    with effect_provider(tmp_path/'provider-fenced.sqlite') as provider:
        transport=HttpEffectProvider(EffectProviderConfiguration(provider.origin,test_loopback_http=True,timeout=1))
        with fixture.current_executor() as old:
            prior=old.claim_effect(lease_seconds=3)
            frozen=old.prepare_effect_dispatch(intent_id=intent,fence=prior['fence'],provider_profile_digest=transport.profile_digest)
            accepted_receipt=transport.dispatch(intent_id=intent,payload_digest=frozen['provider_payload_digest'],parameters=frozen['parameters'])
            assert accepted_receipt.state=='accepted'
            # Independent old Worker lost its lease after the actual provider
            # effect but before record. A new authenticated executor must query.
            time.sleep(3.1)
            with fixture.current_executor() as current:
                recovered=current.claim_effect(lease_seconds=3)
                assert recovered['stage']=='reconcile' and recovered['fence']>prior['fence']
                authorized=current.authorize_effect_query(intent_id=intent,fence=recovered['fence'],provider_profile_digest=transport.profile_digest)
                queried=transport.query(intent_id=intent,payload_digest=authorized['provider_payload_digest'])
                result=current.record_effect_observation(intent_id=intent,fence=recovered['fence'],
                    provider_profile_digest=transport.profile_digest,
                    provider_payload_digest=queried.payload_digest,provider_state=queried.state,provider_reference=queried.provider_reference)
                assert result['state']=='dispatching' and result['provider_state']=='accepted'
                assert result['business_action_success'] is False and result['governed_claim_finalized'] is False
                before=admin.execute('select attempt_revision,effect_fence,provider_reference,state from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()
                with pytest.raises(EffectExecutionUnavailable):
                    old.record_effect_observation(intent_id=intent,fence=prior['fence'],
                        provider_profile_digest=transport.profile_digest,
                        provider_payload_digest=accepted_receipt.payload_digest,provider_state=accepted_receipt.state,
                        provider_reference=accepted_receipt.provider_reference)
                assert admin.execute('select attempt_revision,effect_fence,provider_reference,state from runtime.nexloop_effect_attempts where intent_id=%s',(intent,)).fetchone()==before
        snapshot=provider.control('snapshot')
        assert snapshot['effects']==1 and snapshot['requests']==[('POST',intent,202),('GET',intent,200)]


def complete_then_lose_ack(fixture,admin,provider):
    receipt=accepted(fixture);intent=receipt['intent_id']
    provider.control('pause')
    with independent_worker(fixture,provider.origin) as (old,pipe):
        assert provider.wait('accepted_committed')['intent_id']==intent
        old.kill();old.join(10);assert old.exitcode==-signal.SIGKILL
    provider.control('release');provider.control('fulfill',intent_id=intent);time.sleep(3.1)
    with independent_worker(fixture,provider.origin,hold_terminal_ack=True) as (worker,pipe):
        assert pipe.poll(15) and pipe.recv()=={'event':'terminal_committed'}
        assert admin.execute('select state,governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==('fulfilled',True)
        assert not pipe.poll(), 'terminal caller ACK escaped the controlled window'
        worker.kill();worker.join(10);assert worker.exitcode==-signal.SIGKILL
    return receipt


def test_terminal_receipt_ack_lost_is_current_query_only_no_provider_io(governed_effect_executor,admin,tmp_path):
    fixture=governed_effect_executor
    with effect_provider(tmp_path/'provider-ack-lost.sqlite') as provider:
        receipt=complete_then_lose_ack(fixture,admin,provider);intent=receipt['intent_id']
        before=provider.control('snapshot')
        lease=admin.execute('select lease_until from runtime.nexloop_effect_outbox where intent_id=%s',(intent,)).fetchone()[0]
        assert lease is None
        with fixture.current_executor() as executor:
            replay=executor.read_effect_receipt(intent_id=intent)
            assert executor.read_effect_receipt(intent_id=intent)==replay
        assert replay=={'intent_id':intent,'receipt_id':receipt['receipt_id'],'state':'fulfilled',
            'provider_state':'fulfilled','governed_claim_finalized':True,'business_action_success':True,'receipt_replay':True}
        assert provider.control('snapshot')['requests']==before['requests']
        assert before['effects']==1


def test_terminal_receipt_requires_current_registered_query_grant(governed_effect_executor,admin,tmp_path):
    from nexloop_eios.effect_execution import EffectExecutionUnavailable
    fixture=governed_effect_executor
    with effect_provider(tmp_path/'provider-terminal-revoke.sqlite') as provider:
        receipt=complete_then_lose_ack(fixture,admin,provider);before=provider.control('snapshot')
        fixture.revoke_executor_query_grant()
        with fixture.current_executor() as executor:
            with pytest.raises(EffectExecutionUnavailable):executor.read_effect_receipt(intent_id=receipt['intent_id'])
        assert provider.control('snapshot')['requests']==before['requests']
        assert admin.execute('select state,governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(receipt['intent_id'],)).fetchone()==('fulfilled',True)


def test_nonterminal_receipt_cannot_bypass_owned_lease(governed_effect_executor,tmp_path):
    from nexloop_eios.effect_execution import EffectExecutionUnavailable
    fixture=governed_effect_executor;receipt=accepted(fixture)
    with effect_provider(tmp_path/'provider-nonterminal.sqlite') as provider:
        with fixture.current_executor() as executor:
            with pytest.raises(EffectExecutionUnavailable):executor.read_effect_receipt(intent_id=receipt['intent_id'])
        snapshot=provider.control('snapshot');assert snapshot['effects']==0 and snapshot['requests']==[]
