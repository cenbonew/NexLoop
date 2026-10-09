"""NX-023-B actual PostgreSQL: Context v6 for message Runs (AT-027/028/064).

Human Message → relay with an explicit strategy → v6 pack (frozen v2 core + Engine
sections) bound by 0091, which re-derives the core, re-verifies every source against its
row and refuses Claims in formal state, hypotheses, omitted pinned sources and tampering;
then the guard records each actual model request (dense, digest recomputed in SQL).
Synthetic data only. Admin seeds strategy rows and NX-019 Claim rows (both are inputs of
other tasks with their own tests) and probes; it never authorizes anything.
"""
import copy,hashlib,json,secrets,uuid
from pathlib import Path

import psycopg
import pytest
from jsonschema import Draft202012Validator,FormatChecker
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.authorization import AuthorizationUnavailable
from nexloop_eios.context_artifacts import ACTION,ContextV6ArtifactProducer
from nexloop_eios.context_engine.strategy import load_builtins
from nexloop_eios.contracts import ContextManifest,ContextPackV6
from nexloop_eios.message_relay import MessageRelay,MessageRelayUnavailable
from nexloop_eios.postgres_artifacts import canonical_payload
from test_context_artifacts import context_message,source_declarations,reconfigure,active_worker
from local_message_assembly_fixture import assembled_message,business_plan,configured

ROOT=Path(__file__).resolve().parents[1]
STRATEGY=next(s for s in load_builtins(ROOT/'deploy/configuration/context-strategies.v1.json') if s['strategy_id']=='recent_plus_required')
MANIFEST_SCHEMA=Draft202012Validator(json.loads((ROOT/'packages/contracts/context-manifest.schema.json').read_text()),format_checker=FormatChecker())
ITEM_SECTIONS=('constraints','consumer_state','open_work','evidence','semantics','experience')
ASSEMBLE='eios:action:nexloop.context.assemble:1'


def owner(admin,tenant):
    admin.execute('set local role nexloop_owner');admin.execute("select set_config('eios.tenant_id',%s,true)",(tenant,))


def seed_strategy(admin,tenant,definition):
    with admin.transaction():
        owner(admin,tenant)
        admin.execute("insert into control.nexloop_context_strategies(tenant_id,world,strategy_id,version,definition,definition_digest,published_by,intent_id) "
            "values(%s,'real',%s,%s,%s,encode(sha256(convert_to(%s::jsonb::text,'UTF8')),'hex'),'synthetic-human-owner',%s)",
            (tenant,definition['strategy_id'],definition['version'],Jsonb(definition),json.dumps(definition),'seed-'+definition['strategy_id']+'-'+str(definition['version'])))


def seed_claim(admin,f,*,kind,polarity='affirmed',state='unresolved',quote='不是正式事实',derived=()):
    """NX-019 output row (statement or hypothesis) for the Run's conversation."""
    tenant=f['original']['tenant'];message=f['message']
    claim_id=hashlib.sha256(('claim:'+kind+polarity+state+quote+str(uuid.uuid4())).encode()).hexdigest()
    body=message['body'];start=max(0,body.find(quote));sourced=kind!='hypothesis'
    with admin.transaction():
        owner(admin,tenant)
        admin.execute('''insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,subject_text,
            predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,source_content_hash,quote,
            extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags) values
            (%s,'real',%s,%s,%s,%s,'t1','consumer',%s,'顾客',%s,%s,'consumer',%s,'asserted','','','{}',%s,%s,%s,%s,%s,%s,'nx019-extractor/2',0.8,%s,%s,%s,%s,'[]')''',
            (tenant,claim_id,message['conversation_id'],f['f']['recipe']['consumer_id'],'a'*64,'consumer:'+f['f']['recipe']['consumer_id'],
             'contact_preference' if kind=='constraint' else 'note',Jsonb({'type':'text','value':quote}),polarity,
             message['id'] if sourced else None,message['sequence'] if sourced else None,start if sourced else None,start+len(quote) if sourced else None,
             hashlib.sha256(body.encode()).hexdigest() if sourced else None,quote,kind,state,Jsonb(list(derived)),hashlib.sha256(claim_id.encode()).hexdigest()))
    return claim_id


@pytest.fixture
def v6(context_message,admin):
    """Context Source: same declarations plus EXECUTE nexloop.context.assemble:1 and the
    current Conversation READ its Claim evidence needs; built-in strategy published."""
    from nexloop_eios.service_offerings import OFFERING_FIELDS,BINDING_FIELDS
    f=context_message;tenant=f['original']['tenant'];recipe=f['f']['recipe']
    targets=[]
    for type_name,object_id,fields in (('ServiceOffering',recipe['offering_id'],OFFERING_FIELDS),('ConsumerServiceOffering',recipe['offering_binding_id'],BINDING_FIELDS)):
        targets.append(('eios:object:'+type_name+'/'+object_id,ResourceType.OBJECT))
        targets+=[('eios:property:'+type_name+'/'+object_id+'/'+field,ResourceType.PROPERTY) for field in fields]
    targets+=[('eios:object:Consumer/'+recipe['consumer_id'],ResourceType.OBJECT),('eios:object:Conversation/'+f['message']['conversation_id'],ResourceType.OBJECT)]
    specs=[('eios:action:nexloop.service.request:1',ResourceType.ACTION,Operation.EXECUTE),('eios:action:'+ACTION+':1',ResourceType.ACTION,Operation.EXECUTE),
        ('eios:artifact:local_real',ResourceType.ARTIFACT,Operation.CREATE),('eios:artifact:local_real',ResourceType.ARTIFACT,Operation.READ),
        (ASSEMBLE,ResourceType.ACTION,Operation.EXECUTE)]
    binding,rows=source_declarations(tenant,targets,custom_specs=specs)
    # A credential's binding is immutable: the widened Source gets its own credential.
    binding=binding.model_copy(update={'caller_application_id':binding.caller_application_id+':v6','credential_id':binding.credential_id+':v6'})
    for row in rows:
        if row['kind']=='application':row['key']=[binding.caller_application_id,'1'];row['payload']['application_id']=binding.caller_application_id
        if row['kind']=='authentication':row['key']=[binding.credential_id];row['payload'].update(caller_application_id=binding.caller_application_id,credential_id=binding.credential_id)
        row['payload'].pop('snapshot_digest',None)
    token=secrets.token_urlsafe(48)
    secret_map=json.loads(f['original']['paths']['secrets'].read_text());secret_map['v6-source']=token;f['original']['paths']['secrets'].write_text(json.dumps(secret_map))
    def grant(manifest):
        merged={(r['kind'],tuple(r['key'])):r for r in manifest['authority_facts']};merged.update({(r['kind'],tuple(r['key'])):r for r in rows})
        manifest['authority_facts']=list(merged.values())
        expires=next(row['payload']['expires_at'] for row in rows if row['kind']=='authentication')
        manifest['service_credentials'].append({'reference':'v6-source','binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':expires,'status':'active'})
    reconfigure(f,admin,grant)
    f['source_token']=token;f['source']=f['backend'].authenticate(token,world='real')
    seed_strategy(admin,tenant,STRATEGY)
    f['v6_relay']=lambda:MessageRelay(route=f['route'],source=f['source'],planner=f['planner'],executor_token=f['f']['tokens']['assembly-executor'],
        vault=f['vault'],recipe=f['f']['recipe'],context_strategy='recent_plus_required')
    return f


def bound(admin):
    row=admin.execute('select pack_text,pack_digest,run_id::text,context_id::text from runtime.nexloop_context_artifact_bindings').fetchone()
    return json.loads(row[0]),row


def sources(admin,tenant,context_id):
    with admin.transaction():
        owner(admin,tenant)
        return admin.execute('select ordinal,section,ref,revision,content_hash,evidence_kind,access_decision_ref,status,omitted_reason from runtime.nexloop_context_sources where context_id=%s order by ordinal',(context_id,)).fetchall()


def request_snapshot(call,pack_text,*,provider='faux',model='faux-1',carry=True,tools=({'name':'nexloop.service.request','parameters':{'type':'object'}},),settings=None):
    settings={'maxTokens':512} if settings is None else settings
    request={'model':{'api':'faux','id':model,'provider':provider},'settings':settings,
        'context':{'messages':[{'role':'user','content':pack_text if carry else '另一段输入','timestamp':1}],'tools':list(tools)}}
    text=canonical_payload(request);h=lambda value:hashlib.sha256(value.encode()).hexdigest()
    return {'call_sequence':call,'model_provider':provider,'model_id':model,'request_text':text,'request_digest':h(text),
        'tool_manifest_digest':h(canonical_payload([list(tools)])),'settings_digest':h(canonical_payload(settings))}


def model_requests(admin,tenant):
    with admin.transaction():
        owner(admin,tenant)
        return admin.execute('select r.call_sequence,r.request_digest,p.request_text,r.context_id::text,r.input_token_budget,r.output_token_budget from runtime.nexloop_model_requests r '
            'join runtime.nexloop_model_request_prompts p using(tenant_id,world,run_id,call_sequence) order by r.call_sequence').fetchall()


def manifest(admin,tenant,run_id,call):
    with admin.transaction():
        owner(admin,tenant)
        return admin.execute('select runtime.nexloop_context_manifest(%s,%s)',(run_id,call)).fetchone()[0]


def test_v6_binds_verified_sources_and_keeps_claims_out_of_formal_state(v6,admin):
    f=v6;tenant=f['original']['tenant']
    negated=seed_claim(admin,f,kind='constraint',polarity='negated')
    pending=seed_claim(admin,f,kind='intent',state='awaiting_definition',quote='正式事实')
    rejected=seed_claim(admin,f,kind='preference',state='rejected_definition',quote='用户原文')
    hypothesis=seed_claim(admin,f,kind='hypothesis',state='hypothesis_only',quote='推测顾客不想被联系',derived=(negated,))
    assert f['v6_relay']().run_once()=='queued'
    pack,row=bound(admin);text=row[0]
    ContextPackV6.model_validate(pack)
    assert pack['schema_version']=='nexloop.context-pack.v6' and pack['strategy_ref']=='context-strategy:recent_plus_required@1'
    assert pack['user_statement']['body']==f['message']['body'] and pack['current_event']=={'kind':'consumer_message','message_id':f['message']['id'],'provenance':'eios:object:'+f['message']['id']}
    # AT-064: hypotheses and rejected definitions never enter; Claims are evidence text only.
    assert hypothesis not in text and rejected not in text
    for section in ('constraints','consumer_state','open_work'):assert all(not i['ref'].startswith('claim:') for i in pack[section])
    claims={i['ref']:i for i in pack['evidence'] if i['subsection']=='claim_evidence'}
    assert set(claims)=={'claim:'+negated,'claim:'+pending}
    assert claims['claim:'+negated]['tags']==['contact_limit','negation'] and claims['claim:'+pending]['tags']==[]
    assert all('value' not in i['content'] and i['evidence_kind']=='user_statement' for i in claims.values())
    assert pack['goal']['control_snapshot']['scopes']==[{'kind':'consumer','ref':'consumer:'+f['f']['recipe']['consumer_id']}] and pack['insufficient']==[]
    # Stored provenance: one row per core source and per item, each with its decision.
    rows=sources(admin,tenant,row[3])
    assert [r[1] for r in rows[:3]]==['current_event','constraints','constraints'] and {'bindings','goal'}<={r[1] for r in rows} and all(r[7]=='included' for r in rows)
    assert {r[2] for r in rows}>={'eios:object:Message/'+f['message']['id'],'claim:'+negated,'claim:'+pending,'context-strategy:recent_plus_required@1'}
    assert not any(r[2].startswith('claim:') and r[1]!='evidence' for r in rows)
    with admin.transaction():
        owner(admin,tenant)
        assert admin.execute('select protocol,pack_digest,strategy_ref from runtime.nexloop_context_packs').fetchall()==[('nexloop.context-pack.v6',row[1],pack['strategy_ref'])]
    job=admin.execute('select normalized_input from runtime.jobs').fetchone()[0]
    assert job['input']==text and hashlib.sha256(text.encode()).hexdigest()==row[1]


def test_bound_v6_pack_replays_unchanged_after_a_lost_ack(v6,admin):
    """A retry after the bind committed returns the same Artifact; nothing is re-assembled,
    no source row is added (its read proofs have expired; SQL rechecks core/Artifact/claim)."""
    f=v6;tenant=f['original']['tenant']
    seed_claim(admin,f,kind='constraint',polarity='negated')
    assert f['v6_relay']().run_once()=='queued'
    pack,row=bound(admin)
    before=sources(admin,tenant,row[3])
    record=f['vault'].read(f['vault'].message_key(tenant,'real',f['message']['id']))
    command=admin.execute('select normalized_input from runtime.jobs').fetchone()[0]['run_command']
    seed_claim(admin,f,kind='constraint',polarity='negated',quote='正式')  # new pinned source after the bind
    again=ContextV6ArtifactProducer(f['source'],'recent_plus_required').prepare(message_id=f['message']['id'],run_token=record.token,command=command,
        offering_id=f['f']['recipe']['offering_id'],binding_id=f['f']['recipe']['offering_binding_id'])
    assert again['sha256']==row[1] and again['input']==row[0] and again['context_id']==row[3]
    assert sources(admin,tenant,row[3])==before
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone()==(1,)


def test_guard_records_each_model_request_and_refuses_gaps_conflicts_and_foreign_requests(v6,admin,tmp_path,monkeypatch):
    """AT-027 at the guard: dense call_sequence, digests recomputed from the request bytes,
    the request must carry the bound pack, provider fixed by the Run profile."""
    from nexloop_eios import runtime_activation as R
    f=v6;tenant=f['original']['tenant']
    assert f['v6_relay']().run_once()=='queued'
    pack,row=bound(admin)
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        guard=lambda **kw:worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,**kw)
        for call in (1,2,3):
            assert guard(operation='model',request_snapshot=request_snapshot(call,text,settings={'maxTokens':512,'call':call}))['authorized'] is True
        assert guard(operation='model')['authorized'] is True  # pre-commit check carries no snapshot
        recorded=model_requests(admin,tenant)
        assert [r[0] for r in recorded]==[1,2,3] and all(r[3]==row[3] for r in recorded)
        assert all(hashlib.sha256(r[2].encode()).hexdigest()==r[1] for r in recorded) and len({r[1] for r in recorded})==3
        assert all((r[4],r[5])==(STRATEGY['input_token_budget'],STRATEGY['output_reserve']) for r in recorded)
        for call in (1,2,3):
            body=manifest(admin,tenant,row[2],call)
            MANIFEST_SCHEMA.validate(body);ContextManifest.model_validate(body)
            assert body['call_sequence']==call and body['context_strategy_version']==pack['strategy_ref'] and body['request_digest']==recorded[call-1][1]
            assert {s['ref'] for s in body['sources']}>={'eios:object:Message/'+f['message']['id'],'context-strategy:recent_plus_required@1'}
        # Same call retried by the Host after a lost response: idempotent, nothing new.
        guard(operation='model',request_snapshot=request_snapshot(3,text,settings={'maxTokens':512,'call':3}))
        refused=[request_snapshot(5,text),                                   # gap
                 request_snapshot(2,text,settings={'maxTokens':1}),          # different request under an existing number
                 request_snapshot(4,text,provider='deepseek'),               # provider not allowed for the Run profile
                 request_snapshot(4,text,carry=False)]                       # request does not carry the bound Context
        for snapshot in refused:
            with pytest.raises(AuthorizationUnavailable):guard(operation='model',request_snapshot=snapshot)
        with pytest.raises(AuthorizationUnavailable):guard(operation='tool',request_snapshot=request_snapshot(4,text))
        # SQL itself recomputes digests and refuses snapshots outside `model`, even if the
        # backend shape check is bypassed.
        monkeypatch.setattr(R,'_request_snapshot',lambda operation,value:None)
        forged=request_snapshot(4,text);forged['tool_manifest_digest']='0'*64
        lying=request_snapshot(4,text);lying['request_digest']=hashlib.sha256(b'other').hexdigest()
        for operation,snapshot in (('model',forged),('model',lying),('tool',request_snapshot(4,text))):
            with pytest.raises(AuthorizationUnavailable):guard(operation=operation,request_snapshot=snapshot)
        assert [r[0] for r in model_requests(admin,tenant)]==[1,2,3]
        # Recording failure denies the call; no row survives a denied authorization.
        assert guard(operation='model',request_snapshot=request_snapshot(4,text))['authorized'] is True
    assert [r[0] for r in model_requests(admin,tenant)]==[1,2,3,4]
    with pytest.raises(psycopg.errors.InsufficientPrivilege),admin.transaction():
        admin.execute('set local role nexloop_api');admin.execute('select count(*) from runtime.nexloop_model_request_prompts')


def test_v2_runs_never_accept_a_request_snapshot(context_message,admin,tmp_path):
    f=context_message;assert f['relay'].run_once()=='queued'
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        with pytest.raises(AuthorizationUnavailable):
            worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='model',request_snapshot=request_snapshot(1,text))
        assert worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='model')['authorized'] is True
    assert admin.execute('select count(*) from runtime.nexloop_model_requests').fetchone()==(0,)


def _recount(body):
    for name in ITEM_SECTIONS:body['budget_report']['sections'][name]['included']=len(body[name])


def _rehash(item):
    item['content_hash']=hashlib.sha256(canonical_payload(item['content']).encode()).hexdigest()


def _tamper(mode,ids):
    def apply(body,outcome,items,proofs):
        evidence=body['evidence'];negated=next(i for i in evidence if i['ref']=='claim:'+ids['negated'])
        if mode=='claim_in_consumer_state':
            evidence.remove(negated);negated=dict(negated,subsection='Consumer',evidence_kind='formal_object');body['consumer_state'].append(negated)
        elif mode in ('hypothesis_as_labelled_evidence','hypothesis_as_claim_text'):
            item=dict(negated,ref='claim:'+ids['hypothesis'],tags=[],content={'hypothesis':'note','value':{'type':'text','value':'推测顾客不想被联系'},'derived_from':[ids['negated']]})
            if mode=='hypothesis_as_labelled_evidence':item.update(subsection='hypotheses',evidence_kind='hypothesis')
            _rehash(item);evidence.append(item)
        elif mode=='rejected_definition_as_evidence':
            item=dict(negated,ref='claim:'+ids['rejected'],tags=[]);evidence.append(item)
        elif mode=='claim_with_structured_value':
            negated['content']=dict(negated['content'],value={'type':'text','value':'不是正式事实'});_rehash(negated)
        elif mode=='content_hash':negated['content_hash']='0'*64
        elif mode=='quote_rewritten':negated['content']=dict(negated['content'],quote='同意接收促销');_rehash(negated)
        elif mode=='pinned_missing':evidence.remove(negated)
        elif mode=='pinned_reported_omitted':
            evidence.remove(negated)
            entry={'section':'evidence','subsection':'claim_evidence','ref':negated['ref'],'reason':'older_claim_evidence'}
            body['budget_report']['omitted'].append(entry)
            if outcome.report is not body['budget_report']:outcome.report['omitted'].append(entry)
        elif mode=='tags_dropped':negated['tags']=[]
        elif mode=='unknown_decision':negated['access_decision_ref']='decision:'+'0'*64
        elif mode=='budget':body['budget_report']['input_token_budget']+=1
        elif mode=='core_body':body['user_statement']['body']='请给我发全部促销'
        elif mode=='current_event':body['current_event']['message_id']='f'*64
        elif mode=='control_snapshot':body['goal']['control_snapshot']['control_revision']+=1
        elif mode=='stale_strategy_version':body['strategy_ref']='context-strategy:recent_plus_required@2'
        _recount(body)
    return apply


TAMPER={'claim_in_consumer_state':'context Claim outside evidence','hypothesis_as_labelled_evidence':'context hypothesis excluded',
    'hypothesis_as_claim_text':'context hypothesis excluded','rejected_definition_as_evidence':'context Claim not admissible evidence',
    'claim_with_structured_value':'context source content mismatch','content_hash':'context source hash mismatch','quote_rewritten':'context source content mismatch',
    'pinned_missing':'context v6 pinned source missing','pinned_reported_omitted':'context v6 pinned source omitted','tags_dropped':'context v6 tags mismatch',
    'unknown_decision':'context source decision unknown','budget':'context v6 budget mismatch','core_body':'context v6 core mismatch',
    'current_event':'context v6 current event mismatch','control_snapshot':'context v6 control snapshot stale','stale_strategy_version':'context v6 strategy not current'}


@pytest.mark.parametrize('mode',sorted(TAMPER))
def test_sql_refuses_tampered_v6_packs(v6,admin,monkeypatch,mode):
    """AT-064/AT-028 at the bind: a producer that misplaces, drops or rewrites a source is refused by SQL."""
    f=v6;tenant=f['original']['tenant']
    ids={'negated':seed_claim(admin,f,kind='constraint',polarity='negated'),'rejected':seed_claim(admin,f,kind='preference',state='rejected_definition',quote='用户原文')}
    ids['hypothesis']=seed_claim(admin,f,kind='hypothesis',state='hypothesis_only',quote='推测顾客不想被联系',derived=(ids['negated'],))
    if mode=='stale_strategy_version':
        newer=copy.deepcopy(STRATEGY);newer['version']=2;seed_strategy(admin,tenant,newer)
    original=ContextV6ArtifactProducer.assemble
    if mode=='stale_strategy_version':
        def assemble(self,snapshot,command):
            self.strategy_id='recent_plus_required'
            from nexloop_eios.context_engine import strategy as S
            real=S.StrategyRegistry.get
            S.StrategyRegistry.get=lambda registry,strategy_id,version=None:real(registry,strategy_id,1)
            try:return original(self,snapshot,command)
            finally:S.StrategyRegistry.get=real
    else:
        change=_tamper(mode,ids)
        def assemble(self,snapshot,command):
            body,outcome,items,proofs=original(self,snapshot,command)
            change(body,outcome,items,proofs)
            return body,outcome,items,proofs
    monkeypatch.setattr(ContextV6ArtifactProducer,'assemble',assemble)
    relay=f['v6_relay']()
    with pytest.raises(MessageRelayUnavailable):relay.run_once()
    assert relay._context_diagnostic is not None and relay._context_diagnostic[1]==TAMPER[mode],relay._context_diagnostic
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone()==(0,)
    with admin.transaction():
        owner(admin,tenant)
        assert admin.execute('select (select count(*) from runtime.nexloop_context_packs),(select count(*) from runtime.nexloop_context_sources)').fetchone()==(0,0)


@pytest.mark.parametrize('mode',['trim','mandatory_exceeds'])
def test_budget_trims_evidence_but_never_pinned_sources(v6,admin,mode):
    """AT-028: over budget, non-pinned evidence is omitted with reasons (recorded as omitted
    sources); negations/contact limits stay; if pinned alone exceed, the pack says so."""
    f=v6;tenant=f['original']['tenant']
    tiny=copy.deepcopy(STRATEGY);tiny.update(version=2,input_token_budget=1024,framing_reserve=0)
    seed_strategy(admin,tenant,tiny)
    long='很长的历史原文'*90
    pinned=[seed_claim(admin,f,kind='constraint',polarity='negated',quote=('不要再联系我' if mode=='trim' else '不要再联系我'+long)+str(n)) for n in range(1 if mode=='trim' else 3)]
    loose=[seed_claim(admin,f,kind='intent',state='awaiting_definition',quote=long+str(n)) for n in range(5)]
    assert f['v6_relay']().run_once()=='queued'
    pack,row=bound(admin)
    assert pack['strategy_ref']=='context-strategy:recent_plus_required@2'
    refs={i['ref'] for i in pack['evidence']}
    assert {'claim:'+c for c in pinned}<=refs
    omitted=pack['budget_report']['omitted']
    assert omitted and {o['ref'] for o in omitted}<={'claim:'+c for c in loose} and not any(o['ref'] in refs for o in omitted)
    if mode=='trim':
        assert {o['reason'] for o in omitted}=={'older_claim_evidence'} and pack['insufficient']==[]
    else:
        assert {o['reason'] for o in omitted}=={'mandatory_exceeds_budget'} and {'claim:'+c for c in loose}=={o['ref'] for o in omitted}
        assert pack['insufficient'][0]['code']=='mandatory_exceeds_budget' and set(pack['insufficient'][0]['refs'])=={'claim:'+c for c in pinned}
        # Pinned items are whole, never truncated.
        assert all(next(i for i in pack['evidence'] if i['ref']=='claim:'+c)['content']['quote'].startswith('不要再联系我'+long) for c in pinned)
    stored=sources(admin,tenant,row[3])
    assert {(r[2],r[7],r[8]) for r in stored if r[7]=='omitted'}=={(o['ref'],'omitted',o['reason']) for o in omitted}


def test_actual_pi_run_records_every_model_call_of_a_v6_run(v6,admin,tmp_path):
    """AT-027 end to end: Host parses v6, Pi makes several model calls, each actual
    provider request is recorded before the call with version/sources/hash."""
    import secrets,sqlite3
    from psycopg.conninfo import make_conninfo
    from nexloop_eios.backend import open_backend
    from nexloop_eios.runtime_dispatch import RuntimeDispatcher,HostControlConfiguration
    from test_agent_host import files
    from test_runtime_host_admission import guard_server,configuration
    from test_message_runtime_effect_e2e import actual_host
    from test_runtime_effect_tools import tool_evidence
    f=v6;tenant=f['original']['tenant'];o=f['original']
    negated=seed_claim(admin,f,kind='constraint',polarity='negated')
    assert f['v6_relay']().run_once()=='queued'
    pack,row=bound(admin)
    home=tmp_path/'v6-host';home.mkdir(mode=0o700);runtime,key=files(home)
    guard_key=home/'v6-guard-key';guard_key.write_text(secrets.token_hex(32));guard_key.chmod(0o600)
    with open_backend(database_url=make_conninfo(o['pg'],user='nexloop_domain_worker'),artifact_root=home/'domain-artifacts',signing_key_file=o['paths']['backend_signing'],signing_key_id='explicit-configuration') as backend:
        worker=backend.authenticate(f['f']['tokens']['assembly-runtime-worker'],world='real')
        with guard_server(worker,home,guard_key) as port:
            cfg=configuration(home,port,guard_key);body=json.loads(cfg.read_text())
            body.update(effect_tools=True,deterministic_message_from_input=True,context_input_protocol='nexloop.context-pack.v6');cfg.write_text(json.dumps(body))
            with actual_host(runtime,key,cfg) as (_,client,headers):
                result=RuntimeDispatcher(worker,HostControlConfiguration(str(client.base_url).rstrip('/'),key,home/'host-cert.pem'),queue='operations',lease_seconds=60,total_timeout=45,request_timeout=10).run_once()
                assert result['claimed'] is True and result['status']=='succeeded',result.get('result',{}).get('code')
    calls,receipts=tool_evidence(runtime/row[2]/'runtime.sqlite')
    assert [c['name'] for c in calls]==['nexloop.service.request','nexloop.service.request','nexloop.service.find']
    with sqlite3.connect('file:'+str(runtime/row[2]/'runtime.sqlite')+'?mode=ro',uri=True) as db:
        records=[json.loads(r[0]) for r in db.execute('select record from entries order by id')]
    recorded=model_requests(admin,tenant)
    # Deterministic flow: request, rebuilt request, find, completion = four model calls, each recorded once, densely.
    assert [r[0] for r in recorded]==[1,2,3,4] and len({r[1] for r in recorded})==4
    for call,digest,prompt,context_id,_,_ in recorded:
        assert hashlib.sha256(prompt.encode()).hexdigest()==digest and context_id==row[3]
        request=json.loads(prompt)
        assert request['model']['provider']=='faux' and set(request)=={'model','context','settings'}
        assert any(m['role']=='user' and (m['content']==row[0] or any(c.get('text')==row[0] for c in m['content'] if isinstance(c,dict))) for m in request['context']['messages'])
        declared=[t['name'] for m in request['context']['messages'] if m['role']=='system' for t in m.get('toolsAdded',[])]
        assert sorted(declared)==['nexloop.service.find','nexloop.service.request']
        body=manifest(admin,tenant,row[2],call);MANIFEST_SCHEMA.validate(body);ContextManifest.model_validate(body)
        assert body['model_provider']=='faux' and body['context_id']==row[3] and 'claim:'+negated in {s['ref'] for s in body['sources']}
    # Every actual call also has its outcome, written once after the provider answered.
    outcomes=results(admin,tenant)
    assert [o[:2] for o in outcomes]==[(n,'succeeded') for n in (1,2,3,4)] and all(o[5] and o[2]['total']>=0 and o[3]=='0.00000000' and len(o[4])==64 for o in outcomes)
    # Later requests carry the earlier tool results: the transcript grows call by call.
    assert [len(json.loads(r[2])['context']['messages']) for r in recorded]==sorted(len(json.loads(r[2])['context']['messages']) for r in recorded)


def test_relay_cli_subprocess_binds_v6_with_the_given_strategy(v6,admin,tmp_path):
    """message_relay_cli --context-strategy end to end in its own process."""
    import subprocess,sys
    from psycopg.conninfo import make_conninfo
    f=v6;o=f['original'];tokens=f['f']['tokens']
    private=tmp_path/'relay-cli';private.mkdir(mode=0o700);vault=private/'vault';vault.mkdir(mode=0o700)
    material={'database-url-file':make_conninfo(o['pg'],user='nexloop_api'),'route-credential-file':tokens['assembly-route'],'source-credential-file':f['source_token'],
        'planner-credential-file':tokens['assembly-planner'],'executor-credential-file':tokens['assembly-executor'],'recipe-file':json.dumps(f['f']['recipe'])}
    argv=[]
    for name,value in material.items():
        path=private/name;path.write_text(value);path.chmod(0o600);argv+=['--'+name,str(path)]
    argv+=['--signing-key-file',str(o['paths']['backend_signing']),'--signing-key-id','explicit-configuration','--artifact-root',str(private/'artifacts'),'--vault-root',str(vault),'--once']
    done=subprocess.run([sys.executable,'-m','nexloop_eios.message_relay_cli',*argv,'--context-strategy','recent_plus_required'],capture_output=True,text=True,timeout=60)
    assert done.returncode==0 and done.stdout=='Message relay ready\n{"status":"queued"}\n',done.stderr
    pack,row=bound(admin)
    assert pack['schema_version']=='nexloop.context-pack.v6' and pack['strategy_ref']=='context-strategy:recent_plus_required@1'
    job=admin.execute('select normalized_input from runtime.jobs').fetchone()[0]
    assert job['input']==row[0]
    assert all(token not in done.stdout+done.stderr for token in [*tokens.values(),f['source_token']])


def test_unpublished_strategy_fails_closed_and_binds_nothing(v6,admin):
    f=v6
    relay=MessageRelay(route=f['route'],source=f['source'],planner=f['planner'],executor_token=f['f']['tokens']['assembly-executor'],
        vault=f['vault'],recipe=f['f']['recipe'],context_strategy='unpublished_strategy')
    with pytest.raises(MessageRelayUnavailable):relay.run_once()
    assert relay._context_diagnostic is not None
    assert admin.execute('select count(*) from runtime.nexloop_context_artifact_bindings').fetchone()==(0,)
    assert admin.execute('select count(*) from runtime.nexloop_local_artifacts').fetchone()==(0,)


def results(admin,tenant):
    with admin.transaction():
        owner(admin,tenant)
        return admin.execute('select call_sequence,result_status,usage,cost::text,response_digest,completed_at is not null from runtime.nexloop_model_requests order by call_sequence').fetchall()


def test_model_result_is_written_once_for_a_recorded_request(v6,admin,tmp_path):
    f=v6;tenant=f['original']['tenant']
    assert f['v6_relay']().run_once()=='queued'
    ok={'call_sequence':1,'result_status':'succeeded','usage':{'input':120,'output':30,'cache_read':0,'cache_write':0,'total':150},'cost':'0.00012000','response_digest':'b'*64}
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        guard=lambda **kw:worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='model',**kw)
        with pytest.raises(AuthorizationUnavailable):guard(model_result=ok)                       # no recorded request yet
        guard(request_snapshot=request_snapshot(1,text))
        assert results(admin,tenant)==[(1,None,None,None,None,False)]
        assert guard(model_result=ok)['authorized'] is True
        assert guard(model_result=ok)['authorized'] is True                                    # same outcome retried: idempotent
        for bad in (dict(ok,result_status='failed'),dict(ok,cost='0.5'),                        # a second, different outcome
                    dict(ok,result_status='succeeded',usage=None),dict(ok,cost='1e-5'),dict(ok,call_sequence=2),
                    dict(ok,usage={**ok['usage'],'input':-1}),{k:v for k,v in ok.items() if k!='cost'}):
            with pytest.raises(AuthorizationUnavailable):guard(model_result=bad)
        with pytest.raises(AuthorizationUnavailable):guard(request_snapshot=request_snapshot(2,text),model_result=dict(ok,call_sequence=2))
        guard(request_snapshot=request_snapshot(2,text))
        guard(model_result={'call_sequence':2,'result_status':'unknown','usage':None,'cost':None,'response_digest':None})
    assert results(admin,tenant)==[(1,'succeeded',ok['usage'],'0.00012000','b'*64,True),(2,'unknown',None,None,None,True)]
    assert manifest(admin,tenant,command['run_id'],1)['request_digest']==request_snapshot(1,text)['request_digest']


def test_v2_runs_never_accept_a_model_result(context_message,admin,tmp_path):
    f=context_message;assert f['relay'].run_once()=='queued'
    with active_worker(f,tmp_path) as (worker,activation,command,text):
        with pytest.raises(AuthorizationUnavailable):
            worker.authorize_runtime_activation(activation_ref=activation['activation_ref'],command=command,operation='model',
                model_result={'call_sequence':1,'result_status':'unknown','usage':None,'cost':None,'response_digest':None})
