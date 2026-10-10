"""Synthetic NX-022 authority configuration; goal/control writes use governed ports only.

Human, Agent and service identities are converted from ordinary multi-resource
authority facts the same way the browser/agent fixtures do it, so the actual
EIOS decision chain (grants, scopes, controls, policies) stays in force.
"""
from datetime import UTC,datetime,timedelta
import hashlib,json
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.identity.models import SubjectKind
from nexloop_eios.authorization import WITNESS
from multi_authority_fixture import seed_multi_authority

TENANT='synthetic-a'
ACTIONS={'Metric.approve':'goals.metric.approve','Goal.publish':'goals.version.publish','Goal.propose':'goals.agent.propose',
    'Control.set':'goals.control.set','Budget.set':'goals.budget.set','Contact.release':'goals.contact.release'}
MODELS={'subject':F.SubjectFacts,'membership':F.MembershipFacts,'actor':F.ActorFacts,'authentication':F.CredentialAuthenticationFacts,
    'application':F.ApplicationFacts,'subject_authority':F.SubjectAuthorityFacts,'grants':F.GrantFacts,'scope':F.ScopeAuthorityFacts,
    'controls':F.ControlFacts,'policies':F.PolicyFacts,'resource_graph':F.ResourceGraphFacts,'revision':F.RevisionSourceFacts}


def action_targets(*names):
    return [(f'eios:action:{name}:1',ResourceType.ACTION,Operation.EXECUTE) for name in names]


def register_goal_actions(admin,base,capability):
    for name,cap in ACTIONS.items():
        binding=base.capability_binding.model_copy(update={'capability_name':cap})
        definition=base.model_copy(update={'stable_name':name,'capability_binding':binding,'contract_digest':None})
        admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
            (TENANT,'real',f'eios:action:{name}:1',Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_copy(update={'capability_name':cap}).model_dump(mode='json'))))


def _owned_rows(admin,auth):
    owned={auth.subject_id,auth.subject_principal_id,auth.credential_id}
    for kind,key,payload in admin.execute('select fact_kind,entity_key,payload from authz.nexloop_authority_facts where tenant_id=%s',(TENANT,)).fetchall():
        if owned & set(key) or (kind=='application' and key[0]==auth.caller_application_id):
            yield kind,key,payload


def _rewrite(admin,kind,key,payload,convert):
    body=convert(payload);body.pop('snapshot_digest',None)
    fact=MODELS[kind].model_validate_json(json.dumps(body))
    new_key=convert(key)
    if new_key!=key:admin.execute('delete from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s',(TENANT,kind,key))
    admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',
        (TENANT,kind,new_key,Jsonb(fact.model_dump(mode='json'))))
    return fact


def _converter(mapping):
    def convert(value):
        if isinstance(value,dict):return {k:convert(v) for k,v in value.items()}
        if isinstance(value,list):return [convert(v) for v in value]
        return mapping.get(value,value) if isinstance(value,str) else value
    return convert


def seed_service(admin,pool,names,*,suffix):
    """Returns the token; authenticate after all seeding (seeding moves the directory hash)."""
    return seed_multi_authority(admin,pool,action_targets(*names),identity_suffix=suffix)[1]


def seed_agent_author(admin,pool,names,*,suffix='-goal-agent'):
    """Actual Agent invocation credential with current EXECUTE grants on `names`; returns its token."""
    session,token=seed_multi_authority(admin,pool,action_targets(*names),identity_suffix=suffix)
    auth=session.authentication;agent='eios:agent:goal-proposer'+suffix;release='goal-agent-release'+suffix
    convert=_converter({auth.subject_id:agent,'service':'agent'})
    rows=list(_owned_rows(admin,auth))
    app=None
    with admin.transaction():
        for kind,key,payload in rows:
            fact=_rewrite(admin,kind,key,payload,convert)
            if kind=='application':app=fact
        binding=auth.model_copy(update={'subject_id':agent,'subject_kind':SubjectKind.AGENT})
        invocation=F.AgentInvocationBinding(tenant_id=TENANT,actor_principal_id=auth.subject_principal_id,agent_id=agent,agent_revision=1,
            release_id=release,release_revision=1,release_digest='a'*64,agent_application_id=app.application_id,
            agent_application_version=app.version,agent_application_digest=app.version_digest)
        admin.execute('update authz.nexloop_service_credentials set binding=%s,agent_invocation=%s where token_digest=%s',
            (Jsonb(binding.model_dump(mode='json')),Jsonb(invocation.model_dump(mode='json')),hashlib.sha256(token.encode()).hexdigest()))
        for kind,key,fact in [('agent',[agent],F.AgentFacts(tenant_id=TENANT,repository_witness=WITNESS,agent_id=agent,actor_principal_id=auth.subject_principal_id,application_id=app.application_id,status='active',revision=1)),
            ('agent_release',[release],F.AgentReleaseFacts(tenant_id=TENANT,repository_witness=WITNESS,release_id=release,agent_id=agent,application_id=app.application_id,application_version=app.version,application_version_digest=app.version_digest,release_digest=invocation.release_digest,status='active',eligible=True,revision=1,released_at=datetime.now(UTC)-timedelta(hours=1),revoked_at=None,parent_release_id=None,parent_chain=(),chain_complete=True)),
            ('agent_application',[app.application_id,app.version],app)]:
            admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',(TENANT,kind,key,Jsonb(fact.model_dump(mode='json'))))
    return token


def seed_human_owner(admin,pool,names,identity,uow):
    """Actual password/browser HUMAN session with current EXECUTE grants on `names`; returns the BrowserSession."""
    from test_browser_session_creation import create
    from test_browser_evidence import authenticate
    session,_=seed_multi_authority(admin,pool,action_targets(*names),identity_suffix='-goal-owner')
    auth=session.authentication;subject=identity[1].subject_id;principal=identity[2].principal_id
    convert=_converter({auth.subject_id:subject,auth.subject_principal_id:principal,'service':'human'})
    with admin.transaction():
        for kind,key,payload in list(_owned_rows(admin,auth)):
            if kind in ('subject','membership','authentication'):
                admin.execute('delete from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s',(TENANT,kind,key))
                continue
            _rewrite(admin,kind,key,payload,convert)
        admin.execute('delete from authz.nexloop_service_credentials where credential_id=%s',(auth.credential_id,))
        # Another browser fixture (e.g. conversations) may already map this browser app: the owner's facts take over.
        admin.execute('insert into control.nexloop_browser_business_applications values(%s,%s,%s,%s,%s) on conflict(tenant_id,application_id) do update '
            'set caller_application_id=excluded.caller_application_id,application_version=excluded.application_version,requested_scopes=excluded.requested_scopes',
            (TENANT,uow.application_id,auth.caller_application_id,'1',Jsonb(['action.execute'])))
    return create(uow,identity,authenticate(uow,identity).evidence).session


def authenticate_human(pool,browser_session):
    from nexloop_eios.browser_authorization import authenticate_browser_business
    return authenticate_browser_business(pool,browser_session,world='real')
