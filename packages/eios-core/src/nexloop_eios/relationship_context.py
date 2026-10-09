"""Server-selected, current-authorized relationship Context; no mutation port."""
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib,hmac,re
from nexloop_eios.assessment_actions import AuthorizedAssessmentHeadReader,FIELDS
from nexloop_eios.object_reads import AuthorizedObjectReader
from eios.authz.resources import ResourceType
from nexloop_eios.postgres_artifacts import canonical_payload

@dataclass(frozen=True)
class RelationshipContextRecipe:
    assessment_ids: tuple[str,...]
    def __post_init__(self):
        ids=self.assessment_ids
        if type(ids) is not tuple or not 1<=len(ids)<=4 or tuple(sorted(set(ids)))!=ids or any(type(i) is not str or re.fullmatch('[a-f0-9]{64}',i) is None for i in ids):
            raise ValueError('relationship_recipe_invalid')

class RelationshipContextReader:
    def __init__(self,pool,session,signer,recipe):
        if type(recipe) is not RelationshipContextRecipe or session.world!='real' or session.run_context is not None:
            raise ValueError('relationship_context_source_invalid')
        self.pool,self.session,self.signer,self.recipe=pool,session,signer,recipe
    def message_envelope(self,message_id):
        reader=AuthorizedObjectReader(self.pool,self.session,self.signer)
        proof=reader._authority(ResourceType.OBJECT,'Message/'+message_id)
        proof.update(protocol='nexloop-object-read-v1',key_id=self.signer.key_id,type_name='Message',object_id=message_id,fields=['actor','body'],property_authorities=[reader._authority(ResourceType.PROPERTY,'Message/'+message_id+'/'+field) for field in ('actor','body')])
        text=canonical_payload(proof);signature=hmac.new(self.signer.material,('nexloop-object-read-v1:'+text).encode(),'sha256').hexdigest()
        return {'text':text,'signature':signature}
    def envelopes(self):
        reader=AuthorizedObjectReader(self.pool,self.session,self.signer)
        head=AuthorizedAssessmentHeadReader(self.pool,self.session,self.signer)
        envelopes=[]
        for object_id in self.recipe.assessment_ids:
            current=head.get('RelationshipAssessment',object_id,fields=FIELDS)
            if current is None:raise ValueError('relationship_assessment_unavailable')
            values=current['properties']
            claims=reader._authority(ResourceType.OBJECT,'RelationshipAssessment/'+object_id)
            claims.update(protocol='nexloop-object-read-v1',key_id=self.signer.key_id,type_name='RelationshipAssessment',object_id=object_id,fields=sorted(FIELDS),property_authorities=[reader._authority(ResourceType.PROPERTY,'RelationshipAssessment/'+object_id+'/'+field) for field in sorted(FIELDS)],relation_authority=reader._authority(ResourceType.LINK_TYPE,values['relation_name']) if values['relation_version']>0 else None)
            text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-object-read-v1:'+text).encode(),'sha256').hexdigest()
            refs=[(values[prefix+'_type'],values[prefix+'_id'],()) for prefix in ('source','target')]
            if values['epistemic_kind']=='user_statement':refs.append(('Message',values['evidence_message_id'],('actor','body')))
            sources=[]
            for type_name,target,fields in refs:
                proof=reader._authority(ResourceType.OBJECT,type_name+'/'+target)
                proof.update(protocol='nexloop-object-read-v1',key_id=self.signer.key_id,type_name=type_name,object_id=target,fields=sorted(fields),property_authorities=[reader._authority(ResourceType.PROPERTY,type_name+'/'+target+'/'+field) for field in sorted(fields)])
                source_text=canonical_payload(proof);source_signature=hmac.new(self.signer.material,('nexloop-object-read-v1:'+source_text).encode(),'sha256').hexdigest()
                sources.append({'text':source_text,'signature':source_signature})
            envelopes.append({'text':text,'signature':signature,'sources':sources})
        return envelopes
