"""Parser-only synthetic declarations, never a PostgreSQL authorization proof."""
from datetime import UTC,datetime,timedelta
import hashlib,json,shutil,subprocess
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
FIELDS=['schema_version','run_id','request_id','tenant_id','world_id','mode','consumer_ref','goal_version_ref','role_ref','runtime_owner_epoch','runtime_profile','trigger_event_id','budget','not_after']
def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(text):return hashlib.sha256(text.encode()).hexdigest()
def declaration():
    tenant='740e5644-267c-4bf0-ac7f-9cbcb4e00170';run='e5d4b91d-8c20-47cb-bf02-9186c0e34f91';source='synthetic-source'
    artifact=digest(canonical([tenant,'real',source,'context:'+run]))[:32]
    command={'schema_version':'1.0','run_id':run,'request_id':'synthetic-context-request','tenant_id':tenant,'world_id':'real','mode':'real',
     'consumer_ref':'consumer:'+'a'*64,'goal_version_ref':'goal:'+'b'*64+':revision:1:step:1:control:1','role_ref':'role:source','context_manifest_ref':'artifact:'+artifact,
     'credential_ref':'run:'+run,'runtime_owner_epoch':1,'runtime_profile':'deterministic-test','trigger_event_id':'f7ba857a-6383-4379-a88e-de9e09c94b41',
     'budget':{'maximum_model_turns':8,'maximum_tool_calls':8,'active_timeout_seconds':60,'maximum_cost':'1.0','currency':'USD'},'not_after':(datetime.now(UTC)+timedelta(minutes=2)).isoformat()}
    command_digest=digest(canonical({key:command[key] for key in FIELDS}))
    pack={'schema_version':'nexloop.context-pack.v1','bindings':{'tenant_id':tenant,'world_id':'real','run_id':run,'source_principal':source,
     'context_id':'d03cd184-b6d1-4d0b-a72c-664caf06ab23','namespace':digest(tenant+'\0real'),'artifact_id':artifact,'command_digest':command_digest},
     'user_statement':{'message_id':'e'*64,'conversation_id':'f'*64,'sequence':1,'body':'用户陈述，不是正式事实。','provenance':'eios:object:'+'e'*64},
     'formal_facts':[{'type':kind,'id':letter*64,'revision':1,'provenance':'eios:object:'+letter*64} for kind,letter in [('Consumer','a'),('Goal','b'),('PlanStep','c'),('EffectControl','d')]],
     'current_constraints':{'action':'nexloop.service.request:1','allow_effect':True,'budget_units':1,'reserved_units':0,'valid_until':command['not_after'],'executor_principal':'synthetic-executor'}}
    return command,pack

def run_parser(tmp_path,command,pack,*,input=None,attestation=None):
    text=canonical(pack) if input is None else input
    proof={'artifact_ref':command['context_manifest_ref'],'sha256':digest(text),'command_binding_digest':digest(canonical({key:command[key] for key in FIELDS}))} if attestation is None else attestation
    path=tmp_path/'parser-input.json';path.write_text(json.dumps({'command':command,'input':text,'attestation':proof}));path.chmod(0o600)
    code="""import {readFileSync} from 'node:fs';
import {validateContextInput} from './apps/agent-host/dist/context-input.js';
const f=JSON.parse(readFileSync(process.argv[1],'utf8'));
try{console.log(JSON.stringify(validateContextInput(f.input,f.command,f.attestation)));}
catch(error){if(error.code!=='runtime_context_invalid')process.exit(3);console.log('runtime_context_invalid');}
"""
    node=shutil.which('node');assert node
    result=subprocess.run([node,'--input-type=module','-e',code,str(path)],cwd=ROOT,capture_output=True,text=True,timeout=10)
    assert result.returncode==0 and result.stderr==''
    return result.stdout.strip()

def test_valid_exact_pack_extracts_user_statement_only(tmp_path):
    command,pack=declaration()
    assert json.loads(run_parser(tmp_path,command,pack))=={'body':pack['user_statement']['body'],'run_id':command['run_id']}

@pytest.mark.parametrize('case',['run','tenant','world','ref','command_hash','namespace','source','extra','provenance','facts_duplicate','revision_bool','budget','malformed_date','body_object','self_hash','duplicate_key','loose_json','malformed_json','whitespace','attestation_missing','attestation_hash','attestation_extra'])
def test_invalid_or_unbound_pack_is_never_loosely_interpreted(tmp_path,case):
    command,pack=declaration();input=None;proof=None
    if case=='run':pack['bindings']['run_id']='9429d68f-a5f2-4aee-881d-4f7c9128d003'
    elif case=='tenant':pack['bindings']['tenant_id']='9429d68f-a5f2-4aee-881d-4f7c9128d003'
    elif case=='world':pack['bindings']['world_id']='simulation'
    elif case=='ref':command['context_manifest_ref']='artifact:'+'0'*32
    elif case=='command_hash':pack['bindings']['command_digest']='0'*64
    elif case=='namespace':pack['bindings']['namespace']='0'*64
    elif case=='source':pack['bindings']['source_principal']='another-source'
    elif case=='extra':pack['extra']='not allowed'
    elif case=='provenance':pack['user_statement']['provenance']='eios:object:'+'0'*64
    elif case=='facts_duplicate':pack['formal_facts'][3]=pack['formal_facts'][0]
    elif case=='revision_bool':pack['formal_facts'][0]['revision']=True
    elif case=='budget':pack['current_constraints']['reserved_units']=2
    elif case=='malformed_date':pack['current_constraints']['valid_until']='2026-02-31T01:00:00+00:00'
    elif case=='body_object':pack['user_statement']['body']={'text':'not a string'}
    elif case=='self_hash':pack['sha256']=digest('fabricated self hash')
    elif case=='duplicate_key':input=canonical(pack).replace('"schema_version":','"schema_version":"nexloop.context-pack.v1","schema_version":')
    elif case=='loose_json':input='{"user_statement":{"body":"arbitrary JSON"}}'
    elif case=='malformed_json':input='{"user_statement":NaN}'
    elif case=='whitespace':input=' '+canonical(pack)
    else:
        proof={'artifact_ref':command['context_manifest_ref'],'sha256':digest(canonical(pack)),'command_binding_digest':pack['bindings']['command_digest']}
        if case=='attestation_missing':proof={}
        elif case=='attestation_hash':proof['sha256']='0'*64
        else:proof['extra']='not allowed'
    assert run_parser(tmp_path,command,pack,input=input,attestation=proof)=='runtime_context_invalid'
