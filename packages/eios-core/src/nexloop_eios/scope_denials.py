"""Private governed operational rejection port; never an effect receipt.

The genuine stored Source needs an independent published record Action. Runtime
keeps its existing EFFECT allowlist. Every write is surrounded by the original
opaque activation guard and current signed Source catalog READ on one connection.
"""
from datetime import UTC, datetime, timedelta
import hashlib
import psycopg
from jsonschema import Draft202012Validator
from eios.actions import models as M
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.authorization import _identity
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.effect_contexts import EffectContextRegistrar
from nexloop_eios.effect_intents import EffectIntentConflict
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.runtime_activation import _command
from nexloop_eios.service_offerings import REQUEST_SCOPE_SCHEMA, catalog_envelope_from_hint

RECORD='nexloop.service.scope_denial.record'
PROTOCOL='nexloop-scope-denial-v1'


def record_schema():
    fields={'run_id':{'type':'string','format':'uuid'},
        'parameters_text':{'type':'string','maxLength':65536},'request_scope':REQUEST_SCOPE_SCHEMA}
    return {'type':'object','properties':fields,'required':sorted(fields),'additionalProperties':False}


class ScopeDenialUnavailable(RuntimeError):
    code='scope_denial_unavailable'


class ScopeDenialConflict(EffectIntentConflict):
    code='scope_denial_conflict'
    http_status=409


def record_runtime_scope_denial(port,*,activation_ref,command,parameters,request_scope):
    """Backend-private port; `port` is its authenticated RuntimeActivationPort.

    No caller-supplied identity, scope explanation, or runtime error is persisted.
    PostgreSQL derives the eight-field explanation from current catalog facts.
    """
    try:
        Draft202012Validator(REQUEST_SCOPE_SCHEMA).validate(request_scope)
        if type(parameters) is not dict or set(parameters)!={'message'} or type(parameters['message']) is not str:raise ValueError()
        parameters_text=canonical_payload(parameters)
        if len(parameters_text.encode())>65536:raise ValueError()
        text,digest=_command(command)
        world=port.session.world
        with port.pool.connection() as db,db.transaction():
            verify_application_role(db)
            queue=db.execute('select authz.nexloop_runtime_activation_hint(%s,%s,%s)',
                (port.session.token_digest,world,activation_ref)).fetchone()[0]
            binding=dict(activation_ref=activation_ref,command_text=text,command_digest=digest,input_digest=None,operation='tool')
            resolved=port._execute(db,port._signed(queue,'resolve',**binding))
            source=_identity(db,resolved['_context_source_digest'],world)
            run=_identity(db,resolved['_run_digest'],world)
            if source.run_context is not None or run.run_context is None or run.run_context.run_id!=command['run_id']:raise ValueError()
            def guard_envelope():
                return port._signed(queue,'authorize',**binding,run_proofs=port._run_proofs(run),
                    context_artifact_proof=port._context_read_proof(resolved),
                    context_catalog_envelope=port._context_catalog_envelope(resolved))
            first=port._execute(db,guard_envelope())
            if first.get('authorized') is not True:raise ValueError()
            registrar=EffectContextRegistrar(port.pool,source,port.signer)
            definition,capability,schemas=PostgresActionDefinitionReader(port.pool,source,port.signer).get_with_schemas(RECORD,1)
            if definition.model_dump(mode='json')['input_schema']!=record_schema():raise ValueError()
            payload={'run_id':command['run_id'],'parameters_text':parameters_text,'request_scope':request_scope}
            payload_text=canonical_payload(payload)
            request_digest=M.canonical_request_digest(payload)
            # Slot independent of tool call / fresh timestamp. Different formal
            # parameters on the same Run + scope hit the original claim conflict.
            claim_id='scope-denial:'+M.canonical_request_digest({'run_id':command['run_id'],'request_scope':request_scope})
            now=datetime.now(UTC)
            claim=M.ActionClaimRequest(key=M.ActionReservationKey(tenant_id=source.authentication.tenant_id,
                action_stable_name=RECORD,idempotency_key=claim_id),
                binding=M.ClaimBindingPayload(invocation_id=claim_id,action_reference=definition.reference(),
                    request_digest=request_digest,capability_binding=capability.binding(),adapter_id='nexloop.core',target_system='postgres'),
                requested_at=now,lease_expires_at=now+timedelta(seconds=20))
            reserved=M.ActionClaimResult.model_validate_json(canonical_payload(registrar._claim(db,claim,'reserve')))
            if reserved.disposition is M.ActionClaimDisposition.CONFLICT:raise ScopeDenialConflict('scope_denial_conflict')
            if reserved.disposition not in (M.ActionClaimDisposition.CLAIMED,M.ActionClaimDisposition.REPLAY):raise ValueError()
            def write():
                proof=registrar._proof(source,'eios:action:'+RECORD+':1');proof.pop('_decision_id')
                guard=guard_envelope()
                catalog=catalog_envelope_from_hint(port.pool,port.signer,world,
                    {'_source_digest':source.token_digest,'supply':resolved['_context_catalog'],
                     'consumer_id':resolved['_context_consumer_id']},request_scope=request_scope)
                claims={**proof,'parameters_digest':hashlib.sha256(payload_text.encode()).hexdigest(),
                    'definition':definition.model_dump(mode='json'),'capability':capability.model_dump(mode='json'),
                    'definition_reference':definition.reference().model_dump(mode='json'),
                    'capability_binding':capability.binding().model_dump(mode='json'),'claim_id':claim_id,
                    'worker_digest':port.session.token_digest,'guard':dict(zip(('text','signature','payload'),guard)),
                    'catalog':catalog}
                signed=registrar._sign(PROTOCOL,claims)
                return db.execute('select authz.nexloop_scope_denial_command(%s,%s,%s,%s,%s)',
                    (source.token_digest,world,*signed,payload_text)).fetchone()[0]
            result=write()
            if reserved.claim is not None:
                outcome=M.TerminalOutcomeReference(outcome_id=result['record_id'],outcome_revision=1,
                    status=M.TerminalOutcomeStatus.SUCCEEDED,outcome_digest=M.canonical_request_digest(result),
                    finalized_at=db.execute('select clock_timestamp()').fetchone()[0])
                final=M.ActionClaimFinalizeCommand(key=claim.key,binding=claim.binding,
                    expected_claim_revision=reserved.claim.claim_revision,fencing_token=reserved.claim.fencing_token,outcome=outcome)
                registrar._claim(db,final,'finalize')
            if write()!=result:raise ValueError()
        return result
    except ScopeDenialConflict:raise
    except Exception as error:
        if isinstance(error,psycopg.Error) and error.diag.message_primary=='scope denial conflict':
            raise ScopeDenialConflict('scope_denial_conflict') from None
        raise ScopeDenialUnavailable('scope_denial_unavailable') from None
