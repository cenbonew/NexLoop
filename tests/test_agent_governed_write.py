import pytest
import psycopg
from psycopg.conninfo import make_conninfo
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.authorization import authenticate_service,PostgresAuthorityProvider
from nexloop_eios.backend import open_backend
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from authority_fixture import replace_fact
from agent_authority_fixture import seed_agent,seed_parent
from test_action_definitions import published_action


def configuration(reader,pg,tmp_path):
    key=tmp_path/'agent-signing-key';key.write_bytes(reader.signer.material);key.chmod(0o600)
    return dict(database_url=make_conninfo(pg,user='nexloop_api'),artifact_root=tmp_path/'artifacts',signing_key_file=key,signing_key_id=reader.signer.key_id)


def args():return dict(action_name='Consumer.create',action_version=1,intent_id='synthetic-agent-create-001',type_name='Consumer',properties={})


def test_real_pg_agent_governed_instance_write_with_durable_receipt(published_action,admin,pg,tmp_path):
    reader,_,_=published_action;token,invocation=seed_agent(admin)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        service=backend.authenticate(token,world='real')
        assert service._session.authentication.subject_kind.value=='agent' and service._session.agent_invocation==invocation
        receipt=service.create_object(**args());assert service.create_object(**args())==receipt
        with backend._pool.connection() as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('select * from ontology.objects')
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==1


@pytest.mark.parametrize('denial',['release-revoked','agent-disabled','ceiling-empty','missing-release','wrong-scope','wrong-actor','grant-empty','incomplete-chain'])
def test_agent_denials_leave_business_rows_empty(published_action,admin,pg,tmp_path,denial):
    reader,_,_=published_action;token,invocation=seed_agent(admin)
    if denial=='release-revoked':replace_fact(admin,'synthetic-a','agent_release',[invocation.release_id],F.AgentReleaseFacts,status='revoked')
    elif denial=='agent-disabled':replace_fact(admin,'synthetic-a','agent',[invocation.agent_id],F.AgentFacts,status='disabled')
    elif denial=='ceiling-empty':replace_fact(admin,'synthetic-a','agent_application',[invocation.agent_application_id,'1'],F.ApplicationFacts,resources=(),operations=())
    elif denial=='missing-release':admin.execute("delete from authz.nexloop_authority_facts where fact_kind='agent_release'")
    elif denial=='wrong-actor':replace_fact(admin,'synthetic-a','agent',[invocation.agent_id],F.AgentFacts,actor_principal_id='foreign-principal')
    elif denial=='grant-empty':replace_fact(admin,'synthetic-a','grants',[invocation.actor_principal_id,'eios:action:Consumer.create:1'],F.GrantFacts,grants=[])
    elif denial=='incomplete-chain':replace_fact(admin,'synthetic-a','agent_release',[invocation.release_id],F.AgentReleaseFacts,chain_complete=False)
    else:replace_fact(admin,'synthetic-a','scope',[invocation.actor_principal_id,'eios:action:Consumer.create:1','execute'],F.ScopeAuthorityFacts,authorized_scopes=['action.read'],catalog_scopes=['action.execute','action.read'])
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        service=backend.authenticate(token,world='real')
        with pytest.raises((AuthorizationUnavailable,F.AuthorizationFactDenied,ActionAuthorizationDenied)):service.create_object(**args())
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


def test_caller_cannot_replace_authenticated_agent_invocation(published_action,admin):
    reader,_,_=published_action;token,invocation=seed_agent(admin)
    session=authenticate_service(reader.pool,token,world='real')
    query=session.query(resource_id='eios:action:Consumer.create:1',resource_type=ResourceType.ACTION,operation=Operation.EXECUTE)
    query=query.model_copy(update={'agent_invocation':invocation.model_copy(update={'release_id':'caller-forged-release'})})
    with pytest.raises(AuthorizationUnavailable):
        with PostgresAuthorityProvider(reader.pool,session).open_unit_of_work(query):pass


def test_release_revocation_after_authenticated_session_invalidates_dispatch(published_action,admin,pg,tmp_path):
    reader,_,_=published_action;token,invocation=seed_agent(admin)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        service=backend.authenticate(token,world='real')
        replace_fact(admin,'synthetic-a','agent_release',[invocation.release_id],F.AgentReleaseFacts,status='revoked')
        with pytest.raises(AuthorizationUnavailable):service.create_object(**args())
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


@pytest.mark.parametrize('revocation',['release','grant','credential'])
def test_agent_revocation_after_signature_before_sql_rejects_effect(published_action,admin,pg,tmp_path,monkeypatch,revocation):
    import nexloop_eios.object_actions as module
    reader,_,_=published_action;token,invocation=seed_agent(admin)
    original=module.hmac.new;revoked=False
    def at_signature(key,msg,*pos,**kwargs):
        nonlocal revoked
        if msg.startswith(b'nexloop-object-create-v1:'):
            if revocation=='release':replace_fact(admin,'synthetic-a','agent_release',[invocation.release_id],F.AgentReleaseFacts,status='revoked')
            elif revocation=='grant':replace_fact(admin,'synthetic-a','grants',[invocation.actor_principal_id,'eios:action:Consumer.create:1'],F.GrantFacts,grants=[])
            else:
                import hashlib
                admin.execute("update authz.nexloop_service_credentials set status='revoked' where token_digest=%s",(hashlib.sha256(token.encode()).hexdigest(),))
            revoked=True
        return original(key,msg,*pos,**kwargs)
    monkeypatch.setattr(module.hmac,'new',at_signature)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        service=backend.authenticate(token,world='real')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):service.create_object(**args())
    assert revoked and admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


def test_child_agent_cannot_expand_parent_release_ceiling(published_action,admin,pg,tmp_path):
    reader,_,_=published_action;token,invocation=seed_agent(admin)
    seed_parent(admin,invocation,allow=False)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        with pytest.raises(AuthorizationUnavailable):backend.authenticate(token,world='real').create_object(**args())
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


def test_agent_credential_wrong_world_denied(published_action,admin,pg,tmp_path):
    reader,_,_=published_action;token,_=seed_agent(admin)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        with pytest.raises(AuthorizationUnavailable):backend.authenticate(token,world='test')
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


def test_agent_cannot_write_property_outside_published_action_schema(published_action,admin,pg,tmp_path):
    reader,_,_=published_action;token,_=seed_agent(admin)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        request=args();request['properties']={'unpublished_secret_field':'synthetic-value'}
        with pytest.raises(ValueError):backend.authenticate(token,world='real').create_object(**request)
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


def test_current_parent_release_chain_allows_governed_child_write(published_action,admin,pg,tmp_path):
    reader,_,_=published_action;token,invocation=seed_agent(admin);seed_parent(admin,invocation)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        backend.authenticate(token,world='real').create_object(**args())
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==1


@pytest.mark.parametrize('mutation',['release-revoked','agent-disabled','application-revoked','missing-parent'])
def test_stale_embedded_parent_cannot_authorize_child(published_action,admin,pg,tmp_path,mutation):
    reader,_,_=published_action;token,invocation=seed_agent(admin);parent=seed_parent(admin,invocation)
    if mutation=='release-revoked':replace_fact(admin,'synthetic-a','agent_release',[parent.release_id],F.AgentReleaseFacts,status='revoked')
    elif mutation=='agent-disabled':replace_fact(admin,'synthetic-a','agent',[parent.agent_id],F.AgentFacts,status='disabled')
    elif mutation=='application-revoked':replace_fact(admin,'synthetic-a','agent_application',[parent.application_id,parent.application_version],F.ApplicationFacts,application_status='revoked',eligible=False)
    else:admin.execute("delete from authz.nexloop_authority_facts where fact_kind='agent_release' and entity_key=%s",([parent.release_id],))
    # Fresh authentication cannot revive an unchanged embedded parent frame.
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        with pytest.raises(AuthorizationUnavailable):backend.authenticate(token,world='real').create_object(**args())
    assert admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


def test_parent_revoked_between_signed_claim_and_sql_denies_child(published_action,admin,pg,tmp_path,monkeypatch):
    import nexloop_eios.object_actions as module
    reader,_,_=published_action;token,invocation=seed_agent(admin);parent=seed_parent(admin,invocation)
    original=module.hmac.new;revoked=False
    def at_signature(key,msg,*pos,**kwargs):
        nonlocal revoked
        if msg.startswith(b'nexloop-object-create-v1:'):
            replace_fact(admin,'synthetic-a','agent_release',[parent.release_id],F.AgentReleaseFacts,status='revoked')
            revoked=True
        return original(key,msg,*pos,**kwargs)
    monkeypatch.setattr(module.hmac,'new',at_signature)
    with open_backend(**configuration(reader,pg,tmp_path)) as backend:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):backend.authenticate(token,world='real').create_object(**args())
    assert revoked and admin.execute('select count(*) from ontology.objects').fetchone()[0]==0


def test_service_cannot_read_agent_release_authority_port(published_action,admin):
    reader,_,_=published_action;_,invocation=seed_agent(admin)
    with reader.pool.connection() as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute('select authz.nexloop_load_authority_fact_snapshot(%s,%s,%s,%s)',(reader.session.token_digest,'real','agent_release',[invocation.release_id]))


def test_agent_credential_cannot_bind_different_subject_even_same_principal(published_action,admin):
    import hashlib
    from psycopg.types.json import Jsonb
    reader,_,_=published_action;token,invocation=seed_agent(admin)
    body=invocation.model_dump(mode='json');body['agent_id']='eios:agent:other-subject'
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute('update authz.nexloop_service_credentials set agent_invocation=%s where token_digest=%s',(Jsonb(body),hashlib.sha256(token.encode()).hexdigest()))
