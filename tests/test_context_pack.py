import copy,json,uuid
import pytest
from nexloop_eios.context_pack import encode_pack,command_binding

def inputs():
    tenant,run,context=map(str,(uuid.uuid4(),uuid.uuid4(),uuid.uuid4()))
    command={key:'value' for key in ('schema_version','request_id','consumer_ref','goal_version_ref','role_ref','runtime_profile','trigger_event_id')}
    command.update(run_id=run,tenant_id=tenant,world_id='real',mode='real',runtime_owner_epoch=1,budget={},not_after='2026-10-08T22:00:00Z',credential_ref='run:'+run,context_manifest_ref='artifact:placeholder')
    snapshot={'schema_version':'nexloop.context-pack.v2','bindings':{'tenant_id':tenant,'world_id':'real','run_id':run,'source_principal':'source','context_id':context,'namespace':'e'*64,'artifact_id':'f'*32,'command_digest':'a'*64},
     'user_statement':{'message_id':'b'*64,'conversation_id':'c'*64,'sequence':1,'body':'用户原文；不是已核实事实','provenance':'eios:object:'+'b'*64},
     'formal_facts':[{'type':kind,'id':str(i)*64,'revision':1,'provenance':'eios:object:'+str(i)*64} for i,kind in enumerate(('Consumer','EffectControl','Goal','PlanStep'),1)],
     'current_constraints':{'action':'nexloop.service.request:1','allow_effect':True,'budget_units':2,'reserved_units':0,'valid_until':'2026-10-08T22:00:00Z','executor_principal':'executor'}}
    from nexloop_eios.service_offerings import json_export_example
    snapshot['supply']={'offering_id':'7'*64,'offering_revision':1,'binding_id':'8'*64,'binding_revision':1,'provenance':'eios:object:'+'7'*64,'properties':json_export_example(valid_until='2026-10-08T22:00:00Z')}
    return snapshot,command

def test_canonical_pack_preserves_statement_and_explicit_fact_boundary():
    snapshot,command=inputs();text=encode_pack(snapshot,command)
    assert json.loads(text)['user_statement']['body']==snapshot['user_statement']['body']
    assert '用户原文' not in json.dumps(json.loads(text)['formal_facts'],ensure_ascii=False)
    assert encode_pack(dict(reversed(list(snapshot.items()))),command)==text
    assert command_binding(command)==command_binding({**command,'context_manifest_ref':'artifact:actual','credential_ref':'opaque-other-ref'})

@pytest.mark.parametrize('section,field,value',[
 ('user_statement','sequence',9007199254740992),('current_constraints','budget_units',9007199254740992),
 ('current_constraints','reserved_units',9007199254740992),('current_constraints','allow_effect',False),
 ('bindings','run_id','00000000-0000-4000-8000-00000000c0de'),('user_statement','provenance','eios:object:'+'d'*64)])
def test_invalid_pack_rejected(section,field,value):
    snapshot,command=inputs();snapshot[section][field]=value
    with pytest.raises(Exception):encode_pack(snapshot,command)

def test_revision_safe_limit_and_no_unknown_secret_fields():
    snapshot,command=inputs();snapshot['formal_facts'][0]['revision']=9007199254740991
    encode_pack(snapshot,command)
    snapshot['formal_facts'][0]['revision']+=1
    with pytest.raises(Exception):encode_pack(snapshot,command)
    snapshot,command=inputs();snapshot['bindings']['run_token']='synthetic-private-key'
    with pytest.raises(Exception):encode_pack(snapshot,command)
