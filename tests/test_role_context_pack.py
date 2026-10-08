"""Serializer declarations only, not PG provenance or dispatch evidence."""
import json
from datetime import UTC,datetime,timedelta
import pytest
from test_context_pack import inputs
from nexloop_eios.role_context_pack import encode_role_pack,PROTOCOL

def declared():
    body,command=inputs();now=datetime.now(UTC)
    role,link='9'*64,'a'*64
    command['role_ref']='role:'+role+':mapping:'+link
    body['schema_version']=PROTOCOL
    statement=body.pop('user_statement')
    import uuid
    command['trigger_event_id']=str(uuid.uuid4())
    body['trigger_statement']={'kind':'service_trigger','event_id':command['trigger_event_id'],'source_principal':body['bindings']['source_principal'],'body':statement['body'],'provenance':'eios:role-trigger:'+command['trigger_event_id']}
    body['role_binding']={'binding':{'run_id':command['run_id'],'tenant_id':command['tenant_id'],'world':'real',
      'consumer_id':'1'*64,'step_id':'4'*64,'step_revision':1,'role_id':role,'link_id':link,'link_revision':1,'role_revision':1,
      'role_ref':command['role_ref'],'scope':'synthetic-service','expires_at':(now+timedelta(seconds=30)).isoformat()},
      'definition':{'name':'delivery','responsibility':'synthetic declared responsibility','ceiling_ref':'metadata-only',
      'active':True,'valid_from':(now-timedelta(seconds=10)).isoformat(),'valid_until':(now+timedelta(seconds=60)).isoformat()},
      'definition_provenance':'eios:object:'+role,'mapping_provenance':'eios:object:'+link,'grants_authority':False}
    return body,command

def test_explicit_v3_keeps_role_metadata_and_v2_shape_frozen():
    body,command=declared();encoded=json.loads(encode_role_pack(body,command))
    assert encoded['schema_version']==PROTOCOL
    assert encoded['role_binding']==body['role_binding']
    from nexloop_eios.context_pack import encode_pack
    with pytest.raises(Exception):encode_pack(body,command)

@pytest.mark.parametrize('fault',['missing','grant','run','role','step','revision','provenance','naive','inactive','interval','expired','hypothesis','awaiting_definition'])
def test_role_formal_section_failclosed(fault):
    body,command=declared();role=body['role_binding']
    if fault=='missing':del body['role_binding']
    elif fault=='grant':role['grants_authority']=True
    elif fault=='run':role['binding']['run_id']='11111111-1111-1111-1111-111111111111'
    elif fault=='role':role['binding']['role_ref']='opaque'
    elif fault=='step':role['binding']['step_id']='f'*64
    elif fault=='revision':role['binding']['step_revision']=2
    elif fault=='provenance':role['mapping_provenance']='eios:object:'+'f'*64
    elif fault=='naive':role['definition']['valid_from']='2026-01-01T00:00:00'
    elif fault=='inactive':role['definition']['active']=False
    elif fault=='interval':role['definition']['valid_from']=role['definition']['valid_until']
    elif fault=='expired':role['binding']['expires_at']='2026-01-01T00:00:00Z'
    else:body['formal_facts'].append({'type':fault,'id':'b'*64,'revision':1,'provenance':'eios:object:'+'b'*64})
    with pytest.raises(Exception):encode_role_pack(body,command)
