"""Published EIOS Action contract reader with live signed service authority."""
from datetime import UTC,datetime,timedelta
import hmac
from eios.authz import facts as F
from eios.authz.resources import ResourceType,resource_id
from eios.authz.operations import Operation
from eios.authz.service import AuthorizationDecisionService
from eios.ontology.definitions import ActionDefinition,DefinitionStatus
from eios.ontology.models import ObjectTypeDefinition,RelationTypeDefinition
from eios.ontology.semantics import schema_contract_digest
from eios.ontology.version_resolution import CapabilityContractSnapshot,validate_capability_binding
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied


class PostgresActionDefinitionReader:
    def __init__(self,pool,session,signer):
        self.pool,self.session,self.signer=pool,session,signer
        with pool.connection() as c:verify_application_role(c)

    def get(self,stable_name,version):
        return self.get_with_schemas(stable_name,version)[:2]

    def get_with_schemas(self,stable_name,version,*,relation=False):
        target=resource_id(ResourceType.ACTION,stable_name,version);entries=[]
        query=self.session.query(resource_id=target,resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
        context=F.AuthorizationFactsResolver(PostgresAuthorityProvider(self.pool,self.session,entries)).resolve(query)
        decision=AuthorizationDecisionService().decide_resolved(context)
        if not decision.allowed or not decision.authoritative or decision.obligations:
            raise ActionAuthorizationDenied('Action definition access denied')
        claims={'protocol':'nexloop-action-definition-v1','key_id':self.signer.key_id,
          'tenant_id':self.session.authentication.tenant_id,'principal_id':self.session.authentication.subject_principal_id,
          'credential_id':self.session.authentication.credential_id,'world':self.session.world,
          'directory_hash':self.session.directory_hash,'resource_id':target,'action_resource':target,'operation':'execute',
          'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
          'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))}
        text=canonical_payload(claims)
        signature=hmac.new(self.signer.material,('nexloop-action-definition-v1:'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as c,c.transaction():
            verify_application_role(c)
            row=c.execute('select authz.nexloop_read_action_bundle(%s,%s,%s,%s)',
                (self.session.token_digest,self.session.world,text,signature)).fetchone()[0]
        try:
            definition=ActionDefinition.model_validate_json(canonical_payload(row['definition']))
            capability=CapabilityContractSnapshot.model_validate_json(canonical_payload(row['capability']))
            if (definition.tenant_id!=self.session.authentication.tenant_id or definition.stable_name!=stable_name
                    or definition.version!=version or definition.status is not DefinitionStatus.PUBLISHED):raise ValueError()
            validate_capability_binding(definition,capability)
            expected=set(definition.object_types)|set(definition.governance.change_scope.object_types)
            actual=set();resolved_schemas=[]
            for item in row['schemas']:
                from eios.ontology.definitions import OntologySchemaReference
                ref=OntologySchemaReference.model_validate_json(canonical_payload(item['reference']))
                schema=ObjectTypeDefinition.model_validate_json(canonical_payload(item['definition']))
                if (schema.type_name!=ref.stable_name or schema.version!=ref.version
                        or schema_contract_digest(schema)!=ref.schema_digest):raise ValueError()
                actual.add(ref);resolved_schemas.append(schema)
            if actual!=expected:raise ValueError()
        except Exception:
            raise ActionAuthorizationDenied('published Action contract is invalid') from None
        if relation:
            try:
                binding=row['relation_binding'];relation_schema=RelationTypeDefinition.model_validate_json(canonical_payload(row['relation_schema']))
                if (binding['action_contract_digest']!=definition.reference().contract_digest
                        or binding['relation_name']!=relation_schema.relation_name
                        or binding['relation_version']!=relation_schema.version
                        or binding['schema_digest']!=schema_contract_digest(relation_schema)
                        or not {relation_schema.source_type,relation_schema.target_type} <= {r.stable_name for r in definition.governance.change_scope.object_types}):
                    raise ValueError()
            except Exception:
                raise ActionAuthorizationDenied('published Relation binding unavailable') from None
            return definition,capability,tuple(resolved_schemas),relation_schema,binding
        return definition,capability,tuple(resolved_schemas)
