import time
import pytest
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.backend import open_backend
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from test_action_definitions import published_action
from test_agent_governed_write import configuration,args
from agent_authority_fixture import seed_agent

RESOURCE='eios:action:Consumer.create:1'


def test_server_run_credential_current_agent_write_and_replay(published_action,admin,pg,tmp_path):
    reader,_,_=published_action;token,_=seed_agent(admin)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        parent=backend.authenticate(token,world='real')
        issued=parent.issue_run_credential(action_resources=[RESOURCE])
        assert issued.token not in repr(issued) and issued.audience=='nexloop-agent-host'
        run=backend.authenticate_run(issued.token,world='real',run_id=issued.run_id)
        result=run.create_object(**args());assert run.create_object(**args())==result
        with pytest.raises(ActionAuthorizationDenied):run.issue_run_credential(action_resources=[RESOURCE])
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==1


@pytest.mark.parametrize('denial',['wrong-run','wrong-world','root-entry','wrong-audience'])
def test_run_token_boundaries(published_action,admin,pg,tmp_path,denial):
    from nexloop_eios.authorization import authenticate_service
    reader,_,_=published_action;token,_=seed_agent(admin)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        issued=backend.authenticate(token,world='real').issue_run_credential(action_resources=[RESOURCE])
        with pytest.raises(AuthorizationUnavailable):
            if denial=='root-entry':backend.authenticate(issued.token,world='real')
            elif denial=='wrong-audience':authenticate_service(backend._pool,issued.token,world='real',run_id=issued.run_id,audience='untrusted-client')
            else:backend.authenticate_run(issued.token,world='test' if denial=='wrong-world' else 'real',run_id='00000000-0000-0000-0000-000000000000' if denial=='wrong-run' else issued.run_id)
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


def test_run_expiry_and_source_revocation(published_action,admin,pg,tmp_path):
    import hashlib
    reader,_,_=published_action;token,_=seed_agent(admin)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        parent=backend.authenticate(token,world='real')
        short=parent.issue_run_credential(action_resources=[RESOURCE],ttl_seconds=1)
        time.sleep(1.1)
        with pytest.raises(AuthorizationUnavailable):backend.authenticate_run(short.token,world='real',run_id=short.run_id)
        issued=parent.issue_run_credential(action_resources=[RESOURCE])
        run=backend.authenticate_run(issued.token,world='real',run_id=issued.run_id)
        admin.execute("update authz.nexloop_service_credentials set status='revoked' where token_digest=%s",(hashlib.sha256(token.encode()).hexdigest(),))
        with pytest.raises(AuthorizationUnavailable):run.create_object(**args())
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


@pytest.mark.parametrize('denial',['ttl-zero','ttl-over-budget','ungranted-resource'])
def test_issuer_rejects_bad_budget_and_ungranted_resource(published_action,admin,pg,tmp_path,denial):
    reader,_,_=published_action;token,_=seed_agent(admin)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        parent=backend.authenticate(token,world='real')
        with pytest.raises((ValueError,AuthorizationUnavailable,ActionAuthorizationDenied)):
            parent.issue_run_credential(action_resources=['eios:action:Unowned.create:1'] if denial=='ungranted-resource' else [RESOURCE],ttl_seconds=0 if denial=='ttl-zero' else 301 if denial=='ttl-over-budget' else 300)
    assert admin.execute('select count(*) from authz.nexloop_run_credentials').fetchone()[0]==0


@pytest.mark.parametrize('mutation',['grant','release','run'])
def test_cached_run_rechecks_revocation(published_action,admin,pg,tmp_path,mutation):
    import hashlib
    from authority_fixture import replace_fact
    from eios.authz import facts as F
    reader,_,_=published_action;token,invocation=seed_agent(admin)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        issued=backend.authenticate(token,world='real').issue_run_credential(action_resources=[RESOURCE])
        run=backend.authenticate_run(issued.token,world='real',run_id=issued.run_id)
        if mutation=='grant':replace_fact(admin,'synthetic-a','grants',[invocation.actor_principal_id,RESOURCE],F.GrantFacts,grants=[])
        elif mutation=='release':replace_fact(admin,'synthetic-a','agent_release',[invocation.release_id],F.AgentReleaseFacts,status='revoked')
        else:admin.execute("update authz.nexloop_run_credentials set status='revoked' where token_digest=%s",(hashlib.sha256(issued.token.encode()).hexdigest(),))
        with pytest.raises(AuthorizationUnavailable):run.create_object(**args())
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


def test_signed_run_action_rechecks_revoke_before_sql(published_action,admin,pg,tmp_path,monkeypatch):
    import hashlib
    import psycopg
    import nexloop_eios.object_actions as module
    reader,_,_=published_action;token,_=seed_agent(admin)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        issued=backend.authenticate(token,world='real').issue_run_credential(action_resources=[RESOURCE])
        run=backend.authenticate_run(issued.token,world='real',run_id=issued.run_id)
        original=module.hmac.new;revoked=False
        def intercept(key,msg,*pos,**kwargs):
            nonlocal revoked
            if msg.startswith(b'nexloop-object-create-v1:'):
                admin.execute("update authz.nexloop_run_credentials set status='revoked' where token_digest=%s",(hashlib.sha256(issued.token.encode()).hexdigest(),));revoked=True
            return original(key,msg,*pos,**kwargs)
        monkeypatch.setattr(module.hmac,'new',intercept)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):run.create_object(**args())
    assert revoked and admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


def test_run_directory_hash_is_timezone_invariant(published_action,admin,pg,tmp_path):
    import hashlib
    reader,_,_=published_action;token,_=seed_agent(admin)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        issued=backend.authenticate(token,world='real').issue_run_credential(action_resources=[RESOURCE])
        with backend._pool.connection() as c:
            c.execute("set timezone='Pacific/Honolulu'")
            a=c.execute('select authz.nexloop_service_identity_snapshot(%s,%s)',(hashlib.sha256(issued.token.encode()).hexdigest(),'real')).fetchone()[0]['directory_hash']
            c.execute("set timezone='Asia/Shanghai'")
            b=c.execute('select authz.nexloop_service_identity_snapshot(%s,%s)',(hashlib.sha256(issued.token.encode()).hexdigest(),'real')).fetchone()[0]['directory_hash']
            assert a==b


def test_run_cannot_read_arbitrary_authority_target(published_action,admin,pg,tmp_path):
    import hashlib,psycopg
    reader,_,_=published_action;token,_=seed_agent(admin)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        issued=backend.authenticate(token,world='real').issue_run_credential(action_resources=[RESOURCE])
        with backend._pool.connection() as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('select authz.nexloop_load_authority_fact_snapshot(%s,%s,%s,%s)',(hashlib.sha256(issued.token.encode()).hexdigest(),'real','resource_graph',['eios:action:Unowned.create:1']))
