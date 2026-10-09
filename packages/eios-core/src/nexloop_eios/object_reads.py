"""Service object read projection: each requested property needs real EIOS READ."""
from datetime import UTC,datetime,timedelta
import hmac
import re
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType,resource_id
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.authorization import PostgresAuthorityProvider,resolve_authorities,resolve_authority
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.postgres_artifacts import canonical_payload

class AuthorizedObjectReader:
    def __init__(self,pool,session,signer):self.pool,self.session,self.signer=pool,session,signer

    def _authority(self,kind,name,operation=Operation.READ):
        """Configured EIOS proof; a denied service OBJECT/PROPERTY READ/EDIT may fall back to the governed type derivation.

        Message/Conversation READ first asks SQL for the accepted-Message derivation
        (0077/0080/0086); SQL answers "configured" whenever this principal has configured
        grants on the target, so configured authority keeps precedence.
        """
        if operation is Operation.READ and kind in (ResourceType.OBJECT,ResourceType.PROPERTY) and (name.startswith('Message/') or name.startswith('Conversation/')):
            if getattr(self,'_evidence',None) is None:
                from nexloop_eios.message_read import DerivedEvidenceReads
                self._evidence=DerivedEvidenceReads(self)
            derived=self._evidence.claim(resource_id(kind,name))
            if derived is not None:return derived
        try:return self._configured(kind,name,operation)
        except (ActionAuthorizationDenied,F.AuthorizationFactDenied,AuthorizationUnavailable) as denied:
            try:derived=self._derived(kind,name,operation)
            except Exception:derived=None  # fail closed with the configured denial
            if derived is None:raise denied
            return derived

    def _derived(self,kind,name,operation):
        if kind not in (ResourceType.OBJECT,ResourceType.PROPERTY) or operation not in (Operation.READ,Operation.EDIT):return None
        from nexloop_eios.property_access import derived_claims,property_access_basis
        target=resource_id(kind,name)
        basis=property_access_basis(self.pool,self.session,target,operation.value)
        if basis.get('mode')!='derived':return None
        type_read=self._configured(ResourceType.OBJECT_TYPE,basis['type_name'],Operation.READ)
        return derived_claims(self.session,target,operation.value,basis,type_read)

    def _configured(self,kind,name,operation):
        target=resource_id(kind,name);entries=[]
        query=self.session.query(resource_id=target,resource_type=kind,operation=operation)
        context=resolve_authority(self.pool,self.session,query,entries)
        decision=AuthorizationDecisionService().decide_resolved(context)
        if not decision.allowed or not decision.authoritative or decision.obligations:raise ActionAuthorizationDenied('object/property read denied')
        return self._claims(target,operation,decision,entries)

    def _claims(self,target,operation,decision,entries):
        return {'tenant_id':self.session.authentication.tenant_id,'principal_id':self.session.authentication.subject_principal_id,
          'credential_id':self.session.authentication.credential_id,'directory_hash':self.session.directory_hash,
          'world':self.session.world,'resource_id':target,'target_resource':target,'operation':operation.value,
          'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
          'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))}

    def authorities(self,targets,operation=Operation.READ):
        """O2b batch form of _authority for [(ResourceType, name)]: same claims, same order.

        Configured decisions for all targets are resolved together (one transaction,
        one fact round trip, each target still judged on its own). A target that is
        not plainly allowed by configured authority, and every Message/Conversation
        target, goes through the unchanged single _authority path (derivations,
        configured precedence, identical denial), evaluated in the original order.
        """
        targets=list(targets);claims=[None]*len(targets);batch=[]
        for index,(kind,name) in enumerate(targets):
            if not (operation is Operation.READ and kind in (ResourceType.OBJECT,ResourceType.PROPERTY)
                    and (name.startswith('Message/') or name.startswith('Conversation/'))):
                batch.append(index)
        if len(batch)>1:
            queries=[self.session.query(resource_id=resource_id(*targets[index]),resource_type=targets[index][0],operation=operation) for index in batch]
            for index,(context,entries,error) in zip(batch,resolve_authorities(self.pool,self.session,queries)):
                if error is not None:continue
                try:decision=AuthorizationDecisionService().decide_resolved(context)
                except Exception:continue
                if decision.allowed and decision.authoritative and not decision.obligations:
                    claims[index]=self._claims(resource_id(*targets[index]),operation,decision,entries)
        return [claim if claim is not None else self._authority(kind,name,operation) for claim,(kind,name) in zip(claims,targets)]

    def get(self,type_name,object_id,*,fields=()):
        if (not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',type_name) or not re.fullmatch(r'[a-f0-9]{64}',object_id)
                or len(fields)>64 or any(type(f) is not str or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',f) for f in fields)):
            raise ValueError('invalid object projection')
        fields=tuple(sorted(set(fields)))
        claims,*properties=self.authorities([(ResourceType.OBJECT,f'{type_name}/{object_id}')]+[(ResourceType.PROPERTY,f'{type_name}/{object_id}/{f}') for f in fields])
        claims.update(protocol='nexloop-object-read-v1',key_id=self.signer.key_id,type_name=type_name,object_id=object_id,
            fields=fields,property_authorities=properties)
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-object-read-v1:'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as c,c.transaction():
            verify_application_role(c)
            return c.execute('select authz.nexloop_read_object(%s,%s,%s,%s)',(self.session.token_digest,self.session.world,text,signature)).fetchone()[0]
