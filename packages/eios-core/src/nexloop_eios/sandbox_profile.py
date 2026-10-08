"""Explicit fresh-sandbox service authority configuration; test world only.

Typed EIOS facts are configuration, never a runtime Memory authorization provider.
"""
from datetime import UTC,datetime,timedelta
import hashlib
import secrets
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.applications import ApplicationMode,ResourceRestriction,OperationRestriction
from eios.authz.grants import GrantSubjectKind
from eios.authz.operations import Operation
from eios.authz.resources import ResourceReference,ResourceType
from eios.authz.policy.models import Eq,LiteralValue,ActionParameterRef
from eios.authz.policy.canonical import canonicalize_policy_expression
from eios.authz.policy.registry import PolicyEffect
from eios.identity.models import SubjectKind,MembershipKind
from nexloop_eios.authorization import WITNESS


def _artifact_test_records(tenant,resource_id,world='real',operation=Operation.READ,operations=None,resource_type=ResourceType.ARTIFACT,identity_suffix=""):
    operations=tuple(operations or (operation,))
    now=datetime.now(UTC);past=now-timedelta(days=1);expiry=now+timedelta(hours=1)
    kind=resource_type;identity=tenant+identity_suffix;principal=f'{identity}-principal';subject=f'{identity}-service'
    scopes=frozenset(f'{kind.value}.{op.value}' for op in operations)
    key=f'{identity}-key';application=f'eios:application:{identity}-core';scope=f'{kind.value}.{operation.value}'
    digest=lambda value:F.canonical_authority_digest(value)
    base=dict(tenant_id=tenant,repository_witness=WITNESS)
    role_digest=digest({'role':'artifact-reader','operations':[op.value for op in operations]})
    version_digest=digest({'application':application,'version':'1','resource':resource_id,'operations':[op.value for op in operations],'world':world})
    binding=F.CredentialAuthenticationBinding(tenant_id=tenant,credential_tenant_id=tenant,credential_id=key,
        subject_id=subject,subject_kind=SubjectKind.SERVICE,subject_principal_id=principal,
        subject_revision=1,membership_revision=1,credential_revision=1,credential_epoch=1,
        caller_application_id=application,caller_application_version='1',caller_application_digest=version_digest,
        requested_scopes=scopes,credential_kind='api_key')
    ref=ResourceReference(tenant_id=tenant,resource_id=resource_id,resource_type=kind,security_revision=1)
    condition=Eq(op='eq',node_id='world-boundary',left=ActionParameterRef(kind='action_parameter',parameter_name='world'),right=LiteralValue(kind='literal',value=world))
    canonical=canonicalize_policy_expression(condition)
    rule=F.PolicyRuleFact(binding_id='world-policy',binding_digest=digest({'world':world}),resource=ref,operation=operation,
        policy_set_id='artifact-world',active_version=1,version_digest=canonical.sha256,
        canonical_ast_utf8=canonical.utf8,canonical_ast_sha256=canonical.sha256,effect=PolicyEffect.REQUIRE,
        condition=condition,activation_head_revision=1,activation_head_digest=canonical.sha256,
        registry_revision=1,registry_digest=canonical.sha256,activation_witness=canonical.sha256,published_at=past)
    clearance=F.SubjectClearanceFact(clearance_id='service-clearance',subject_kind=GrantSubjectKind.SERVICE,
        principal_id=principal,kind=F.ControlKind.ORGANIZATION,namespace='nexloop',value='internal',rank=0,
        dominates=frozenset(),revision=1,digest=digest({'clearance':'internal'}),valid_until=None)
    requirement=F.ResourceControlRequirementFact(requirement_id='internal-resource',resource=ref,operation=operation,
        kind=F.ControlKind.ORGANIZATION,namespace='nexloop',value='internal',minimum_rank=0,
        dominance=F.ControlDominance.EXACT,revision=1,digest=digest({'requirement':'internal'}),valid_until=None)
    grant=F.EffectiveGrantFact(grant_id='artifact-grant',subject_kind=GrantSubjectKind.SERVICE,subject_id=principal,
        role_id='artifact-role',role_revision=1,role_digest=role_digest,resource=ref,operations=frozenset(operations),
        inherited=False,valid_from=past,valid_until=None,revision=1,digest=digest({'grant':resource_id,'principal':principal}))
    records=[
      ('subject',[subject],F.SubjectFacts(**base,subject_id=subject,kind=SubjectKind.SERVICE,status='active',revision=1)),
      ('membership',[subject,principal],F.MembershipFacts(**base,subject_id=subject,principal_id=principal,kind=MembershipKind.HOME,status='active',valid_from=past,valid_until=None,revision=1)),
      ('actor',[principal],F.ActorFacts(**base,actor_id=subject,actor_principal_id=principal,kind=SubjectKind.SERVICE,status='active',revision=1)),
      ('authentication',[key],F.CredentialAuthenticationFacts(**binding.model_dump(),status='active',expires_at=expiry,repository_witness=WITNESS)),
      ('application',[application,'1'],F.ApplicationFacts(**base,application_id=application,application_revision=1,version='1',version_revision=1,version_digest=version_digest,record_digest=version_digest,
        application_status='active',version_status='published',eligible=True,mode=ApplicationMode.RESTRICTED,break_glass=False,
        resources=(ResourceRestriction(tenant_id=tenant,resource_type=kind.value,resource_id=resource_id),),
        operations=tuple(OperationRestriction(operation=op) for op in operations),published_at=past,revoked_at=None,expires_at=None)),
      ('subject_authority',[principal],F.SubjectAuthorityFacts(**base,subject_kind=GrantSubjectKind.SERVICE,principal_id=principal,groups=(),
        roles=(F.RevisionedAuthority(authority_id='artifact-role',revision=1,digest=role_digest),),attribute_revisions=(),clearances=(),revision=1,valid_until=None)),
      ('resource_graph',[resource_id],F.ResourceGraphFacts(**base,root=F.ResourceNodeFact(resource=ref,parent=None,active=True),ancestors=(),dependencies=(),registry_revision=1,registry_digest=digest(ref),closure_complete=True,next_cursor=None)),
      ('grants',[principal,resource_id],F.GrantFacts(**base,subject_kind=GrantSubjectKind.SERVICE,principal_id=principal,grants=(grant,),valid_until=None,complete=True,next_cursor=None,revision=1)),
      ('scope',[principal,resource_id,operation.value],F.ScopeAuthorityFacts(**base,principal_id=principal,binding_id='artifact-route',catalog_id='nexloop-scopes',catalog_version='1',catalog_revision=1,catalog_digest=digest({'scope':scope}),catalog_scopes=scopes,required_scopes=frozenset({scope}),
        authorities=(F.ScopeAuthority(scope=scope,resource_type=kind,operation=operation,resource_id=resource_id),),risk=F.AuthorizationRisk.READ_ONLY if kind is ResourceType.ARTIFACT else F.AuthorizationRisk.SENSITIVE,authorized_scopes=scopes,revision=1,valid_until=None)),
      ('controls',[principal,resource_id,operation.value],F.ControlFacts(**base,subject_clearances=(clearance,),resource_requirements=(requirement,),complete=True,valid_until=None,revision=1)),
      ('policies',[principal,resource_id,operation.value],F.PolicyFacts(**base,rules=(rule,),complete=True,valid_until=None,revision=1)),
      ('revision',['catalog'],F.RevisionSourceFacts(**base,schema_revision=1,schema_digest=digest({'schema':'nexloop-authority-v1'}),event_high_water=1,revision=1)),
    ]
    return binding,expiry,records



def publish_test_artifact_identity(connection):
    """Trusted fresh sandbox bootstrap only; never widen a real service identity."""
    role=connection.execute('select current_user,session_user').fetchone()
    if role!=('nexloop_bootstrap','nexloop_bootstrap'):
        raise PermissionError('fresh sandbox bootstrap identity required')
    for table in ('control.nexloop_tenants','authz.nexloop_service_credentials',
                  'runtime.nexloop_local_artifacts','ontology.objects'):
        if connection.execute('select count(*) from '+table).fetchone()[0]:
            raise PermissionError('sandbox profile requires empty configuration/business state')
    tenant='nexloop-sandbox';world='test';resource='eios:artifact:local_test'
    operations=(Operation.CREATE,Operation.READ,Operation.DELETE);all_records={}
    for operation in operations:
        binding,expiry,rows=_artifact_test_records(tenant,resource,world,operation,operations)
        for kind,key,fact in rows:all_records[(kind,tuple(key))]=(kind,key,fact)
    token=secrets.token_urlsafe(48)
    with connection.transaction():
        connection.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active')",(tenant,))
        connection.execute('insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at) values(%s,%s,%s,%s,%s,%s,%s,%s)',
            (hashlib.sha256(token.encode()).hexdigest(),tenant,binding.credential_id,
             Jsonb(binding.model_dump(mode='json')),[world],'nexloop-core','active',expiry))
        for kind,key,fact in all_records.values():
            connection.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',
                (tenant,kind,key,Jsonb(fact.model_dump(mode='json'))))
    return token
