"""Actual protected PG provider identity binding, with two durable fault servers."""
import pytest
from nexloop_eios.effect_dispatch import EffectDispatcher
from nexloop_eios.effect_execution import EffectExecutionUnavailable
from nexloop_eios.effect_provider import HttpEffectProvider,EffectProviderConfiguration
from support.effect_provider import effect_provider
from effect_execution_fixture import governed_effect_executor,execution_plan


def transport(server):
    return HttpEffectProvider(EffectProviderConfiguration(server.origin,test_loopback_http=True,timeout=1))


def snapshot(admin,intent):
    return (admin.execute('select state,governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone(),
        admin.execute('select count(*) from runtime.nexloop_effect_observations where intent_id=%s',(intent,)).fetchone(),
        admin.execute('select claim from runtime.nexloop_action_claims where intent_id=%s',(intent,)).fetchone())


def test_restart_wrong_provider_cannot_query_observe_or_finalize_original_attempt(governed_effect_executor,admin,tmp_path):
    fixture=governed_effect_executor
    receipt=fixture.ports[0].submit_effect_intent(parameters={'message':'provider binding'})
    intent=receipt['intent_id']
    with effect_provider(tmp_path/'provider-a.sqlite') as a,effect_provider(tmp_path/'provider-b.sqlite') as b:
        pa,pb=transport(a),transport(b)
        assert pa.profile_digest!=pb.profile_digest
        with fixture.current_executor() as executor:
            job=executor.claim_effect(lease_seconds=30)
            identity=dict(intent_id=intent,fence=job['fence'])
            admitted=executor.prepare_effect_dispatch(**identity,provider_profile_digest=pa.profile_digest)
            accepted=pa.dispatch(intent_id=intent,payload_digest=admitted['provider_payload_digest'],parameters=admitted['parameters'])
            assert accepted.state=='accepted'
            executor.record_effect_unknown(**identity)
        # Independent B really has the same key/digest and a fulfilled response;
        # transport-only seeding is attack evidence, never an authorized B send.
        pb.dispatch(intent_id=intent,payload_digest=admitted['provider_payload_digest'],parameters=admitted['parameters'])
        b.control('fulfill',intent_id=intent)
        forged=pb.query(intent_id=intent,payload_digest=admitted['provider_payload_digest'])
        assert forged.state=='fulfilled' and forged.intent_id==intent
        with fixture.current_executor() as restarted:
            job=restarted.claim_effect(lease_seconds=30);identity=dict(intent_id=intent,fence=job['fence'])
            assert job['stage']=='reconcile'
            before=snapshot(admin,intent);requests=b.control('snapshot')['requests']
            with pytest.raises(EffectExecutionUnavailable):
                restarted.authorize_effect_query(**identity,provider_profile_digest=pb.profile_digest)
            assert b.control('snapshot')['requests']==requests
            with pytest.raises(EffectExecutionUnavailable):
                restarted.record_effect_observation(**identity,provider_profile_digest=pb.profile_digest,
                    provider_payload_digest=forged.payload_digest,provider_state=forged.state,provider_reference=forged.provider_reference)
            assert snapshot(admin,intent)==before
            restarted.record_effect_unknown(**identity)
        a.control('fulfill',intent_id=intent)
        with fixture.current_executor() as original_profile:
            result=EffectDispatcher(original_profile,transport(a),lease_seconds=30).run_once()
            assert result['status']=='fulfilled' and result['business_action_success'] is True
            assert original_profile.read_effect_receipt(intent_id=intent)['business_action_success'] is True
        assert a.control('snapshot')['requests']==[('POST',intent,202),('GET',intent,200)]
        assert admin.execute('select provider_profile_digest from runtime.nexloop_effect_provider_bindings where intent_id=%s',(intent,)).fetchone()==(pa.profile_digest,)


def test_missing_profile_rejects_admission_before_attempt_or_binding_write(governed_effect_executor,admin):
    fixture=governed_effect_executor
    receipt=fixture.ports[0].submit_effect_intent(parameters={'message':'invalid profile'})
    with fixture.current_executor() as executor:
        job=executor.claim_effect(lease_seconds=30)
        for profile in ('',None,'f'*63,'F'*64):
            with pytest.raises(EffectExecutionUnavailable):
                executor.prepare_effect_dispatch(intent_id=receipt['intent_id'],fence=job['fence'],provider_profile_digest=profile)
    assert admin.execute('select count(*) from runtime.nexloop_effect_attempts').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_provider_bindings').fetchone()==(0,)


def test_legacy_attempt_without_profile_is_not_guessed(governed_effect_executor,admin,tmp_path):
    fixture=governed_effect_executor
    receipt=fixture.ports[0].submit_effect_intent(parameters={'message':'legacy provider'})
    intent=receipt['intent_id']
    with effect_provider(tmp_path/'legacy-provider.sqlite') as server:
        provider=transport(server)
        with fixture.current_executor() as executor:
            job=executor.claim_effect(lease_seconds=30);identity=dict(intent_id=intent,fence=job['fence'])
            admitted=executor.prepare_effect_dispatch(**identity,provider_profile_digest=provider.profile_digest)
            provider.dispatch(intent_id=intent,payload_digest=admitted['provider_payload_digest'],parameters=admitted['parameters'])
            executor.record_effect_unknown(**identity)
        # Disposable migration-era technical fixture only: emulate an existing
        # pre044 attempt. This does not grant production workers a delete path.
        admin.execute('alter table runtime.nexloop_effect_provider_bindings disable trigger effect_provider_binding_immutable')
        try:admin.execute('delete from runtime.nexloop_effect_provider_bindings where intent_id=%s',(intent,))
        finally:admin.execute('alter table runtime.nexloop_effect_provider_bindings enable trigger effect_provider_binding_immutable')
        before=server.control('snapshot')['requests']
        with fixture.current_executor() as executor:
            result=EffectDispatcher(executor,transport(server),lease_seconds=30).run_once()
            assert result['status']=='query_unavailable' and result['business_action_success'] is False
        assert server.control('snapshot')['requests']==before
        assert admin.execute('select count(*) from runtime.nexloop_effect_observations where intent_id=%s',(intent,)).fetchone()==(0,)
        assert admin.execute('select governed_claim_finalized from runtime.nexloop_effect_intents where intent_id=%s',(intent,)).fetchone()==(False,)


def test_profile_identity_ignores_credential_rotation_but_pins_origin_and_ca(tmp_path):
    from test_agent_host import files
    import secrets
    first=tmp_path/'first';second=tmp_path/'second';first.mkdir();second.mkdir()
    files(first);files(second)
    credential=tmp_path/'provider-key';credential.write_text(secrets.token_hex(32));credential.chmod(0o600)
    config=EffectProviderConfiguration('https://127.0.0.1:14443',credential_file=str(credential),ca_file=str(first/'host-cert.pem'))
    pinned=HttpEffectProvider(config);digest=pinned.profile_digest
    credential.write_text(secrets.token_hex(32))
    assert HttpEffectProvider(config).profile_digest==digest
    alternate_key=tmp_path/'rotated-key';alternate_key.write_text(secrets.token_hex(32));alternate_key.chmod(0o600)
    assert HttpEffectProvider(EffectProviderConfiguration(config.origin,credential_file=str(alternate_key),ca_file=config.ca_file)).profile_digest==digest
    assert HttpEffectProvider(EffectProviderConfiguration('https://127.0.0.1:14444',credential_file=str(credential),ca_file=config.ca_file)).profile_digest!=digest
    original_ca=first/'host-cert.pem';original_ca.write_bytes((second/'host-cert.pem').read_bytes())
    assert pinned.profile_digest==digest
    assert HttpEffectProvider(config).profile_digest!=digest


def test_provider_binding_table_force_rls_and_no_application_read(governed_effect_executor,admin):
    import psycopg
    from psycopg.conninfo import make_conninfo
    fixture=governed_effect_executor
    assert admin.execute("select relrowsecurity,relforcerowsecurity from pg_class where oid='runtime.nexloop_effect_provider_bindings'::regclass").fetchone()==(True,True)
    for role in ('nexloop_api','nexloop_scheduler','nexloop_domain_worker','nexloop_action_worker'):
        with psycopg.connect(make_conninfo(fixture.pg,user=role)) as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute('select * from runtime.nexloop_effect_provider_bindings')
            connection.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute('select authz.nexloop_effect_execution_command_v0042(NULL,NULL,NULL,NULL,NULL)')
            connection.rollback()


def test_binding_insert_wait_cannot_commit_after_effect_lease_expiry(governed_effect_executor,admin):
    fixture=governed_effect_executor
    receipt=fixture.ports[0].submit_effect_intent(parameters={'message':'late profile bind'})
    admin.execute('create function public.synthetic_profile_delay() returns trigger language plpgsql as $$begin perform pg_sleep(4);return new;end$$')
    admin.execute('grant execute on function public.synthetic_profile_delay() to nexloop_owner')
    admin.execute('create trigger synthetic_profile_delay after insert on runtime.nexloop_effect_provider_bindings for each row execute function public.synthetic_profile_delay()')
    with fixture.current_executor() as executor:
        job=executor.claim_effect(lease_seconds=3)
        with pytest.raises(EffectExecutionUnavailable):
            executor.prepare_effect_dispatch(intent_id=receipt['intent_id'],fence=job['fence'],provider_profile_digest='a'*64)
    assert admin.execute('select count(*) from runtime.nexloop_effect_attempts').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_effect_provider_bindings').fetchone()==(0,)
    assert admin.execute('select state from runtime.nexloop_effect_intents where intent_id=%s',(receipt['intent_id'],)).fetchone()==('accepted',)
