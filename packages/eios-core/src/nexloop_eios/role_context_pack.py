"""Explicit RoleBinding Context v3. v2 remains frozen.

Input must be an owner-protected PG snapshot reconstructed inside the Artifact
bind transaction; this serializer cannot produce a dispatch permission.
"""
from copy import deepcopy
from datetime import datetime,UTC
from jsonschema import Draft202012Validator
from nexloop_eios.context_pack import PACK_SCHEMA,REV,ID,_object_schema,encode_pack,MAX_PACK_BYTES
from nexloop_eios.postgres_artifacts import canonical_payload

PROTOCOL='nexloop.context-pack.v3'
UTC_TIME={'type':'string','format':'date-time','pattern':r'(Z|\+00:00)$'}
ROLE_CONTEXT_SCHEMA=_object_schema({
 'binding':_object_schema({'run_id':{'type':'string','format':'uuid'},'tenant_id':{'type':'string','format':'uuid'},
 'world':{'const':'real'},'consumer_id':ID,'link_id':ID,'role_id':ID,'step_id':ID,
 'link_revision':REV,'role_revision':REV,'step_revision':REV,
 'role_ref':{'type':'string','pattern':r'^role:[a-f0-9]{64}:mapping:[a-f0-9]{64}$'},
 'scope':{'type':'string','minLength':1,'maxLength':8192},'expires_at':UTC_TIME}),
 'definition':_object_schema({'name':{'type':'string','minLength':1,'maxLength':8192},
 'responsibility':{'type':'string','minLength':1,'maxLength':8192},
 'ceiling_ref':{'type':'string','minLength':1,'maxLength':8192},'active':{'const':True},
 'valid_from':UTC_TIME,'valid_until':UTC_TIME}),
 'definition_provenance':{'type':'string','pattern':r'^eios:object:[a-f0-9]{64}$'},
 'mapping_provenance':{'type':'string','pattern':r'^eios:object:[a-f0-9]{64}$'},'grants_authority':{'const':False}})
PACK_SCHEMA_V3=deepcopy(PACK_SCHEMA)
PACK_SCHEMA_V3['properties']['schema_version']={'const':PROTOCOL}
PACK_SCHEMA_V3['properties']['role_binding']=ROLE_CONTEXT_SCHEMA
PACK_SCHEMA_V3['required'].append('role_binding')
PACK_SCHEMA_V3['properties'].pop('user_statement')
PACK_SCHEMA_V3['required'].remove('user_statement')
PACK_SCHEMA_V3['properties']['trigger_statement']=_object_schema({'kind':{'const':'service_trigger'},'event_id':{'type':'string','format':'uuid'},'source_principal':{'type':'string','minLength':1,'maxLength':512},'body':{'type':'string','minLength':1,'maxLength':8192},'provenance':{'type':'string','pattern':r'^eios:role-trigger:[a-f0-9-]{36}$'}})
PACK_SCHEMA_V3['required'].append('trigger_statement')


def encode_role_pack(snapshot,command):
    body=deepcopy(snapshot)
    Draft202012Validator(PACK_SCHEMA_V3,format_checker=Draft202012Validator.FORMAT_CHECKER).validate(body)
    role=body['role_binding'];bound=role['binding'];definition=role['definition']
    facts={fact['type']:fact for fact in body['formal_facts']}
    for key,expected in {'run_id':command['run_id'],'tenant_id':command['tenant_id'],
                        'consumer_id':facts['Consumer']['id'],'step_id':facts['PlanStep']['id'],
                        'step_revision':facts['PlanStep']['revision'],'role_ref':command['role_ref']}.items():
        if bound[key]!=expected:raise ValueError('context_role_binding_invalid')
    if bound['role_ref']!='role:'+bound['role_id']+':mapping:'+bound['link_id']:
        raise ValueError('context_role_binding_invalid')
    if role['definition_provenance']!='eios:object:'+bound['role_id'] or role['mapping_provenance']!='eios:object:'+bound['link_id']:
        raise ValueError('context_role_provenance_invalid')
    start,end=map(datetime.fromisoformat,(definition['valid_from'],definition['valid_until']))
    expiry=datetime.fromisoformat(bound['expires_at'])
    if start>=end or not start<=datetime.now(UTC)<end or expiry>end or expiry<=datetime.now(UTC):
        raise ValueError('context_role_validity_invalid')
    trigger=body['trigger_statement']
    if trigger['event_id']!=command['trigger_event_id'] or trigger['source_principal']!=body['bindings']['source_principal'] or trigger['provenance']!='eios:role-trigger:'+trigger['event_id']:
        raise ValueError('context_trigger_invalid')
    if len(facts)!=4 or any(f['provenance']!='eios:object:'+f['id'] for f in facts.values()):
        raise ValueError('context_facts_invalid')
    for field in ('tenant_id','world_id','run_id'):
        if body['bindings'][field]!=command[field]:raise ValueError('context_command_invalid')
    if body['current_constraints']['reserved_units']>body['current_constraints']['budget_units']:
        raise ValueError('context_budget_invalid')
    if body['supply']['provenance']!='eios:object:'+body['supply']['offering_id']:raise ValueError('context_supply_invalid')
    from nexloop_eios.context_pack import command_binding
    import hashlib
    body['bindings']['command_digest']=hashlib.sha256(canonical_payload(command_binding(command)).encode()).hexdigest()
    text=canonical_payload(body)
    if len(text.encode())>MAX_PACK_BYTES:raise ValueError('context_pack_too_large')
    return text
