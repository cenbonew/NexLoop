"""Governed human read of one model-call Manifest (nexloop.context.audit:1, 0094).

Owner decision: only humans (owner / audit role) may read Manifests; the grant lives in
trusted configuration, never in service-grants. SQL refuses services, Agents and Run
credentials before it evaluates any grant, so holding a grant does not help them.
"""
import hashlib,hmac
import uuid

from nexloop_eios.context_engine.authority import ContextDenied,action_claims
from nexloop_eios.postgres_artifacts import canonical_payload

AUDIT_ACTION='nexloop.context.audit'
AUDIT_CAPABILITY='context.audit'
PROTOCOL='nexloop-context-audit-v1'


def context_manifest_object_type():
    """Typed read scope of nexloop.context.audit:1; Manifest rows live in runtime.nexloop_model_requests."""
    from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
    return ObjectTypeDefinition(type_name='ContextManifest',version=1,only_edit_via_actions=True,display_name='Context Manifest',
        description='单次模型调用的 Context 来源、版本与请求摘要（只读审计）',
        properties=tuple(PropertyDefinition(property_name=name,value_type=kind,required=True) for name,kind in
            (('run_id',PropertyValueType.STRING),('call_sequence',PropertyValueType.INTEGER),('request_digest',PropertyValueType.STRING))))


class ContextAuditReader:
    def __init__(self,pool,session,signer):self.pool,self.session,self.signer=pool,session,signer

    def _call(self,claims,run_id,call_sequence):
        from nexloop_eios.action_definitions import PostgresActionDefinitionReader
        from nexloop_eios.assembly import verify_application_role
        body=canonical_payload({'run_id':str(uuid.UUID(str(run_id))),'call_sequence':int(call_sequence)})
        claims={**claims,'protocol':PROTOCOL,'key_id':self.signer.key_id,'parameters_digest':hashlib.sha256(body.encode()).hexdigest()}
        if 'definition' not in claims:
            definition,_=PostgresActionDefinitionReader(self.pool,self.session,self.signer).get(AUDIT_ACTION,1)
            claims['definition']=definition.model_dump(mode='json')
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,(PROTOCOL+':'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as db,db.transaction():
            verify_application_role(db)
            return db.execute('select authz.nexloop_context_manifest_read(%s,%s,%s,%s,%s)',(self.session.token_digest,self.session.world,text,signature,body)).fetchone()[0]

    def manifest(self,run_id,call_sequence):
        """Manifest dict, or None when this tenant has no such call. Raises ContextDenied without the grant."""
        return self._call(action_claims(self.pool,self.session,AUDIT_ACTION),run_id,call_sequence)
