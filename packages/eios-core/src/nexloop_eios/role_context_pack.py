"""Explicit RoleBinding Context v3, and v5 = v3 + governed Role policy provenance. v2 remains frozen.

Input must be an owner-protected PG snapshot reconstructed inside the Artifact
bind transaction; this serializer cannot produce a dispatch permission.
"""
from copy import deepcopy
import json
from datetime import datetime,UTC
from jsonschema import Draft202012Validator
from nexloop_eios.context_pack import PACK_SCHEMA,REV,ID,_object_schema,encode_pack,MAX_PACK_BYTES
from nexloop_eios.postgres_artifacts import canonical_payload

PROTOCOL='nexloop.context-pack.v3'
PROTOCOL_V5='nexloop.context-pack.v5'
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

POLICY_SCHEMA=_object_schema({
 'binding':_object_schema({'run_id':{'type':'string','format':'uuid'},'tenant_id':{'type':'string','format':'uuid'},'world':{'const':'real'},
  'ceiling_id':ID,'ceiling_revision':REV,'scope_id':ID,'scope_revision':REV,'budget':{'type':'object'},
  'effect_units':{'type':'integer','minimum':1,'maximum':1000000},'expires_at':UTC_TIME}),
 'ceiling':{'type':'object'},'scope':{'type':'object'},
 'ceiling_provenance':{'type':'string','pattern':r'^eios:object:[a-f0-9]{64}$'},
 'scope_provenance':{'type':'string','pattern':r'^eios:object:[a-f0-9]{64}$'},'grants_authority':{'const':False}})


def _validate_policy_section(policy,body,command):
    from nexloop_eios.role_policies import validate_policy,assert_budget_within
    Draft202012Validator(POLICY_SCHEMA,format_checker=Draft202012Validator.FORMAT_CHECKER).validate(policy)
    binding=policy['binding'];role=body['role_binding']['binding'];definition=body['role_binding']['definition']
    validate_policy('RoleExecutionCeiling',policy['ceiling']);validate_policy('RoleAssignmentScope',policy['scope'])
    assert_budget_within(command['budget'],policy['ceiling']['budget'])
    if binding['run_id']!=command['run_id'] or binding['tenant_id']!=command['tenant_id'] or binding['budget']!=command['budget'] \
            or binding['effect_units']!=policy['ceiling']['effect_units']:raise ValueError('context_policy_binding_invalid')
    if definition['ceiling_ref']!=binding['ceiling_id'] or role['scope']!=binding['scope_id'] \
            or policy['scope']['role_id']!=role['role_id'] or policy['scope']['consumer_id']!=role['consumer_id']:raise ValueError('context_policy_selection_invalid')
    facts={f['type']:f for f in body['formal_facts']}
    if role['consumer_id'] not in policy['ceiling']['consumer_ids'] or any(facts[kind]['id'] not in p[field]
            for p in (policy['ceiling'],policy['scope']) for kind,field in (('Goal','goal_ids'),('PlanStep','step_ids'))):raise ValueError('context_policy_range_invalid')
    if datetime.fromisoformat(binding['expires_at'])<=datetime.now(UTC):raise ValueError('context_policy_expired')
    for key in ('ceiling','scope'):
        if policy[key+'_provenance']!='eios:object:'+binding[key+'_id']:raise ValueError('context_policy_provenance_invalid')


def encode_role_pack(snapshot,command):
    """v3, or v5 when the bound Run carries a governed Role policy section."""
    if snapshot.get('schema_version')==PROTOCOL_V5:
        body=deepcopy(snapshot);policy=body.pop('role_policy',None)
        if policy is None:raise ValueError('context_policy_required')
        body['schema_version']=PROTOCOL
        _validate_policy_section(policy,body,command)
        encoded=json.loads(_encode_v3(body,command))
        encoded['schema_version']=PROTOCOL_V5;encoded['role_policy']=policy
        text=canonical_payload(encoded)
        if len(text.encode())>MAX_PACK_BYTES:raise ValueError('context_pack_too_large')
        return text
    return _encode_v3(snapshot,command)


def _encode_v3(snapshot,command):
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
