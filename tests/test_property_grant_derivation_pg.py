"""Governed per-object property READ/EDIT derivation (0084) on clean catalog PostgreSQL.

docs/implementation/property-grant-derivation.md. A real browser Human approves a
new Consumer property (NX-044); the claim_matcher-like service holds the type-level
READ and the Consumer.edit Action but no per-object Consumer EDIT and no authority on
the new property at all. A property_access_rule plus the owner's
property_group_restriction let SQL derive the missing READ/EDIT at use. Rule and
restriction facts are written here as test fixtures; the trusted-configuration path
that writes them in deployments is covered in test_service_grants_pg. Synthetic data.
"""
from datetime import UTC,datetime,timedelta
import copy

import psycopg
import pytest
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.grants import GrantSubjectKind
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.authorization import WITNESS,authenticate_service
from nexloop_eios.browser_authorization import authenticate_browser_business
from nexloop_eios.candidate_merge import CandidateGluer
from nexloop_eios.claim_matching import ClaimMatcher,MatchConfiguration,ScriptedMatchProvider
from nexloop_eios.object_reads import AuthorizedObjectReader
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from nexloop_eios.property_access import PropertyAccessRule,PropertyGroupRestriction,property_access_basis
from nexloop_eios.recall import EiosRecallAuthorizer,OntologyRecall
from nexloop_eios.review_actions import ReviewReflowWorker
from multi_authority_fixture import seed_multi_authority
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_browser_session_reads import authenticate,create
from test_candidate_merge_pg import candidates
from test_claim_matching_pg import env,matcher_targets,obj,resolution  # noqa: F401
from test_review_decisions_pg import human,pending,review  # noqa: F401

TENANT='synthetic-a'
pytestmark=pytest.mark.parametrize('env',[TENANT],indirect=True)
GROUPS=('channel_tech','lifestyle','needs_intent','pain_point','preference','purchase_behavior','sentiment_attitude')
READ,EDIT=Operation.READ,Operation.EDIT
DENIED=(ActionAuthorizationDenied,F.AuthorizationFactDenied,AuthorizationUnavailable)


def put(admin,kind,key,payload):
    admin.execute('''insert into authz.nexloop_authority_facts values(%s,%s,%s,%s)
        on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload''',(TENANT,kind,key,Jsonb(payload.model_dump(mode='json'))))


def rule(admin,principal,**changes):
    value=dict(tenant_id=TENANT,principal_id=principal,type_name='Consumer',operations=('edit','read'),property_groups=GROUPS,
        include_review_published=True,basis_schema_version=1,active=True,valid_until=datetime.now(UTC)+timedelta(days=1))|changes
    put(admin,'property_access_rule',[principal,'Consumer'],PropertyAccessRule(**value))


def restrict(admin,groups=()):
    """Owner decision default is no restricted group; tests pass synthetic restrictions explicitly."""
    put(admin,'property_group_restriction',['Consumer'],PropertyGroupRestriction(tenant_id=TENANT,type_name='Consumer',
        restricted_groups=tuple(groups),decision='synthetic owner decision'))


def published(f,label='pd'):
    claim,cid,_,_=pending(f,label,'常用付款方式','花呗','我一般用花呗付款','payment_method')
    result=human(f).decide(candidate_id=cid,decision='approve',expected_revision=candidates(f['admin'])[cid][8],rationale='新增付款方式属性',
        idempotency_key=f'synthetic-derive-{label}-approve')
    assert result['outcome']=='published',result['publication']
    return claim,result


def service(f,suffix='-derived'):
    """Matcher authority without the Consumer object, per-object Consumer EDIT or budget_level, or anything on the new property."""
    prefix='eios:property:Consumer/'+f['consumer']+'/'
    targets=[t for t in matcher_targets(f) if t[0] not in ('eios:object:Consumer/'+f['consumer'],prefix+'budget_level')
        and not (t[2] is EDIT and t[0].startswith(prefix))]
    targets.append(('eios:action:Consumer.edit:2',ResourceType.ACTION,Operation.EXECUTE))
    session,f['token']=seed_multi_authority(f['admin'],f['worker'],targets,identity_suffix=suffix,tenant=f['tenant'])
    return session


def current(f):
    """Every authority-fact change advances the tenant authority revision: services re-authenticate."""
    return authenticate_service(f['worker'],f['token'],world='real')


def reflow(f,session):
    recall=OntologyRecall(f['worker'],session,authorizer=EiosRecallAuthorizer(f['worker'],session),provider=f['provider'],expected_dimension=64)
    m=ClaimMatcher(f['worker'],session,f['signer'],recall=recall,provider=ScriptedMatchProvider({}),
        configuration=MatchConfiguration(edit_actions={'Consumer':('Consumer.edit',2)},create_actions={'Product':('Product.create',1)}))
    gluer=CandidateGluer(f['worker'],session,f['signer'],indexer=f['indexer'],matcher=m,provider=f['provider'])
    return ReviewReflowWorker(f['worker'],session,f['signer'],gluer=gluer)


def resource(f,prop='payment_method'):return f"eios:property:Consumer/{f['consumer']}/{prop}"


def proof(f,session,prop='payment_method',op=EDIT):
    return AuthorizedObjectReader(f['worker'],session,f['signer'])._authority(ResourceType.PROPERTY,f"Consumer/{f['consumer']}/{prop}",op)


def basis(f,session,prop='payment_method',op='edit'):return property_access_basis(f['worker'],session,resource(f,prop),op)


def assert_edit(admin,session,claims):
    return admin.execute('select authz.nexloop_assert_edit_authority(%s,%s,%s)',(session.token_digest,'real',Jsonb(claims))).fetchone()[0]


def refreshed(claims,session):
    """The same derived basis re-bound to a re-authenticated session (isolates the basis comparison)."""
    return copy.deepcopy(claims)|{'directory_hash':session.directory_hash}


def fingerprint(admin):return admin.execute('select authz.nexloop_tenant_authority_fingerprint(%s)',(TENANT,)).fetchone()[0]


def test_reviewed_property_applies_by_type_derivation_without_object_grants(review):
    """Positive: after the NX-044 approve, reflow applies the Claim with no per-object grant on the new property."""
    f=review;admin=f['admin'];claim,result=published(f,'p1')
    session=service(f);principal=session.authentication.subject_principal_id
    # Without a rule nothing is derived and the reflow keeps waiting.
    assert basis(f,session)=={'mode':'configured'}
    with pytest.raises(DENIED):proof(f,session)
    rule(admin,principal);restrict(admin);session=current(f)
    epoch=fingerprint(admin)
    derived=basis(f,session)
    assert derived['mode']=='derived' and derived['type_name']=='Consumer' and derived['schema_version']==2
    assert derived['property_group']=='purchase_behavior' and derived['review_decision_id']==result['decision_id']
    # Object EDIT is derived from the rule alone; the definition-level READ of the new property too.
    assert all(property_access_basis(f['worker'],session,'eios:object:Consumer/'+f['consumer'],op)['mode']=='derived' for op in ('read','edit'))
    # Configured per-object READ of an existing property wins over derivation (even for EDIT, which it lacks).
    assert basis(f,session,'favorite_sport',op='edit')=={'mode':'configured'}
    assert property_access_basis(f['worker'],session,'eios:property:Consumer/payment_method','read')['mode']=='derived'
    assert property_access_basis(f['worker'],session,'eios:property:Consumer/payment_method','edit')=={'mode':'configured'}
    readable=EiosRecallAuthorizer(f['worker'],session).readable(ResourceType.PROPERTY,['Consumer/payment_method',f"Consumer/{f['consumer']}/payment_method"])
    assert readable==frozenset({'Consumer/payment_method',f"Consumer/{f['consumer']}/payment_method"})
    report=reflow(f,session).run_pending()
    assert report[result['decision_id']]['status']=='done' and report[result['decision_id']]['applied']==1,report
    assert obj(admin,f['consumer'])[0]['payment_method']=='花呗' and resolution(admin,claim)=='resolved'
    # Nothing per object was written; the new property/object did not move the authority epoch.
    assert admin.execute("select count(*) from authz.nexloop_authority_facts where entity_key[1]=%s and array_to_string(entity_key,'|') like '%%payment_method%%'",
        (principal,)).fetchone()==(0,)
    assert fingerprint(admin)==epoch and basis(f,session)['mode']=='derived'


@pytest.mark.parametrize('case',['no_rule','operation_not_in_rule','group_not_in_rule','restricted_group_listed','include_false',
    'inactive','expired','no_owner_restriction','explicit_empty_grant'])
def test_property_access_is_not_derived(review,case):
    f=review;admin=f['admin'];published(f,'n'+str(len(case)))
    session=service(f);principal=session.authentication.subject_principal_id
    changes={'operation_not_in_rule':{'operations':('read',)},'group_not_in_rule':{'property_groups':('needs_intent',)},
        'include_false':{'include_review_published':False},'inactive':{'active':False},
        'expired':{'valid_until':datetime.now(UTC)-timedelta(seconds=1)}}.get(case,{})
    if case!='no_rule':rule(admin,principal,**changes)
    if case=='restricted_group_listed':restrict(admin,('purchase_behavior',))
    elif case!='no_owner_restriction':restrict(admin)
    if case=='explicit_empty_grant':
        put(admin,'grants',[principal,resource(f)],F.GrantFacts(tenant_id=TENANT,repository_witness=WITNESS,subject_kind=GrantSubjectKind.SERVICE,
            principal_id=principal,grants=(),valid_until=None,complete=True,next_cursor=None,revision=1))
    session=current(f)
    assert basis(f,session)=={'mode':'configured'}
    with pytest.raises(DENIED):proof(f,session)
    if case=='operation_not_in_rule':assert basis(f,session,op='read')['mode']=='derived'


def test_restricted_group_is_never_derived_even_when_the_rule_lists_it(review):
    """budget_level is in the rule's basis version and its group is listed; the owner restriction alone withholds it."""
    f=review;admin=f['admin'];published(f,'rg')
    session=service(f);principal=session.authentication.subject_principal_id
    rule(admin,principal,property_groups=tuple(sorted(GROUPS+('demographics','spending_power'))));restrict(admin,('spending_power',));session=current(f)
    assert basis(f,session,'budget_level')=={'mode':'configured'}
    with pytest.raises(DENIED):proof(f,session,'budget_level')
    # The same rule derives it once the owner no longer restricts the group (proves the restriction is what blocks it).
    restrict(admin);session=current(f)
    assert basis(f,session,'budget_level')['mode']=='derived' and basis(f,session,'budget_level')['review_decision_id'] is None
    # A group no rule names stays underived whatever the restriction says (display_name has no group at all).
    assert basis(f,session,'display_name')=={'mode':'configured'}


def test_property_not_published_by_review_is_not_derived(review):
    """A property added outside a human review decision (fault-injected Schema v3) derives nothing."""
    f=review;admin=f['admin'];published(f,'nr')
    session=service(f);principal=session.authentication.subject_principal_id
    rule(admin,principal);restrict(admin);session=current(f)
    definition=copy.deepcopy(admin.execute("select definition from ontology.object_type_versions where tenant_id=%s and type_name='Consumer' and version=2",(TENANT,)).fetchone()[0])
    extra=copy.deepcopy(next(p for p in definition['properties'] if p['property_name']=='payment_method'));extra['property_name']='hidden_note'
    definition['properties'].append(extra);definition['version']=3
    for group in definition['property_groups']:
        if group['group_name']=='purchase_behavior':group['property_names'].append('hidden_note')
    admin.execute("insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,'Consumer',3,%s)",(TENANT,Jsonb(definition)))
    admin.execute('update ontology.objects set schema_version=3 where object_id=%s',(f['consumer'],))
    assert basis(f,session,'hidden_note')=={'mode':'configured'}
    assert basis(f,session)['mode']=='derived'  # the reviewed one still is


def test_scope_tenant_world_object_and_caller_kind(review):
    f=review;admin=f['admin'];published(f,'sc')
    session=service(f);principal=session.authentication.subject_principal_id
    rule(admin,principal);restrict(admin);session=current(f)
    def derive(tenant,world,res,op='edit'):
        with admin.transaction():  # forced RLS: the caller's tenant context, as the basis definer sets it
            admin.execute("select set_config('eios.tenant_id',%s,true)",(tenant,))
            return admin.execute('select authz.nexloop_property_access_derivation(%s,%s,%s,%s,%s)',(tenant,principal,world,res,op)).fetchone()[0]
    assert derive(TENANT,'real',resource(f))['mode']=='derived'
    assert derive(TENANT,'shadow',resource(f)) is None and derive(TENANT,'simulation',resource(f)) is None
    assert derive('synthetic-other','real',resource(f)) is None
    assert derive(TENANT,'real','eios:property:Consumer/'+'0'*64+'/payment_method') is None  # no such (or deleted) object
    assert derive(TENANT,'real',f"eios:property:Product/{f['product']}/name") is None  # other type: no rule
    assert derive(TENANT,'real',resource(f),'delete') is None and derive(TENANT,'real',resource(f),'execute') is None
    # A Human browser session never derives (service-only path).
    issued=create(f['uow'],f['identity'],authenticate(f['uow'],f['identity']).evidence)
    person=authenticate_browser_business(f['api'],issued.session,world='real')
    try:assert property_access_basis(f['api'],person,resource(f),'edit')=={'mode':'configured'}
    except psycopg.Error:pass
    # Agent principals cannot hold a rule at all: configure_manifest requires a service subject_authority (test_service_grants_pg).


def test_old_proof_is_rejected_after_rule_or_restriction_change(review):
    f=review;admin=f['admin'];published(f,'op')
    session=service(f);principal=session.authentication.subject_principal_id
    rule(admin,principal);restrict(admin);session=current(f)
    claims=proof(f,session)
    assert claims['derivation']=='type-property-v1' and claims['facts']==[] and claims['derivation_basis']['type_read']['resource_id']=='eios:object_type:Consumer'
    assert assert_edit(admin,session,claims)
    for tamper in ({'review_decision_id':None},{'schema_version':1},{'property_group':'preference'},{'type_read':None},
            {'rule_hash':'0'*64},{'restriction_hash':'0'*64}):
        forged=copy.deepcopy(claims);forged['derivation_basis'].update(tamper)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):assert_edit(admin,session,forged)
    forged=copy.deepcopy(claims);forged['operation']='read'
    with pytest.raises(psycopg.errors.InsufficientPrivilege):assert_edit(admin,session,forged)
    # Changing the rule or the owner restriction rejects the earlier proof, also for a re-authenticated caller.
    rule(admin,principal,property_groups=tuple(sorted(GROUPS+('other',))))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):assert_edit(admin,session,claims)
    session=current(f);claims=refreshed(claims,session)
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='changed'):assert_edit(admin,session,claims)
    rule(admin,principal);session=current(f);claims=proof(f,session);assert assert_edit(admin,session,claims)
    restrict(admin,('synthetic_restricted',));session=current(f)
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='changed'):assert_edit(admin,session,refreshed(claims,session))
    restrict(admin);session=current(f);claims=proof(f,session);assert assert_edit(admin,session,claims)
    # A configured grant appearing later supersedes the derived proof.
    put(admin,'grants',[principal,resource(f)],F.GrantFacts(tenant_id=TENANT,repository_witness=WITNESS,subject_kind=GrantSubjectKind.SERVICE,
        principal_id=principal,grants=(),valid_until=None,complete=True,next_cursor=None,revision=1))
    session=current(f)
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match='superseded'):assert_edit(admin,session,refreshed(claims,session))


def test_review_workbench_reports_derivation_impact_before_approve(review):
    """Approved design decision 6: before approve the reviewer sees who would automatically read/write, the group, and whether it is restricted."""
    f=review;admin=f['admin']
    _,cid,_,_=pending(f,'wi','常用付款方式','花呗','我一般用花呗付款','payment_method')
    session=service(f);principal=session.authentication.subject_principal_id
    assert human(f).derivation_impact(cid)=={'applies':True,'type_name':'Consumer','property_group':'purchase_behavior',
        'restriction_configured':False,'restricted':False,'auto_access':[]}
    rule(admin,principal);restrict(admin)
    assert human(f).derivation_impact(cid)=={'applies':True,'type_name':'Consumer','property_group':'purchase_behavior',
        'restriction_configured':True,'restricted':False,'auto_access':[{'principal_id':principal,'operations':['edit','read']}]}
    rule(admin,principal,include_review_published=False)
    assert human(f).derivation_impact(cid)['auto_access']==[]
    rule(admin,principal);restrict(admin,('purchase_behavior',))
    impact=human(f).derivation_impact(cid)
    assert impact['restricted'] is True and impact['auto_access']==[]
    # A service credential cannot read it (Human review read protocol only).
    from nexloop_eios.review_actions import ReviewDecisionPort
    with pytest.raises(PermissionError):ReviewDecisionPort(f['api'],current(f),f['signer'])


def test_erasing_consumer_refuses_derived_property_access(review):
    """NX-029 §4.2a: 0084 hands v0072 a type-level READ, so its own claims are checked on the function itself."""
    from erasure_support import ERASING, mark_erasing, withdraw
    f=review;admin=f['admin'];published(f,'er')
    session=service(f);principal=session.authentication.subject_principal_id
    rule(admin,principal);restrict(admin);session=current(f)
    claims=proof(f,session);assert assert_edit(admin,session,claims)
    direct=lambda op:admin.execute('select authz.nexloop_assert_derived_property_access(%s,%s,%s,%s)',(session.token_digest,'real',Jsonb(claims),op)).fetchone()
    mark_erasing(admin,f['tenant'],f['consumer'])
    with pytest.raises(psycopg.errors.InsufficientPrivilege,match=ERASING),admin.transaction():assert_edit(admin,session,claims)   # public edit entry
    for op in ('edit','read'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege,match=ERASING),admin.transaction():direct(op)
    withdraw(admin,f['tenant'],f['consumer']);assert assert_edit(admin,session,claims)
