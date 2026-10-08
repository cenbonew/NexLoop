"""Protected as-of assessment evidence; no raw audit discovery/list interface."""
from datetime import datetime
import hmac
import re
from eios.authz.resources import ResourceType
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.object_reads import AuthorizedObjectReader
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.assessment_actions import FIELDS

class AuthorizedAssessmentHistory:
    """Explicit as-of evidence read; caller names Message source, SQL verifies it.

    No raw audit-list endpoint or provenance discovery that could expose hidden
    historical source text is provided. Unknown source requires an authorized
    source reference from the caller; mismatch fails closed in protected SQL.
    """
    def __init__(self,pool,session,signer):self.pool,self.session,self.signer=pool,session,signer
    def as_of(self,object_id,*,known_at,valid_at,evidence_message_id=None):
        for value in (known_at,valid_at):
            if not isinstance(value,datetime) or value.tzinfo is None:raise ValueError('timezone required')
        reader=AuthorizedObjectReader(self.pool,self.session,self.signer)
        metadata=reader.get('RelationshipAssessment',object_id,fields=('relation_name','relation_version','source_type','source_id','target_type','target_id'))['properties']
        proofs=[]
        if metadata['relation_version']>0:proofs.append(reader._authority(ResourceType.LINK_TYPE,metadata['relation_name']))
        for prefix in ('source','target'):proofs.append(reader._authority(ResourceType.OBJECT,metadata[prefix+'_type']+'/'+metadata[prefix+'_id']))
        if evidence_message_id is not None:
            if type(evidence_message_id) is not str or not re.fullmatch(r'[a-f0-9]{64}',evidence_message_id):raise ValueError('invalid source Message')
            name='Message/'+evidence_message_id
            proofs.append(reader._authority(ResourceType.OBJECT,name))
            proofs.extend(reader._authority(ResourceType.PROPERTY,name+'/'+f) for f in ('actor','body'))
        claims=reader._authority(ResourceType.OBJECT,'RelationshipAssessment/'+object_id)
        claims.update(protocol='nexloop-assessment-history-v1',key_id=self.signer.key_id,object_id=object_id,known_at=known_at.isoformat(),valid_at=valid_at.isoformat(),
          assessment_authorities=proofs,property_authorities=[reader._authority(ResourceType.PROPERTY,'RelationshipAssessment/'+object_id+'/'+f) for f in FIELDS])
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-assessment-history-v1:'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as c,c.transaction():
            verify_application_role(c)
            return c.execute('select authz.nexloop_read_assessment_history(%s,%s,%s,%s)',(self.session.token_digest,self.session.world,text,signature)).fetchone()[0]
