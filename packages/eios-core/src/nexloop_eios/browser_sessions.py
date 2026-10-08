"""Canonical Evidence consumption and new Session in one restricted transaction."""
from eios.identity.models import BrowserSession
from eios.identity.ports import CreateBrowserSessionCommand,TrustedIdentityOperator
from eios.identity.evidence import authentication_evidence_proof_digest
from eios.identity.errors import CredentialInvalid,IdentityUnavailable
from nexloop_eios.browser_evidence import PostgresBrowserEvidenceStore
from nexloop_eios.browser_identity import verify_identity_role
from psycopg.types.json import Jsonb


class PostgresBrowserSessionUnitOfWork(PostgresBrowserEvidenceStore):
    def create_session(self,command,*,operator):
        try:
            if type(command) is not CreateBrowserSessionCommand or type(operator) is not TrustedIdentityOperator:raise ValueError()
            command=CreateBrowserSessionCommand.model_validate(command);operator=TrustedIdentityOperator.model_validate(operator)
            session=BrowserSession.model_validate(command.session)
            if session.tenant_id!=self.tenant_id or session.application_id!=self.application_id or operator.operator_principal_id!='nexloop_identity':raise ValueError()
            body=session.model_dump(mode='json')
            with self.pool.connection() as c,c.transaction():
                verify_identity_role(c)
                params=(self.tenant_id,self.application_id,operator.operator_principal_id,command.authentication_evidence_id,
                    authentication_evidence_proof_digest(command.authentication_evidence_proof.get_secret_value()),Jsonb(body),
                    session.session_token_digest.get_secret_value(),session.csrf_token_digest.get_secret_value())
                if command.replaced_session_id is None:
                    query='select control.nexloop_create_browser_session(%s,%s,%s,%s,%s,%s,%s,%s)'
                else:
                    query='select control.nexloop_replace_browser_login_session(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)'
                    params+= (command.replaced_session_id,command.replaced_session_token_digest.get_secret_value(),command.expected_replaced_session_revision)
                result=c.execute(query,params).fetchone()[0]
                if result!=body:raise ValueError()
            return session
        except Exception as exc:
            if getattr(exc,'sqlstate',None)=='EID03':raise CredentialInvalid('session is invalid or expired') from None
            raise IdentityUnavailable('identity service is unavailable') from None

    def _read_session(self,session_id=None,token_digest=None):
        from datetime import datetime
        from eios.identity.models import SecretDigest32,SubjectKind
        try:
            if session_id is not None and (type(session_id) is not str or not 0<len(session_id)<=320):raise ValueError()
            if token_digest is not None and type(token_digest) is not SecretDigest32:raise ValueError()
            with self.pool.connection() as c,c.transaction():
                c.execute('set transaction isolation level repeatable read read only');verify_identity_role(c)
                value=c.execute('select control.nexloop_read_browser_session(%s,%s,%s,%s,%s)',
                    (self.tenant_id,self.application_id,'nexloop_identity',session_id,None if token_digest is None else token_digest.get_secret_value())).fetchone()[0]
                if value is None:return None
                return decode_session(value)
        except Exception:raise IdentityUnavailable('identity service is unavailable') from None

    def get_session(self,session_id):return self._read_session(session_id=session_id)
    def find_by_token_digest(self,token_digest):return self._read_session(token_digest=token_digest)


    def touch_session(self,command,*,operator):
        from eios.identity.ports import TouchBrowserSessionCommand
        try:
            if type(command) is not TouchBrowserSessionCommand or type(operator) is not TrustedIdentityOperator:raise ValueError()
            command=TouchBrowserSessionCommand.model_validate(command);operator=TrustedIdentityOperator.model_validate(operator)
            if command.tenant_id!=self.tenant_id or command.application_id!=self.application_id or operator.operator_principal_id!='nexloop_identity':raise ValueError()
            with self.pool.connection() as c,c.transaction():
                verify_identity_role(c)
                value=c.execute('select control.nexloop_touch_browser_session(%s,%s,%s,%s,%s)',
                    (self.tenant_id,self.application_id,operator.operator_principal_id,Jsonb(command.model_dump(mode='json')),
                     None if command.new_csrf_token_digest is None else command.new_csrf_token_digest.get_secret_value())).fetchone()[0]
                result=decode_session(value)
                if result.last_seen_at!=command.seen_at or result.revision!=command.expected_revision:raise ValueError()
            return result
        except Exception as exc:
            if getattr(exc,'sqlstate',None)=='EID03':raise CredentialInvalid('session is invalid or expired') from None
            raise IdentityUnavailable('identity service is unavailable') from None


    def revoke_session(self,command,*,operator):
        from eios.identity.ports import RevokeBrowserSessionCommand
        try:
            if type(command) is not RevokeBrowserSessionCommand or type(operator) is not TrustedIdentityOperator:raise ValueError()
            command=RevokeBrowserSessionCommand.model_validate(command);operator=TrustedIdentityOperator.model_validate(operator)
            if operator.operator_principal_id!='nexloop_identity':raise ValueError()
            with self.pool.connection() as c,c.transaction():
                verify_identity_role(c)
                value=c.execute('select control.nexloop_revoke_browser_session(%s,%s,%s,%s)',
                    (self.tenant_id,self.application_id,operator.operator_principal_id,Jsonb(command.model_dump(mode='json')))).fetchone()[0]
                result=decode_session(value)
                if result.revoked_at!=command.revoked_at or result.revision!=command.expected_revision+1:raise ValueError()
            return result
        except Exception as exc:
            if getattr(exc,'sqlstate',None)=='EID03':raise CredentialInvalid('session is invalid or expired') from None
            raise IdentityUnavailable('identity service is unavailable') from None


    def list_memberships(self,subject_id):
        from eios.identity.models import TenantMembership
        import json
        try:
            with self.pool.connection() as c,c.transaction():
                c.execute('set transaction read only');verify_identity_role(c)
                values=c.execute('select control.nexloop_list_browser_memberships(%s,%s,%s,%s)',
                    (self.tenant_id,self.application_id,'nexloop_identity',subject_id)).fetchone()[0]
                return tuple(TenantMembership.model_validate_json(json.dumps(v)) for v in values)
        except Exception:raise IdentityUnavailable('identity service is unavailable') from None

    def rotate_session(self,command,*,operator):
        from eios.identity.ports import RotateBrowserSessionCommand,BrowserSessionRotationResult
        try:
            if type(command) is not RotateBrowserSessionCommand or type(operator) is not TrustedIdentityOperator:raise ValueError()
            command=RotateBrowserSessionCommand.model_validate(command);operator=TrustedIdentityOperator.model_validate(operator)
            if command.source_tenant_id!=self.tenant_id or command.source_application_id!=self.application_id or operator.operator_principal_id!='nexloop_identity':raise ValueError()
            replacement=command.replacement_session;body=command.model_dump(mode='json')
            with self.pool.connection() as c,c.transaction():
                verify_identity_role(c)
                result=c.execute('select control.nexloop_rotate_browser_tenant(%s,%s,%s,%s,%s,%s,%s,%s)',
                    (self.tenant_id,self.application_id,operator.operator_principal_id,Jsonb(body),command.source_session_token_digest.get_secret_value(),
                     command.source_csrf_token_digest.get_secret_value(),replacement.session_token_digest.get_secret_value(),replacement.csrf_token_digest.get_secret_value())).fetchone()[0]
                if result!=replacement.model_dump(mode='json'):raise ValueError()
            return BrowserSessionRotationResult(source_session_id=command.source_session_id,source_revoked_at=command.revoked_at,replacement_session=replacement)
        except Exception as exc:
            if getattr(exc,'sqlstate',None)=='EID03':raise CredentialInvalid('session is invalid or expired') from None
            raise IdentityUnavailable('identity service is unavailable') from None


def decode_session(value):
    from datetime import datetime
    from eios.identity.models import SecretDigest32,SubjectKind
    value=dict(value)
    for name in ('session_token_digest','csrf_token_digest'):value[name]=SecretDigest32(bytes.fromhex(value[name]))
    for name in ('created_at','last_seen_at','idle_expires_at','absolute_expires_at','revoked_at'):value[name]=None if value[name] is None else datetime.fromisoformat(value[name])
    value['authentication_methods']=tuple(value['authentication_methods'])
    value['subject_kind']=SubjectKind(value['subject_kind'])
    return BrowserSession.model_validate(value)
