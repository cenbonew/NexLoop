"""Run-bound stable effect intent admission, not provider execution.

Only trusted persisted governed planner context selects the business slot.
Absent context or controls always fail closed.
The existing Action claim principal check remains unchanged; an independently
registered real executor will own that later claim, not the submitting Run.
"""
from datetime import UTC,datetime,timedelta
import hashlib
import hmac
import uuid

import psycopg
from jsonschema import Draft202012Validator
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.service_offerings import CatalogScopeDenied
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_artifacts import canonical_payload


ACTION='nexloop.service.request'

class EffectIntentUnavailable(PermissionError):pass
class EffectIntentConflict(RuntimeError):
    code='intent_payload_conflict'
    http_status=409


class EffectIntentPort:
    def __init__(self,pool,session,signer):
        if session.run_context is None:raise EffectIntentUnavailable('effect intent unavailable')
        self.pool,self.session,self.signer=pool,session,signer

    def _execute_in_transaction(self,db,verb,*,action_version,runtime_refs=None,request_scope=None,**arguments):
        try:
            if type(action_version) is not int or not 1<=action_version<=2147483647:raise ValueError()
            definition,capability=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get(ACTION,action_version)
            # This thin admission supports only explicit low-risk/no-approval
            # published contracts. Unsupported policy/precondition machinery is
            # blocked, never treated as successful governance or a claim permit.
            governance=definition.governance
            if (governance.approval_mode.value!='none' or governance.risk_level.value!='low'
                or governance.policy_refs or definition.preconditions or definition.parameters):raise ValueError()
            needed=set(definition.required_scopes)|set(capability.required_scopes)
            if not needed<=self.session.authentication.requested_scopes:raise ValueError()
            if verb=='submit':
                parameters=arguments['parameters']
                if type(parameters) is not dict:raise ValueError()
                schema=definition.model_dump(mode='json')['input_schema']
                Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(parameters)
                text=canonical_payload(parameters)
                if len(text.encode())>65536:raise ValueError()
                arguments={**arguments,'parameters_text':text,'provider_payload_digest':hashlib.sha256(text.encode()).hexdigest()}
            if runtime_refs is not None:
                if type(runtime_refs) is not dict or set(runtime_refs)!={'consumer_ref','goal_version_ref'}:raise ValueError()
                if any(type(value) is not str or not 1<=len(value)<=512 for value in runtime_refs.values()):raise ValueError()
                arguments={**arguments,'runtime_refs':runtime_refs}
            payload=canonical_payload({'verb':verb,'action_version':action_version,**arguments})
            if len(payload.encode())>131072:raise ValueError()
            entries=[];target=f'eios:action:{ACTION}:{action_version}'
            query=self.session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
            decision=AuthorizationDecisionService().decide_resolved(F.AuthorizationFactsResolver(PostgresAuthorityProvider(self.pool,self.session,entries)).resolve(query))
            if not decision.allowed or not decision.authoritative or decision.obligations:raise ValueError()
            session=self.session
            claims={'protocol':'nexloop-effect-intent-v1','key_id':self.signer.key_id,'tenant_id':session.authentication.tenant_id,
                'principal_id':session.authentication.subject_principal_id,'credential_id':session.authentication.credential_id,
                'directory_hash':session.directory_hash,'world':session.world,'resource_id':target,'action_resource':target,'operation':'execute',
                'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
                'parameters_digest':hashlib.sha256(payload.encode()).hexdigest(),'facts':sorted(entries,key=lambda row:(row['kind'],row['key'])),
                'definition':definition.model_dump(mode='json'),'capability':capability.model_dump(mode='json')}
            text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-effect-intent-v1:'+text).encode(),'sha256').hexdigest()
            verify_application_role(db)
            hint=db.execute('select authz.nexloop_effect_catalog_hint(%s,%s,%s,%s,%s)',
              (session.token_digest,session.world,text,signature,payload)).fetchone()[0]
            from nexloop_eios.service_offerings import catalog_envelope_from_hint,preflight_scope
            from nexloop_eios.role_runs import role_envelope_for_run
            if session.run_context is not None:
                role=role_envelope_for_run(self.pool,self.signer,session.world,session.token_digest)
                if role is not None:claims['role_envelope']=role
            claims['catalog_envelope']=catalog_envelope_from_hint(self.pool,self.signer,session.world,hint,request_scope=request_scope)
            preflight_scope(self.pool,session.world,hint,claims['catalog_envelope'])
            text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-effect-intent-v1:'+text).encode(),'sha256').hexdigest()
            return db.execute('select authz.nexloop_effect_intent_command(%s,%s,%s,%s,%s)',
                (session.token_digest,session.world,text,signature,payload)).fetchone()[0]
        except Exception as error:
            from nexloop_eios.service_offerings import CatalogScopeDenied
            if isinstance(error,CatalogScopeDenied):raise
            if isinstance(error,psycopg.Error) and error.diag.message_primary in ('effect_payload_conflict','effect catalog conflict'):
                raise EffectIntentConflict('intent_payload_conflict') from None
            raise EffectIntentUnavailable('effect intent unavailable') from None

    def _call(self,verb,*,action_version,**arguments):
        # Commit before returning; the private Runtime bridge may use this same
        # signed command inside its own authority-checked transaction instead.
        try:
            with self.pool.connection() as db,db.transaction():
                return self._execute_in_transaction(db,verb,action_version=action_version,**arguments)
        except (EffectIntentConflict,EffectIntentUnavailable):raise
        except CatalogScopeDenied:raise
        except Exception:raise EffectIntentUnavailable('effect intent unavailable') from None

    def submit(self,*,parameters,action_version=1,request_scope=None):
        return self._call('submit',action_version=action_version,parameters=parameters,request_scope=request_scope)

    def find(self,*,intent_id,action_version=1):
        try:identifier=str(uuid.UUID(intent_id))
        except Exception:raise EffectIntentUnavailable('effect intent unavailable') from None
        return self._call('find',action_version=action_version,intent_id=identifier)
