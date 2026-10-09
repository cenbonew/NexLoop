"""Service object read projection: each requested property needs real EIOS READ."""
from datetime import UTC,datetime,timedelta
import hmac
import re
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType,resource_id
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.authorization import PostgresAuthorityProvider,resolve_authority
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.postgres_artifacts import canonical_payload

class AuthorizedObjectReader:
    def __init__(self,pool,session,signer):self.pool,self.session,self.signer=pool,session,signer

    def _authority(self,kind,name,operation=Operation.READ):
        target=resource_id(kind,name);entries=[]
        query=self.session.query(resource_id=target,resource_type=kind,operation=operation)
        context=resolve_authority(self.pool,self.session,query,entries)
        decision=AuthorizationDecisionService().decide_resolved(context)
        if not decision.allowed or not decision.authoritative or decision.obligations:raise ActionAuthorizationDenied('object/property read denied')
        return {'tenant_id':self.session.authentication.tenant_id,'principal_id':self.session.authentication.subject_principal_id,
          'credential_id':self.session.authentication.credential_id,'directory_hash':self.session.directory_hash,
          'world':self.session.world,'resource_id':target,'target_resource':target,'operation':operation.value,
          'expires_at':min(decision.expires_at,datetime.now(UTC)+timedelta(seconds=25)).isoformat(),
          'facts':sorted(entries,key=lambda row:(row['kind'],row['key']))}

    def get(self,type_name,object_id,*,fields=()):
        if (not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',type_name) or not re.fullmatch(r'[a-f0-9]{64}',object_id)
                or len(fields)>64 or any(type(f) is not str or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',f) for f in fields)):
            raise ValueError('invalid object projection')
        fields=tuple(sorted(set(fields)))
        if type_name=='Message' and fields and set(fields)<={'actor','body'}:
            from nexloop_eios.message_read import message_read_basis,derived_message_read_envelope
            basis=message_read_basis(self.pool,self.session,object_id)
            if basis.get('mode')=='derived':
                envelope=derived_message_read_envelope(self.pool,self.session,self.signer,object_id,basis,fields)
                with self.pool.connection() as c,c.transaction():
                    verify_application_role(c)
                    return c.execute('select authz.nexloop_read_object(%s,%s,%s,%s)',(self.session.token_digest,self.session.world,envelope['text'],envelope['signature'])).fetchone()[0]
        claims=self._authority(ResourceType.OBJECT,f'{type_name}/{object_id}')
        claims.update(protocol='nexloop-object-read-v1',key_id=self.signer.key_id,type_name=type_name,object_id=object_id,
            fields=fields,property_authorities=[self._authority(ResourceType.PROPERTY,f'{type_name}/{object_id}/{f}') for f in fields])
        text=canonical_payload(claims);signature=hmac.new(self.signer.material,('nexloop-object-read-v1:'+text).encode(),'sha256').hexdigest()
        with self.pool.connection() as c,c.transaction():
            verify_application_role(c)
            return c.execute('select authz.nexloop_read_object(%s,%s,%s,%s)',(self.session.token_digest,self.session.world,text,signature)).fetchone()[0]
