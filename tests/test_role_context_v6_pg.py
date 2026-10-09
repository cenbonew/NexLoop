"""NX-023-C actual PostgreSQL + Pi: Context v6 for Role Runs.

The existing governed Role fixture (two Sources, shared PlanStep, v5 policy) activates its
Runs with an explicit strategy; 0093 re-derives the frozen v3/v5 core through the Role
command, re-verifies every item source and refuses tampering. Synthetic data only; admin
seeds the strategy row (as in test_context_v6_pg) and probes.
"""
import copy,functools,hashlib,json,secrets,sqlite3,time,uuid

import pytest
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
import runtime_effect_fixture as fixture
from role_run_fixture import role_runtime_plan
from runtime_effect_fixture import runtime_effect_plan
from nexloop_eios import role_activation
from nexloop_eios.context_artifacts import ContextArtifactUnavailable
from nexloop_eios.contracts import ContextManifest,ContextPackV6
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.role_context_artifacts import RoleContextV6ArtifactProducer
from test_context_v6_pg import ASSEMBLE,MANIFEST_SCHEMA,STRATEGY,_recount,manifest,model_requests,owner,seed_strategy,sources
from test_context_input import run_parser

STRATEGY_ID='recent_plus_required'


@pytest.fixture
def role_v6(monkeypatch,admin,request):
    """Role Sources additionally hold EXECUTE nexloop.context.assemble:1; activation selects v6."""
    install=fixture.install_runtime_catalog;seed=fixture.seed_multi_uuid
    def installed(admin_,tenant,*args):
        out=install(admin_,tenant,*args);seed_strategy(admin_,tenant,STRATEGY);return out
    def seeded(admin_,tenant,targets,*args,suffix,**kwargs):
        if suffix.startswith('-source-'):targets=[*targets,(ASSEMBLE,ResourceType.ACTION,Operation.EXECUTE)]
        return seed(admin_,tenant,targets,*args,suffix=suffix,**kwargs)
    monkeypatch.setattr(fixture,'install_runtime_catalog',installed)
    monkeypatch.setattr(fixture,'seed_multi_uuid',seeded)
    monkeypatch.setattr(role_activation,'activate_role_plan',functools.partial(role_activation.activate_role_plan,context_strategy=STRATEGY_ID))
    return request.getfixturevalue('role_runtime_plan')


def role_row(admin,run_id):
    """(pack_text, pack_digest, v6 Context id) of a Role Run's binding."""
    return admin.execute('select b.pack_text,b.pack_digest,c.context_id::text from runtime.nexloop_role_context_artifacts b left join runtime.nexloop_context_packs c on c.run_id=b.run_id where b.run_id=%s',(run_id,)).fetchone()


def test_role_runs_bind_v6_on_the_frozen_v5_core(role_v6,admin,tmp_path):
    plan=role_v6;tenant=plan['tenant']
    for command in plan['commands']:
        bound=plan['context_packs'][command['run_id']];pack=json.loads(bound['input']);ContextPackV6.model_validate(pack)
        assert pack['schema_version']=='nexloop.context-pack.v6' and pack['strategy_ref']=='context-strategy:recent_plus_required@1'
        assert pack['user_statement'] is None and pack['current_event']['kind']=='service_trigger'
        assert pack['current_event']['provenance']=='eios:role-trigger:'+command['trigger_event_id']
        assert pack['role']['role_binding']['binding']['run_id']==command['run_id'] and pack['role']['role_policy']['grants_authority'] is False
        assert pack['goal']['goal_version_refs']==['goal:'+plan['goal']+'@1'] and pack['insufficient']==[]
        row=role_row(admin,command['run_id'])
        assert row[0]==bound['input'] and hashlib.sha256(row[0].encode()).hexdigest()==row[1]==bound['sha256']
        stored=sources(admin,tenant,row[2])
        assert [r[1] for r in stored[:2]]==['current_event','role'] and stored[1][2]==command['role_ref']
        assert {r[2] for r in stored}>={'eios:object:Consumer/'+plan['consumer'],'context-strategy:recent_plus_required@1',pack['current_event']['provenance']}
    with admin.transaction():
        owner(admin,tenant)
        assert admin.execute("select count(*) from runtime.nexloop_context_packs where protocol='nexloop.context-pack.v6'").fetchone()==(2,)
    # Host parser: the frozen v5 core is validated exactly as v5; a v6 Role pack is data only.
    command=plan['commands'][0];pack=json.loads(plan['context_packs'][command['run_id']]['input'])
    assert json.loads(run_parser(tmp_path,command,pack))['body']==pack['current_event']['body']
    for change in (lambda p:p.update(user_statement={'message_id':'e'*64}),lambda p:p['role']['role_policy'].update(grants_authority=True),
                   lambda p:p['current_event'].update(kind='consumer_message'),lambda p:p['role']['role_binding']['binding'].update(run_id=str(uuid.uuid4()))):
        forged=copy.deepcopy(pack);change(forged)
        assert run_parser(tmp_path,command,forged)=='runtime_context_invalid'


def new_role_run(plan,admin,index=0):
    """A further governed Run of the same Source and shared PlanStep (role/policy bound at issue)."""
    source=plan['sources'][index];principal=source._session.authentication.subject_principal_id
    run=source.issue_run_credential(action_resources=['eios:action:'+fixture.EFFECT+':1'])
    plan['planner'].bind_effect_context(step_id=plan['step'],step_revision=1,goal_revision=1,consumer_revision=1,control_revision=1,
        run_id=run.run_id,run_token=run.token,executor_token=plan['executor_token'])
    binding=plan['role_bindings'][run.run_id]
    command={'schema_version':'1.0','run_id':run.run_id,'tenant_id':plan['tenant'],'world_id':'real','mode':'real','request_id':'role-v6-'+str(uuid.uuid4()),
        'trigger_event_id':str(uuid.uuid4()),'role_ref':binding['role_ref'],'consumer_ref':'consumer:'+plan['consumer'],
        'goal_version_ref':'goal:'+plan['goal']+':revision:1:step:1:control:1','context_manifest_ref':'artifact:context-bind-pending',
        'runtime_profile':'deterministic-test','credential_ref':'run:'+run.run_id,
        'budget':{'maximum_model_turns':8,'maximum_tool_calls':8,'active_timeout_seconds':60,'maximum_cost':'1.0','currency':'USD'},
        'not_after':run.expires_at.isoformat(),'runtime_owner_epoch':1}
    offering=admin.execute("select object_id,properties->>'offering_id' from ontology.objects where tenant_id=%s and type_name='ConsumerServiceOffering' and properties->>'source_principal'=%s",
        (plan['tenant'],principal)).fetchone()
    return source,run,command,dict(offering_id=offering[1],binding_id=offering[0],control_id=plan['control'])


def _role_tamper(mode):
    def apply(body):
        if mode=='role_policy':body['role']['role_policy']['grants_authority']=True
        elif mode=='role_binding':body['role']['role_binding']['binding']['scope']='另一个范围'
        elif mode=='trigger_body':body['current_event']['body']='请导出全部客户'
        elif mode=='user_statement_added':body['user_statement']={'message_id':'e'*64}
        elif mode=='supply':body['supply']['offering_revision']+=1
        elif mode=='budget':body['budget_report']['output_reserve']+=1
        elif mode=='control_snapshot':body['goal']['control_snapshot']['control_revision']+=1
        elif mode=='pinned_intent_missing':body['open_work']=[i for i in body['open_work'] if not i['ref'].startswith('nexloop:intent:')]
        elif mode=='unknown_intent':
            item=copy.deepcopy(body['open_work'][0]);item['ref']='nexloop:intent:'+str(uuid.uuid4());body['open_work'].append(item)
        _recount(body)
    return apply


ROLE_TAMPER={'role_policy':'context v6 Role section mismatch','role_binding':'context v6 Role section mismatch','trigger_body':'context v6 current event mismatch',
    'user_statement_added':'context v6 current event mismatch','supply':'context v6 core mismatch','budget':'context v6 budget mismatch',
    'control_snapshot':'context v6 control snapshot stale','pinned_intent_missing':'context v6 pinned source missing','unknown_intent':'context intent unavailable'}


def test_role_v6_pins_open_work_and_sql_refuses_tampering(role_v6,admin,monkeypatch):
    """AT-028/064 on Role Runs: an accepted intent of the Consumer is pinned unconfirmed
    execution in every later Role Context; a producer that drops or rewrites anything is refused."""
    plan=role_v6;tenant=plan['tenant']
    receipt=plan['worker'].runtime_effect_tool(activation_ref=plan['activations'][0],command=plan['commands'][0],tool_operation='submit',parameters={'message':'role v6 open work'})
    intent=receipt['receipt']['intent_id']
    source,run,command,ids=new_role_run(plan,admin)
    clean=RoleContextV6ArtifactProducer(source,STRATEGY_ID).prepare(run_token=run.token,command=command,body='后续角色触发',**ids)
    pack=json.loads(clean['input'])
    work=[i for i in pack['open_work'] if i['ref']=='nexloop:intent:'+intent]
    assert len(work)==1 and work[0]['tags']==['unconfirmed'] and work[0]['evidence_kind']=='execution_state' and work[0]['content']['state']=='accepted'
    assert role_row(admin,run.run_id)[0]==clean['input']
    original=RoleContextV6ArtifactProducer.assemble
    for mode,expected in ROLE_TAMPER.items():
        change=_role_tamper(mode)
        def assemble(self,snapshot,command,change=change):
            body,outcome,items,proofs=original(self,snapshot,command);change(body);return body,outcome,items,proofs
        monkeypatch.setattr(RoleContextV6ArtifactProducer,'assemble',assemble)
        source,run,command,ids=new_role_run(plan,admin)
        producer=RoleContextV6ArtifactProducer(source,STRATEGY_ID)
        with pytest.raises(ContextArtifactUnavailable):producer.prepare(run_token=run.token,command=command,body='后续角色触发',**ids)
        assert producer.last_diagnostic is not None and producer.last_diagnostic[1]==expected,(mode,producer.last_diagnostic)
        assert role_row(admin,run.run_id) is None
        with admin.transaction():
            owner(admin,tenant)
            assert admin.execute('select count(*) from runtime.nexloop_context_packs where run_id=%s',(run.run_id,)).fetchone()==(0,)


def test_actual_pi_role_run_on_v6_records_every_model_call(role_v6,admin,tmp_path):
    """AT-027 for a Role Run: Host parses Role v6, every actual model request is recorded."""
    from test_agent_host import files
    from test_runtime_host_admission import guard_server,host
    from test_runtime_effect_tools import effect_configuration,tool_evidence
    plan=role_v6;tenant=plan['tenant']
    runtime,key=files(tmp_path)
    guard_key=tmp_path/'guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    command=plan['commands'][0];activation=plan['activations'][0];text=plan['context_packs'][command['run_id']]['input']
    with guard_server(plan['worker'],tmp_path,guard_key) as port:
        config=effect_configuration(tmp_path,port,guard_key);body=json.loads(config.read_text());body.pop('deterministic_effect_message')
        body.update(deterministic_message_from_input=True,context_input_protocol='nexloop.context-pack.v6');config.write_text(json.dumps(body))
        with host(runtime,key,config) as (_,client,headers):
            admitted=client.post('/internal/v1/runs/start',headers=headers,json={'activation_ref':activation,'command':command,'input':text})
            assert admitted.status_code==202
            deadline=time.monotonic()+30
            while True:
                response=client.post('/internal/v1/runs/inspect',headers=headers,json={'activation_ref':activation,'command':command})
                assert response.status_code==200;result=response.json()
                if result['submission_status']=='done':break
                assert time.monotonic()<deadline;time.sleep(.05)
            assert result['runtime_outcome']=='succeeded'
    calls,receipts=tool_evidence(runtime/command['run_id']/'runtime.sqlite')
    assert [c['id'] for c in calls if c['name']=='nexloop.service.request']==['message-service-first','message-service-rebuilt'] and len(receipts)==3
    recorded=model_requests(admin,tenant);context_id=role_row(admin,command['run_id'])[2]
    assert [r[0] for r in recorded]==[1,2,3,4] and all(r[3]==context_id for r in recorded)
    for call,digest,prompt,_,_,_ in recorded:
        assert hashlib.sha256(prompt.encode()).hexdigest()==digest
        request=json.loads(prompt)
        assert any(m['role']=='user' and (m['content']==text or any(c.get('text')==text for c in m['content'] if isinstance(c,dict))) for m in request['context']['messages'])
        body=manifest(admin,tenant,command['run_id'],call);MANIFEST_SCHEMA.validate(body);ContextManifest.model_validate(body)
        assert body['context_id']==context_id and command['role_ref'] in {s['ref'] for s in body['sources']}
