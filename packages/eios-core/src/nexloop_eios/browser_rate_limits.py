"""Durable implementation of the frozen EIOS identity rate-limit port."""
import hashlib
import hmac
from eios.identity.models import SecretDigest32
from eios.identity.ports import TrustedIdentityOperator
from eios.identity.rate_limits import IdentityRateLimitDecision
from eios.identity.errors import IdentityUnavailable
from nexloop_eios.assembly import verify_application_role


def client_digest(key,*,canonical_origin,client_identifier):
    if type(key) is not bytes or len(key)!=32 or type(canonical_origin) is not str or not canonical_origin or type(client_identifier) is not str or not 0<len(client_identifier)<=1024:
        raise ValueError('server-owned rate limit binding required')
    return SecretDigest32(hmac.new(key,(canonical_origin+'\0'+client_identifier).encode(),'sha256').digest())


class PostgresBrowserRateLimiter:
    def __init__(self,pool,*,identity_operator=False):self.pool=pool;self.identity_operator=identity_operator

    def consume(self,tenant_id,action,digest,*,operator):
        try:
            if type(operator) is not TrustedIdentityOperator:raise ValueError()
            raw=object.__getattribute__(operator,'__dict__')
            fields={'operator_principal_id','request_id','trace_id'}
            if type(raw) is not dict or set(raw)!=fields or operator.__pydantic_extra__ is not None or operator.__pydantic_private__ is not None:
                raise ValueError()
            operator=TrustedIdentityOperator.model_validate(dict(raw))
            if type(digest) is not SecretDigest32:raise ValueError()
            with self.pool.connection() as c,c.transaction():
                if self.identity_operator:
                    from nexloop_eios.browser_identity import verify_identity_role
                    verify_identity_role(c)
                    if operator.operator_principal_id!='nexloop_identity':raise ValueError()
                elif verify_application_role(c)!='nexloop_api' or operator.operator_principal_id!='nexloop_api':raise ValueError()
                row=c.execute('select * from control.nexloop_consume_browser_rate_limit(%s,%s,%s,%s,%s,%s)',
                    (tenant_id,action,digest.get_secret_value(),operator.operator_principal_id,operator.request_id,operator.trace_id)).fetchone()
                decision=IdentityRateLimitDecision(allowed=row[0],retry_after_seconds=row[1],remaining=row[2],window_ends_at=row[3],revision=row[4])
            return decision
        except Exception:raise IdentityUnavailable('identity service is unavailable') from None
