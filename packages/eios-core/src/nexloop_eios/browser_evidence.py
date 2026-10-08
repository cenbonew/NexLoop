"""Private durable local-password Evidence port; no standalone consume."""
from dataclasses import asdict
from datetime import datetime
from eios.identity.evidence import AuthenticationEvidenceFacts,AuthenticationEvidenceRecord,AuthenticationEvidenceRejected,authentication_evidence_record_is_exact
from eios.identity.errors import IdentityUnavailable
from nexloop_eios.browser_identity import PostgresBrowserLocalAccountRepository,verify_identity_role
from psycopg.types.json import Jsonb


class PostgresBrowserEvidenceStore(PostgresBrowserLocalAccountRepository):
    def _evidence(self,mode,evidence_id,proof_digest,facts=None):
        try:
            if type(evidence_id) is not str or not 0<len(evidence_id)<=320 or type(proof_digest) is not bytes or len(proof_digest)!=32:raise ValueError()
            with self.pool.connection() as c,c.transaction():
                verify_identity_role(c)
                value=c.execute('select control.nexloop_browser_evidence(%s,%s,%s,%s,%s,%s,%s)',
                    (self.tenant_id,self.application_id,'nexloop_identity',mode,evidence_id,proof_digest,None if facts is None else Jsonb(asdict(facts)))).fetchone()[0]
                if value is None:return None
                value=dict(value);value['proof_digest']=proof_digest
                value['authentication_methods']=tuple(value['authentication_methods'])
                for name in ('issued_at','expires_at'):value[name]=datetime.fromisoformat(value[name])
                record=AuthenticationEvidenceRecord(**value)
                if facts is not None and not authentication_evidence_record_is_exact(record,evidence_id,proof_digest,facts):raise ValueError()
            return record
        except Exception as exc:
            if getattr(exc,'sqlstate',None)=='EID03':raise AuthenticationEvidenceRejected from None
            raise IdentityUnavailable('authentication evidence is unavailable') from None

    def issue(self,evidence_id,proof_digest,facts):
        if type(facts) is not AuthenticationEvidenceFacts:raise IdentityUnavailable('authentication evidence is unavailable')
        if facts.tenant_id!=self.tenant_id or facts.application_id!=self.application_id:raise AuthenticationEvidenceRejected
        return self._evidence('issue',evidence_id,proof_digest,facts)

    def inspect(self,evidence_id,proof_digest,*,tenant_id,application_id,purpose):
        if tenant_id!=self.tenant_id or application_id!=self.application_id or purpose!='session.create':return None
        try:return self._evidence('inspect',evidence_id,proof_digest)
        except AuthenticationEvidenceRejected:return None
