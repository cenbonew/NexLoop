"""Published read-only Function + current Human ownership gate; no external calls.

Invoking a Function uses FUNCTION EXECUTE permission, independently of the
Conversation read Action gate. It never executes/finalizes service.request.
"""
from datetime import UTC,datetime,timedelta
import hashlib
import hmac
import re

import psycopg
from jsonschema import Draft202012Validator
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from eios.ontology.definitions import FunctionDefinition,DefinitionStatus
from eios.ontology.models import ObjectTypeDefinition
from eios.ontology.semantics import schema_contract_digest
from eios.ontology.version_resolution import CapabilityContractSnapshot,validate_capability_binding
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.browser_authorization import BrowserBusinessSession
from nexloop_eios.conversation_messages import ConversationDenied,ConversationUnavailable,READ
from nexloop_eios.postgres_artifacts import canonical_payload

FUNCTION='nexloop.conversation.service_receipt'
QUERY_CAPABILITY='nexloop.conversation.receipts.read'
PROTOCOL='nexloop-conversation-effect-query-v1'


class ConversationEffectReceiptPort:
    def __init__(self,pool,session,signer):
        if type(session) is not BrowserBusinessSession or session.world!='real':raise ConversationDenied()
        self.pool,self.session,self.signer=pool,session,signer

    def _proof(self,target,kind,payload):
        entries=[]
        decision=AuthorizationDecisionService().decide_resolved(F.AuthorizationFactsResolver(
            PostgresAuthorityProvider(self.pool,self.session,entries)).resolve(self.session.query(
                resource_id=target,resource_type=kind,operation=Operation.EXECUTE)))
        if not decision.allowed or not decision.authoritative or decision.obligations:raise ConversationDenied()
        auth=self.session.authentication
        return {'protocol':PROTOCOL,'key_id':self.signer.key_id,'tenant_id':auth.tenant_id,
            'principal_id':auth.subject_principal_id,'credential_id':auth.credential_id,
            'directory_hash':self.session.directory_hash,'world':self.session.world,
            'resource_id':target,'action_resource':target,'operation':'execute',
            'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
            'parameters_digest':hashlib.sha256(payload.encode()).hexdigest(),
            'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))}

    def _envelope(self,claims):
        text=canonical_payload(claims)
        signature=hmac.new(self.signer.material,(PROTOCOL+':'+text).encode(),'sha256').hexdigest()
        return text,signature

    def _bundle(self):
        payload=canonical_payload({'verb':'bundle'})
        claims=self._proof('eios:function:'+FUNCTION+':1',ResourceType.FUNCTION,payload)
        text,signature=self._envelope(claims)
        with self.pool.connection() as db,db.transaction():
            if verify_application_role(db)!='nexloop_api':raise ConversationDenied()
            row=db.execute('select authz.nexloop_conversation_effect_receipt_query(%s,%s,%s,%s,%s)',
                (self.session.token_digest,self.session.world,text,signature,payload)).fetchone()[0]
        definition=FunctionDefinition.model_validate_json(canonical_payload(row['definition']))
        capability=CapabilityContractSnapshot.model_validate_json(canonical_payload(row['capability']))
        if (definition.tenant_id!=self.session.authentication.tenant_id or definition.stable_name!=FUNCTION
            or definition.version!=1 or definition.status is not DefinitionStatus.PUBLISHED):raise ConversationDenied()
        validate_capability_binding(definition,capability)
        if capability.capability_name!=QUERY_CAPABILITY or capability.has_side_effects is not False:raise ConversationDenied()
        if not (set(definition.required_scopes)|set(capability.required_scopes))<=self.session.authentication.requested_scopes:raise ConversationDenied()
        if definition.property_dependencies or definition.link_dependencies or definition.required_markings:raise ConversationDenied()
        expected={item.object_type for item in definition.applies_to}
        if None in expected or {item.stable_name for item in expected}!={'Consumer','Conversation','Message'}:raise ConversationDenied()
        actual=set()
        from eios.ontology.definitions import OntologySchemaReference
        for item in row['schemas']:
            ref=OntologySchemaReference.model_validate_json(canonical_payload(item['reference']))
            schema=ObjectTypeDefinition.model_validate_json(canonical_payload(item['definition']))
            if schema.type_name!=ref.stable_name or schema.version!=ref.version or schema_contract_digest(schema)!=ref.schema_digest:raise ConversationDenied()
            actual.add(ref)
        if actual!=expected:raise ConversationDenied()
        return definition,capability

    def read_message_service_receipt(self,*,message_id):
        try:
            if type(message_id) is not str or re.fullmatch('[0-9a-f]{64}',message_id) is None:raise ConversationDenied()
            definition,capability=self._bundle()
            parameters={'message_id':message_id}
            schema=definition.model_dump(mode='json')['input_schema']
            Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(parameters)
            payload=canonical_payload({'verb':'receipt',**parameters})
            claims=self._proof('eios:function:'+FUNCTION+':1',ResourceType.FUNCTION,payload)
            ownership=self._proof('eios:action:'+READ+':1',ResourceType.ACTION,payload)
            claims.update({'ownership_proof':ownership,'definition':definition.model_dump(mode='json'),
                'capability':capability.model_dump(mode='json')})
            text,signature=self._envelope(claims)
            # Business read-only; current permission locks require a normal short
            # transaction, not PostgreSQL READ ONLY (which forbids FOR SHARE).
            with self.pool.connection() as db,db.transaction():
                if verify_application_role(db)!='nexloop_api':raise ConversationDenied()
                result=db.execute('select authz.nexloop_conversation_effect_receipt_query(%s,%s,%s,%s,%s)',
                    (self.session.token_digest,self.session.world,text,signature,payload)).fetchone()[0]
            Draft202012Validator(definition.model_dump(mode='json')['output_schema']).validate(result)
            return result
        except ConversationDenied:raise
        except psycopg.Error as error:
            if error.sqlstate=='42501':raise ConversationDenied() from None
            raise ConversationUnavailable() from None
        except Exception:raise ConversationUnavailable() from None
