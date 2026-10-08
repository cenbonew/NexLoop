"""Typed synthetic Agent configuration only; business writes use governed ports."""
from datetime import UTC,datetime,timedelta
import hashlib,json
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from authority_fixture import seed_authority
from nexloop_eios.authorization import WITNESS


def seed_agent(admin,tenant='synthetic-a'):
    resource='eios:action:Consumer.create:1'
    token,rows=seed_authority(admin,tenant,resource,operation=Operation.EXECUTE,resource_type=ResourceType.ACTION,identity_suffix='-agent')
    agent='eios:agent:synthetic-writer';release='synthetic-agent-release-1'
    binding=admin.execute('select binding from authz.nexloop_service_credentials where token_digest=%s',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()[0]
    old_subject=binding['subject_id'];binding['subject_id']=agent;binding['subject_kind']='agent'
    binding=F.CredentialAuthenticationBinding.model_validate_json(json.dumps(binding))
    app=next(fact for kind,key,fact in rows if kind=='application')
    invocation=F.AgentInvocationBinding(tenant_id=tenant,actor_principal_id=binding.subject_principal_id,
        agent_id=agent,agent_revision=1,release_id=release,release_revision=1,release_digest='a'*64,
        agent_application_id=app.application_id,agent_application_version=app.version,agent_application_digest=app.version_digest)
    def convert(value):
        if isinstance(value,dict):return {key:convert(item) for key,item in value.items() if key!='snapshot_digest'}
        if isinstance(value,list):return [convert(item) for item in value]
        if value=='service':return 'agent'
        if value==old_subject:return agent
        return value
    with admin.transaction():
        admin.execute('update authz.nexloop_service_credentials set binding=%s,agent_invocation=%s where token_digest=%s',(Jsonb(binding.model_dump(mode='json')),Jsonb(invocation.model_dump(mode='json')),hashlib.sha256(token.encode()).hexdigest()))
        for kind,key,fact in rows:
            new_key=[agent if item==old_subject else item for item in key]
            value=type(fact).model_validate_json(json.dumps(convert(fact.model_dump(mode='json'))))
            if new_key!=key:admin.execute('delete from authz.nexloop_authority_facts where tenant_id=%s and fact_kind=%s and entity_key=%s',(tenant,kind,key))
            admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s) on conflict(tenant_id,fact_kind,entity_key) do update set payload=excluded.payload',(tenant,kind,new_key,Jsonb(value.model_dump(mode='json'))))
        extra=[('agent',[agent],F.AgentFacts(tenant_id=tenant,repository_witness=WITNESS,agent_id=agent,actor_principal_id=binding.subject_principal_id,application_id=app.application_id,status='active',revision=1)),
            ('agent_release',[release],F.AgentReleaseFacts(tenant_id=tenant,repository_witness=WITNESS,release_id=release,agent_id=agent,application_id=app.application_id,application_version=app.version,application_version_digest=app.version_digest,release_digest=invocation.release_digest,status='active',eligible=True,revision=1,released_at=datetime.now(UTC)-timedelta(hours=1),revoked_at=None,parent_release_id=None,parent_chain=(),chain_complete=True)),
            ('agent_application',[app.application_id,app.version],app)]
        for kind,key,fact in extra:admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',(tenant,kind,key,Jsonb(fact.model_dump(mode='json'))))
    return token,invocation


def seed_parent(admin,invocation,*,allow=True):
    from authority_fixture import replace_fact
    row=admin.execute("select payload from authz.nexloop_authority_facts where tenant_id='synthetic-a' and fact_kind='agent_application' and entity_key=%s",([invocation.agent_application_id,invocation.agent_application_version],)).fetchone()[0]
    app={k:v for k,v in row.items() if k not in ('repository_witness','snapshot_digest')}
    app.update(application_id='eios:application:synthetic-parent',version_digest='b'*64,record_digest='b'*64)
    if not allow:app.update(resources=[],operations=[])
    ceiling=F.ParentApplicationAuthorityFact.model_validate_json(json.dumps(app))
    parent_app=F.ApplicationFacts.model_validate_json(json.dumps({**app,'repository_witness':WITNESS}))
    parent_agent=F.AgentFacts(tenant_id='synthetic-a',repository_witness=WITNESS,agent_id='eios:agent:synthetic-parent',actor_principal_id='synthetic-parent-principal',application_id=ceiling.application_id,status='active',revision=1)
    parent_release=F.AgentReleaseFacts(tenant_id='synthetic-a',repository_witness=WITNESS,release_id='synthetic-parent-release',parent_release_id=None,agent_id=parent_agent.agent_id,application_id=ceiling.application_id,application_version=ceiling.version,application_version_digest=ceiling.version_digest,release_digest='c'*64,status='active',eligible=True,revision=1,released_at=datetime.now(UTC)-timedelta(hours=2),revoked_at=None,parent_chain=(),chain_complete=True)
    body=parent_release.model_dump(mode='json')
    for key in ('repository_witness','snapshot_digest','parent_chain','chain_complete'):body.pop(key,None)
    body.update(agent_revision=parent_agent.revision,agent_status=parent_agent.status,application=ceiling.model_dump(mode='json'))
    frame=F.ParentAgentReleaseFact.model_validate_json(json.dumps(body))
    for kind,key,fact in [('agent',[parent_agent.agent_id],parent_agent),('agent_release',[parent_release.release_id],parent_release),('agent_application',[parent_app.application_id,parent_app.version],parent_app)]:
        admin.execute('insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) values(%s,%s,%s,%s)',('synthetic-a',kind,key,Jsonb(fact.model_dump(mode='json'))))
    replace_fact(admin,'synthetic-a','agent_release',[invocation.release_id],F.AgentReleaseFacts,parent_release_id=frame.release_id,parent_chain=[frame.model_dump(mode='json')])
    return frame
