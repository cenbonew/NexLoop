"""Governed property patch adapter; restricted SQL function is the only business writer."""
from datetime import UTC,datetime,timedelta
import hmac
import re
from eios.actions import models as M
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType,resource_id
from eios.authz.service import AuthorizationDecisionService
from eios.ontology.models import validate_object_properties
from nexloop_eios.object_reads import AuthorizedObjectReader
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.action_governor import govern_published_action
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.postgres_artifacts import canonical_payload


class GovernedObjectEditor:
    def __init__(self,pool,session,signer):self.pool,self.session,self.signer=pool,session,signer

    def edit(self,*,action_name,action_version,intent_id,type_name,object_id,expected_revision,properties):
        if type(expected_revision) is not int or expected_revision<1 or not re.fullmatch(r"[a-f0-9]{64}",object_id):
            raise ValueError("invalid object edit target/revision")
        if not properties:
            raise ValueError("empty object edit")
        definition,capability,schemas=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get_with_schemas(action_name,action_version)
        schema=next((s for s in schemas if s.type_name==type_name),None)
        if schema is None:raise ActionAuthorizationDenied('type is outside the Action schema bundle')
        if capability.capability_name not in ('consumer.edit','ontology.object.edit'):
            raise ActionAuthorizationDenied('Action capability does not support object edit')
        values=validate_object_properties(schema,properties,partial=True)
        if len(values)>64:
            raise ValueError('too many edited properties')
        authority=AuthorizedObjectReader(self.pool,self.session,self.signer)
        object_authority=authority._authority(ResourceType.OBJECT,f'{type_name}/{object_id}',Operation.EDIT)
        property_authorities=[authority._authority(ResourceType.PROPERTY,f'{type_name}/{object_id}/{field}',Operation.EDIT) for field in sorted(values)]
        payload={'request_id':intent_id,'type_name':type_name,'properties':values,'object_id':object_id,'expected_revision':expected_revision}
        now=datetime.now(UTC);ref=definition.reference()
        command=M.ActionClaimRequest(key=M.ActionReservationKey(tenant_id=self.session.authentication.tenant_id,action_stable_name=action_name,idempotency_key=intent_id),
          binding=M.ClaimBindingPayload(invocation_id=intent_id,action_reference=ref,request_digest=M.canonical_request_digest(payload),
            capability_binding=capability.binding(),adapter_id='nexloop.core',target_system='postgres'),requested_at=now,lease_expires_at=now+timedelta(seconds=20))
        permit=govern_published_action(self.pool,self.session,self.signer,claim_request=command,request=payload)
        if type(permit) is M.TerminalOutcomeReference:
            if permit.status is not M.TerminalOutcomeStatus.SUCCEEDED:raise ActionAuthorizationDenied('Action has a non-success terminal outcome')
            return {'object_id':permit.outcome_id,'type_name':type_name,'world':self.session.world,'revision':expected_revision+1}
        target=resource_id(ResourceType.ACTION,action_name,action_version);entries=[]
        query=self.session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        decision=AuthorizationDecisionService().decide_resolved(F.AuthorizationFactsResolver(PostgresAuthorityProvider(self.pool,self.session,entries)).resolve(query))
        if not decision.allowed or not decision.authoritative or decision.obligations:raise ActionAuthorizationDenied('instance authorization denied')
        claims={'protocol':'nexloop-object-edit-v1','key_id':self.signer.key_id,'tenant_id':self.session.authentication.tenant_id,
          'principal_id':self.session.authentication.subject_principal_id,'credential_id':self.session.authentication.credential_id,
          'directory_hash':self.session.directory_hash,'world':self.session.world,'resource_id':target,'action_resource':target,'operation':'execute',
          'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
          'facts':sorted(entries,key=lambda row:(row['kind'],row['key'])),
          'permit':permit.model_dump(mode='json'),'definition':definition.model_dump(mode='json'),'schema':schema.model_dump(mode='json')}
        claims['object_authority']=object_authority
        claims['property_authorities']=property_authorities
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-object-edit-v1:'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as c,c.transaction():
            verify_application_role(c)
            return c.execute('select authz.nexloop_edit_object_action(%s,%s,%s,%s,%s)',
              (self.session.token_digest,self.session.world,text,signature,canonical_payload(payload))).fetchone()[0]
