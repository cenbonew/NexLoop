"""Governed create adapter; restricted SQL function is the only business writer."""
from datetime import UTC,datetime,timedelta
import hmac
import re
from nexloop_eios.object_reads import AuthorizedObjectReader
from eios.actions import models as M
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType,resource_id
from eios.authz.service import AuthorizationDecisionService
from eios.ontology.models import validate_object_properties
from nexloop_eios.action_definitions import PostgresActionDefinitionReader
from nexloop_eios.action_governor import govern_published_action
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.postgres_artifacts import canonical_payload


class GovernedAssessmentCreator:
    def __init__(self,pool,session,signer):self.pool,self.session,self.signer=pool,session,signer

    def create(self,*,action_name,action_version,intent_id,properties):
        type_name="RelationshipAssessment"
        validate_assessment(properties,creating=True)
        definition,capability,schemas=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get_with_schemas(action_name,action_version)
        schema=next((s for s in schemas if s.type_name==type_name),None)
        if schema is None:raise ActionAuthorizationDenied('type is outside the Action schema bundle')
        values=validate_object_properties(schema,properties)
        if capability.capability_name != 'ontology.relationship_assessment.create': raise ActionAuthorizationDenied('assessment capability required')
        provenance=assessment_authorities(self,values)
        payload={'request_id':intent_id,'type_name':type_name,'properties':values}
        now=datetime.now(UTC);ref=definition.reference()
        command=M.ActionClaimRequest(key=M.ActionReservationKey(tenant_id=self.session.authentication.tenant_id,action_stable_name=action_name,idempotency_key=intent_id),
          binding=M.ClaimBindingPayload(invocation_id=intent_id,action_reference=ref,request_digest=M.canonical_request_digest(payload),
            capability_binding=capability.binding(),adapter_id='nexloop.core',target_system='postgres'),requested_at=now,lease_expires_at=now+timedelta(seconds=20))
        permit=govern_published_action(self.pool,self.session,self.signer,claim_request=command,request=payload)
        if type(permit) is M.TerminalOutcomeReference:
            if permit.status is not M.TerminalOutcomeStatus.SUCCEEDED:raise ActionAuthorizationDenied('Action has a non-success terminal outcome')
            return {'object_id':permit.outcome_id,'type_name':type_name,'world':self.session.world}
        target=resource_id(ResourceType.ACTION,action_name,action_version);entries=[]
        query=self.session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        decision=AuthorizationDecisionService().decide_resolved(F.AuthorizationFactsResolver(PostgresAuthorityProvider(self.pool,self.session,entries)).resolve(query))
        if not decision.allowed or not decision.authoritative or decision.obligations:raise ActionAuthorizationDenied('instance authorization denied')
        claims={'protocol':'nexloop-assessment-create-v1','key_id':self.signer.key_id,'tenant_id':self.session.authentication.tenant_id,
          'principal_id':self.session.authentication.subject_principal_id,'credential_id':self.session.authentication.credential_id,
          'directory_hash':self.session.directory_hash,'world':self.session.world,'resource_id':target,'action_resource':target,'operation':'execute',
          'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
          'facts':sorted(entries,key=lambda row:(row['kind'],row['key'])),
          'permit':permit.model_dump(mode='json'),'definition':definition.model_dump(mode='json'),'schema':schema.model_dump(mode='json')}
        claims['assessment_authorities']=provenance
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-assessment-create-v1:'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as c,c.transaction():
            verify_application_role(c)
            return c.execute('select authz.nexloop_create_assessment_action(%s,%s,%s,%s,%s)',
              (self.session.token_digest,self.session.world,text,signature,canonical_payload(payload))).fetchone()[0]

FIELDS = ('relation_name', 'relation_version', 'source_type', 'source_id', 'target_type', 'target_id', 'conclusion', 'epistemic_kind', 'resolution_state', 'valid_from', 'valid_to', 'evidence_message_id', 'evidence_content_hash', 'corrects_revision')

def validate_assessment(values, *, creating):
    if type(values) is not dict or set(values) != set(FIELDS):
        raise ValueError('complete typed assessment required')
    if any(type(values[f]) is not str for f in FIELDS if f not in ('corrects_revision','relation_version')):
        raise ValueError('assessment values must be typed')
    if type(values['corrects_revision']) is not int or values['corrects_revision'] != (0 if creating else values['corrects_revision']) or values['corrects_revision'] < (0 if creating else 1):
        raise ValueError('invalid correction chain')
    if type(values['relation_version']) is not int or values['relation_version']<0:
        raise ValueError('invalid relation definition version')
    if values['relation_version']==0 and values['epistemic_kind']!='hypothesis' and values['resolution_state']!='awaiting_definition':
        raise ValueError('unknown relation definition cannot be resolved')
    if values['epistemic_kind'] not in ('hypothesis','user_statement') or values['resolution_state'] not in ('resolved','awaiting_definition','unresolved'):
        raise ValueError('unsupported epistemic classification')
    if not values['conclusion'] or len(values['conclusion']) > 8192 or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', values['relation_name']):
        raise ValueError('invalid assessment conclusion/relation')
    for prefix in ('source','target'):
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',values[prefix+'_type']) or not re.fullmatch(r'[a-f0-9]{64}',values[prefix+'_id']):
            raise ValueError('invalid strong endpoint reference')
    for field in ('valid_from','valid_to'):
        if field == 'valid_to' and not values[field]: continue
        stamp=datetime.fromisoformat(values[field].replace('Z','+00:00'))
        if stamp.tzinfo is None: raise ValueError('timezone required')
    if values['valid_to'] and datetime.fromisoformat(values['valid_to'].replace('Z','+00:00')) <= datetime.fromisoformat(values['valid_from'].replace('Z','+00:00')):
        raise ValueError('invalid validity interval')
    if values['epistemic_kind']=='user_statement':
        if not re.fullmatch(r'[a-f0-9]{64}',values['evidence_message_id']) or not re.fullmatch(r'[a-f0-9]{64}',values['evidence_content_hash']):
            raise ValueError('explicit Message provenance required')
    elif values['evidence_message_id'] or values['evidence_content_hash']:
        raise ValueError('hypothesis cannot assert human evidence')
    return values

def assessment_authorities(port, values):
    authority=AuthorizedObjectReader(port.pool,port.session,port.signer)
    proofs=[]
    if values['relation_version']>0:
        proofs.append(authority._authority(ResourceType.LINK_TYPE,values['relation_name']))
    for prefix in ('source','target'):
        name=values[prefix+'_type']+'/'+values[prefix+'_id']
        proofs.extend([authority._authority(ResourceType.OBJECT,name,op) for op in (Operation.READ,Operation.EDIT)])
    if values['epistemic_kind']=='user_statement':
        name='Message/'+values['evidence_message_id']
        proofs.append(authority._authority(ResourceType.OBJECT,name))
        proofs.extend(authority._authority(ResourceType.PROPERTY,name+'/'+field) for field in ('actor','body'))
    return proofs

class GovernedAssessmentCorrector:
    def __init__(self,pool,session,signer):self.pool,self.session,self.signer=pool,session,signer

    def correct(self,*,action_name,action_version,intent_id,object_id,expected_revision,properties):
        type_name="RelationshipAssessment"
        validate_assessment(properties,creating=False)
        if properties["corrects_revision"] != expected_revision: raise ValueError("correction must name current revision")
        if type(expected_revision) is not int or expected_revision<1 or not re.fullmatch(r"[a-f0-9]{64}",object_id):
            raise ValueError("invalid object edit target/revision")
        if not properties:
            raise ValueError("empty object edit")
        definition,capability,schemas=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get_with_schemas(action_name,action_version)
        schema=next((s for s in schemas if s.type_name==type_name),None)
        if schema is None:raise ActionAuthorizationDenied('type is outside the Action schema bundle')
        if capability.capability_name != 'ontology.relationship_assessment.correct':
            raise ActionAuthorizationDenied('Action capability does not support object edit')
        values=validate_object_properties(schema,properties,partial=True)
        if len(values)>64:
            raise ValueError('too many edited properties')
        authority=AuthorizedObjectReader(self.pool,self.session,self.signer)
        object_authority=authority._authority(ResourceType.OBJECT,f'{type_name}/{object_id}',Operation.EDIT)
        property_authorities=[authority._authority(ResourceType.PROPERTY,f'{type_name}/{object_id}/{field}',Operation.EDIT) for field in sorted(values)]
        provenance=assessment_authorities(self,values)
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
        claims={'protocol':'nexloop-assessment-correct-v1','key_id':self.signer.key_id,'tenant_id':self.session.authentication.tenant_id,
          'principal_id':self.session.authentication.subject_principal_id,'credential_id':self.session.authentication.credential_id,
          'directory_hash':self.session.directory_hash,'world':self.session.world,'resource_id':target,'action_resource':target,'operation':'execute',
          'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
          'facts':sorted(entries,key=lambda row:(row['kind'],row['key'])),
          'permit':permit.model_dump(mode='json'),'definition':definition.model_dump(mode='json'),'schema':schema.model_dump(mode='json')}
        claims['object_authority']=object_authority
        claims['property_authorities']=property_authorities
        claims['assessment_authorities']=provenance
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-assessment-correct-v1:'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as c,c.transaction():
            verify_application_role(c)
            return c.execute('select authz.nexloop_correct_assessment_action(%s,%s,%s,%s,%s)',
              (self.session.token_digest,self.session.world,text,signature,canonical_payload(payload))).fetchone()[0]

class AuthorizedAssessmentHeadReader(AuthorizedObjectReader):
    def get(self,type_name,object_id,*,fields=()):
        if (not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',type_name) or not re.fullmatch(r'[a-f0-9]{64}',object_id)
                or len(fields)>64 or any(type(f) is not str or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',f) for f in fields)):
            raise ValueError('invalid object projection')
        fields=tuple(sorted(set(fields)))
        claims=self._authority(ResourceType.OBJECT,f'{type_name}/{object_id}')
        if type_name!='RelationshipAssessment':raise ValueError('assessment head only')
        relation=super().get(type_name,object_id,fields=('relation_name','relation_version'))
        version=relation['properties']['relation_version']
        claims['relation_authority']=self._authority(ResourceType.LINK_TYPE,relation['properties']['relation_name']) if version>0 else None
        claims.update(protocol='nexloop-object-read-v1',key_id=self.signer.key_id,type_name=type_name,object_id=object_id,
            fields=fields,property_authorities=[self._authority(ResourceType.PROPERTY,f'{type_name}/{object_id}/{f}') for f in fields])
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-object-read-v1:'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as c,c.transaction():
            verify_application_role(c)
            return c.execute('select authz.nexloop_read_assessment_object(%s,%s,%s,%s)',(self.session.token_digest,self.session.world,text,signature)).fetchone()[0]


class AuthorizedAssessmentProjection:
    """Typed current assessment; formal/evidence separation, no topology assertion.

    This port is not yet wired into Context construction. User statements remain
    labeled statements; neither hypotheses nor pending definitions are formal.
    """
    def __init__(self,pool,session,signer):self.pool,self.session,self.signer=pool,session,signer
    def current(self,object_id,*,valid_at=None):
        at=valid_at or datetime.now(UTC)
        if at.tzinfo is None:raise ValueError('timezone required')
        # Keep existing protected reader row locks for the entire composed projection.
        # No direct business SELECT or caller-controlled GUC is introduced.
        from contextlib import contextmanager
        with self.pool.connection() as connection,connection.transaction():
            class BoundPool:
                @contextmanager
                def connection(self):yield connection
            authority=AuthorizedObjectReader(self.pool,self.session,self.signer)
            class BoundReader(AuthorizedObjectReader):
                def _authority(self,kind,name,operation=Operation.READ):
                    return authority._authority(kind,name,operation)
            reader=BoundReader(BoundPool(),self.session,self.signer)
            class HeadReader(AuthorizedAssessmentHeadReader):
                def _authority(self,kind,name,operation=Operation.READ):
                    return authority._authority(kind,name,operation)
            head_reader=HeadReader(BoundPool(),self.session,self.signer)
            return self._current(reader,head_reader,object_id,at)

    def _current(self,reader,head_reader,object_id,at):
        head=head_reader.get('RelationshipAssessment',object_id,fields=FIELDS)
        if head is None:return {'formal':[],'evidence':[]}
        values=head['properties'];validate_assessment(values,creating=values['corrects_revision']==0)
        for prefix in ('source','target'):
            if reader.get(values[prefix+'_type'],values[prefix+'_id']) is None:raise ActionAuthorizationDenied('assessment endpoint unavailable')
        if values['epistemic_kind']=='user_statement':
            source=reader.get('Message',values['evidence_message_id'],fields=('actor','body'))
            import hashlib
            if source is None or hashlib.sha256(source['properties']['body'].encode()).hexdigest()!=values['evidence_content_hash']:
                raise ActionAuthorizationDenied('assessment source unavailable')
        begins=datetime.fromisoformat(values['valid_from'].replace('Z','+00:00'))
        ends=datetime.fromisoformat(values['valid_to'].replace('Z','+00:00')) if values['valid_to'] else None
        formal=(values['relation_version']>0 and values['epistemic_kind']=='user_statement' and values['resolution_state']=='resolved' and begins<=at and (ends is None or at<ends))
        item={'assessment_id':object_id,'revision':head['revision'],'epistemic_kind':values['epistemic_kind'],'resolution_state':values['resolution_state'],'properties':values}
        return {'formal':[item] if formal else [],'evidence':[] if formal else [item]}
