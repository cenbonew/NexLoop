from dataclasses import replace
import hashlib
from psycopg.conninfo import make_conninfo
import psycopg
import pytest
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service,authorization_service,PostgresAuthorityProvider
from nexloop_eios.local_artifacts import LocalBlobStore,AuthorizedArtifactAccess,ArtifactAccessDenied
from authority_fixture import seed_authority,replace_fact


def test_real_pg_service_resolver_authorizes_exact_artifact(admin,pg,tmp_path):
    bootstrap(admin)
    with LocalBlobStore(tmp_path/'blobs') as store:
        ref=store.put(tenant_id='synthetic-a',world='real',payload=b'synthetic evidence',media_type='text/plain')
        token,_=seed_authority(admin,ref.tenant_id,ref.resource_id)
        with open_core(make_conninfo(pg,user='nexloop_api')) as pool:
            session=authenticate_service(pool,token,world='real')
            assert session.authentication.subject_kind.value=='service'
            assert not hasattr(session.authentication,'session_id')
            assert token not in repr(session) and hashlib.sha256(token.encode()).hexdigest() not in repr(session)
            q=session.query(resource_id=ref.resource_id,resource_type=ResourceType.ARTIFACT,operation=Operation.READ)
            decision=authorization_service(pool,session).decide(q)
            assert decision.allowed and decision.authoritative and decision.matched_grants==('artifact-grant',)
            assert decision.matched_policies==('world-policy',)
            assert AuthorizedArtifactAccess(store,authorization_service(pool,session)).read(q,ref,start=1,stop=5)==b'ynth'
            # Real restricted app role cannot mutate authoritative source rows.
            with pool.connection() as c,c.transaction():
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute("update authz.nexloop_authority_facts set payload='{}'")


def test_two_tenants_and_worlds_cannot_forge_server_binding(admin,pg,tmp_path):
    bootstrap(admin)
    resource='eios:artifact:synthetic'
    a,_=seed_authority(admin,'synthetic-a',resource)
    seed_authority(admin,'synthetic-b',resource)
    with open_core(make_conninfo(pg,user='nexloop_api')) as pool:
        with pytest.raises(AuthorizationUnavailable):authenticate_service(pool,a,world='shadow')
        with pytest.raises(AuthorizationUnavailable):authenticate_service(pool,'invalid-credential'*4,world='real')
        session=authenticate_service(pool,a,world='real')
        assert session.authentication.tenant_id=='synthetic-a'
        q=session.query(resource_id=resource,resource_type=ResourceType.ARTIFACT,operation=Operation.READ)
        forged_binding=F.CredentialAuthenticationBinding.model_validate_json(session.authentication.model_dump_json().replace('synthetic-a','synthetic-b'))
        forged=q.model_copy(update={'tenant_id':'synthetic-b','authentication':forged_binding,
            'target':q.target.model_copy(update={'tenant_id':'synthetic-b'})})
        with pytest.raises(AuthorizationUnavailable):authorization_service(pool,session).decide(forged)
        with pool.connection() as c,c.transaction():
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute('select authz.nexloop_load_authority_fact(%s,%s,%s,%s)',(session.token_digest,'real','subject',['synthetic-b-service']))


@pytest.mark.parametrize('layer',['grants','application','controls','policies','scope','membership','credential'])
def test_live_revocation_or_missing_authority_fails_closed(admin,pg,layer):
    bootstrap(admin);tenant='synthetic-a';resource='eios:artifact:synthetic'
    token,_=seed_authority(admin,tenant,resource)
    with open_core(make_conninfo(pg,user='nexloop_api')) as pool:
        session=authenticate_service(pool,token,world='real')
        q=session.query(resource_id=resource,resource_type=ResourceType.ARTIFACT,operation=Operation.READ)
        service=authorization_service(pool,session)
        assert service.decide(q).allowed
        principal=tenant+'-principal'
        if layer=='credential':
            admin.execute("update authz.nexloop_service_credentials set status='revoked' where tenant_id=%s",(tenant,))
        elif layer=='membership':
            replace_fact(admin,tenant,layer,[tenant+'-service',principal],F.MembershipFacts,status='revoked')
        elif layer=='application':
            replace_fact(admin,tenant,layer,['eios:application:'+tenant+'-core','1'],F.ApplicationFacts,resources=[])
        elif layer=='grants':
            replace_fact(admin,tenant,layer,[principal,resource],F.GrantFacts,grants=[])
        else:
            admin.execute('delete from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s',(tenant,layer))
        try:
            result=service.decide(q)
        except (AuthorizationUnavailable,F.AuthorizationFactDenied):
            pass
        else:
            assert not result.allowed


def test_database_policy_can_deny_even_when_grant_and_scope_allow(admin,pg):
    from psycopg.types.json import Jsonb
    from eios.authz.policy.models import Eq,ActionParameterRef,LiteralValue
    from eios.authz.policy.canonical import canonicalize_policy_expression
    bootstrap(admin);tenant='synthetic-a';resource='eios:artifact:synthetic'
    token,records=seed_authority(admin,tenant,resource)
    policy=next(fact for kind,key,fact in records if kind=='policies')
    values=policy.rules[0].model_dump(exclude={'condition','canonical_ast_utf8','canonical_ast_sha256','version_digest','active_version'})
    condition=Eq(op='eq',node_id='world-denial',left=ActionParameterRef(kind='action_parameter',parameter_name='world'),right=LiteralValue(kind='literal',value='shadow'))
    canonical=canonicalize_policy_expression(condition)
    rule=F.PolicyRuleFact(**values,condition=condition,canonical_ast_utf8=canonical.utf8,canonical_ast_sha256=canonical.sha256,version_digest=canonical.sha256,active_version=2)
    replace_fact(admin,tenant,'policies',[tenant+'-principal',resource,'read'],F.PolicyFacts,rules=[rule.model_dump(mode='json')])
    with open_core(make_conninfo(pg,user='nexloop_api')) as pool:
        session=authenticate_service(pool,token,world='real')
        result=authorization_service(pool,session).decide(session.query(resource_id=resource,resource_type=ResourceType.ARTIFACT,operation=Operation.READ))
        assert not result.allowed and 'policy_denied' in result.reason_codes


def test_suspended_tenant_cannot_authenticate_or_reuse_session(admin,pg):
    bootstrap(admin);token,_=seed_authority(admin,'synthetic-a','eios:artifact:synthetic')
    with open_core(make_conninfo(pg,user='nexloop_api')) as pool:
        session=authenticate_service(pool,token,world='real')
        q=session.query(resource_id='eios:artifact:synthetic',resource_type=ResourceType.ARTIFACT,operation=Operation.READ)
        assert authorization_service(pool,session).decide(q).allowed
        admin.execute("update control.nexloop_tenants set status='suspended' where tenant_id='synthetic-a'")
        with pytest.raises(AuthorizationUnavailable):authenticate_service(pool,token,world='real')
        with pytest.raises(AuthorizationUnavailable):authorization_service(pool,session).decide(q)
