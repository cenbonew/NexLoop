from concurrent.futures import ThreadPoolExecutor
import secrets
import pytest
import psycopg
from psycopg.conninfo import make_conninfo
from eios.identity.errors import IdentityUnavailable,IdentityRateLimited
from eios.identity.ports import TrustedIdentityOperator
from eios.identity.rate_limits import IdentityRequestRateLimiter
from nexloop_eios.browser_rate_limits import PostgresBrowserRateLimiter,client_digest
from nexloop_eios.assembly import open_core
from nexloop_eios.bootstrap import bootstrap

OP=TrustedIdentityOperator(operator_principal_id='nexloop_api',request_id='synthetic-rate-request',trace_id='synthetic-rate-trace')

@pytest.fixture
def limiter(admin,pg):
    bootstrap(admin)
    # Administrative auth policy only. Application role never inserts policy/counters.
    admin.execute('insert into control.nexloop_browser_rate_policies values(%s,%s,%s,%s,%s,true)',('synthetic-a','local_login',OP.operator_principal_id,2,60))
    with open_core(make_conninfo(pg,user='nexloop_api')) as pool:
        yield PostgresBrowserRateLimiter(pool)


def test_durable_limit_reopen_and_frozen_request_port(limiter,pg,admin):
    digest=client_digest(secrets.token_bytes(32),canonical_origin='https://synthetic.example',client_identifier='synthetic-client')
    request=IdentityRequestRateLimiter(limiter,client_digest=digest,operator=OP)
    request.require('synthetic-a','local_login');request.require('synthetic-a','local_login')
    with pytest.raises(IdentityRateLimited):request.require('synthetic-a','local_login')
    with open_core(make_conninfo(pg,user='nexloop_api')) as reopened:
        denied=PostgresBrowserRateLimiter(reopened).consume('synthetic-a','local_login',digest,operator=OP)
        assert not denied.allowed and 1<=denied.retry_after_seconds<=60 and denied.remaining==0
    assert admin.execute('select attempts from control.nexloop_browser_rate_counters').fetchone()[0]==2


def test_concurrent_allowance_never_exceeds_policy(limiter,admin):
    digest=client_digest(secrets.token_bytes(32),canonical_origin='https://synthetic.example',client_identifier='synthetic-race')
    with ThreadPoolExecutor(max_workers=8) as executor:
        decisions=list(executor.map(lambda _:limiter.consume('synthetic-a','local_login',digest,operator=OP),range(12)))
    assert sum(d.allowed for d in decisions)==2
    assert admin.execute('select attempts from control.nexloop_browser_rate_counters').fetchone()[0]==2


def test_policy_revocation_and_tenant_operator_boundaries(limiter,admin):
    digest=client_digest(secrets.token_bytes(32),canonical_origin='https://synthetic.example',client_identifier='synthetic')
    for tenant,op in [('synthetic-b',OP),('synthetic-a',OP.model_copy(update={'operator_principal_id':'other'}))]:
        with pytest.raises(IdentityUnavailable):limiter.consume(tenant,'local_login',digest,operator=op)
    assert admin.execute('select count(*) from control.nexloop_browser_rate_counters').fetchone()[0]==0
    admin.execute('update control.nexloop_browser_rate_policies set active=false')
    with pytest.raises(IdentityUnavailable):limiter.consume('synthetic-a','local_login',digest,operator=OP)


def test_application_cannot_read_write_counters_or_policy(limiter):
    with limiter.pool.connection() as c:
        for statement in ['select * from control.nexloop_browser_rate_counters','select * from control.nexloop_browser_rate_policies',"insert into control.nexloop_browser_rate_counters values('x','local_login','o',decode(repeat('aa',32),'hex'),clock_timestamp(),0,1)"]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege),c.transaction():c.execute(statement)


def test_client_hmac_is_origin_bound():
    key=secrets.token_bytes(32)
    a=client_digest(key,canonical_origin='https://a.example',client_identifier='synthetic')
    assert a.get_secret_value()!=client_digest(key,canonical_origin='https://b.example',client_identifier='synthetic').get_secret_value()
    assert 'synthetic' not in repr(a)
    with pytest.raises(ValueError):client_digest(b'',canonical_origin='x',client_identifier='y')


def test_database_clock_window_expiry_and_client_isolation(limiter,admin):
    import time
    admin.execute('update control.nexloop_browser_rate_policies set window_seconds=1')
    key=secrets.token_bytes(32)
    a=client_digest(key,canonical_origin='https://synthetic.example',client_identifier='one')
    b=client_digest(key,canonical_origin='https://synthetic.example',client_identifier='two')
    for _ in range(2):assert limiter.consume('synthetic-a','local_login',a,operator=OP).allowed
    denied=limiter.consume('synthetic-a','local_login',a,operator=OP);assert not denied.allowed
    assert limiter.consume('synthetic-a','local_login',b,operator=OP).allowed
    time.sleep(1.1)
    renewed=limiter.consume('synthetic-a','local_login',a,operator=OP)
    assert renewed.allowed and renewed.revision>denied.revision and renewed.window_ends_at>denied.window_ends_at


def test_admin_and_worker_cannot_use_api_identity_function(limiter,admin,pg):
    args=('synthetic-a','local_login',b'a'*32,OP.operator_principal_id,OP.request_id,OP.trace_id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):admin.execute('select * from control.nexloop_consume_browser_rate_limit(%s,%s,%s,%s,%s,%s)',args)
    with psycopg.connect(make_conninfo(pg,user='nexloop_scheduler')) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('select * from control.nexloop_consume_browser_rate_limit(%s,%s,%s,%s,%s,%s)',args)
    assert admin.execute('select count(*) from control.nexloop_browser_rate_counters').fetchone()[0]==0


def test_configured_wrong_operator_cannot_replace_database_identity(limiter,admin):
    admin.execute('insert into control.nexloop_browser_rate_policies values(%s,%s,%s,10,60,true)',('synthetic-a','local_login','synthetic-spoofed-operator'))
    digest=client_digest(secrets.token_bytes(32),canonical_origin='https://synthetic.example',client_identifier='synthetic')
    fake=OP.model_copy(update={'operator_principal_id':'synthetic-spoofed-operator'})
    with pytest.raises(IdentityUnavailable):limiter.consume('synthetic-a','local_login',digest,operator=fake)
    # Bypass Python to prove the protected SQL function checks session identity too.
    with limiter.pool.connection() as c,c.transaction(),pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute('select * from control.nexloop_consume_browser_rate_limit(%s,%s,%s,%s,%s,%s)',('synthetic-a','local_login',digest.get_secret_value(),'synthetic-spoofed-operator',OP.request_id,OP.trace_id))
    assert admin.execute('select count(*) from control.nexloop_browser_rate_counters').fetchone()[0]==0


def test_operator_mapping_and_unvalidated_models_refused(limiter,admin):
    digest=client_digest(secrets.token_bytes(32),canonical_origin='https://synthetic.example',client_identifier='synthetic')
    for operator in [OP.model_dump(),OP.model_construct(operator_principal_id='nexloop_api',request_id='',trace_id='synthetic')]:
        with pytest.raises(IdentityUnavailable):limiter.consume('synthetic-a','local_login',digest,operator=operator)
    assert admin.execute('select count(*) from control.nexloop_browser_rate_counters').fetchone()[0]==0
