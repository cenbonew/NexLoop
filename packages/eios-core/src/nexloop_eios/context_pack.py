"""Bounded runtime context serialization; caller input never becomes authority.

The producer consumes a protected PostgreSQL snapshot. SQL must independently
reconstruct and compare the same JSON before binding its Artifact. This module
neither grants permissions nor reads arbitrary business tables.
"""
import hashlib
from jsonschema import Draft202012Validator
from nexloop_eios.postgres_artifacts import canonical_payload

MAX_PACK_BYTES=65536
COMMAND_FIELDS=('schema_version','run_id','request_id','tenant_id','world_id','mode',
 'consumer_ref','goal_version_ref','role_ref','runtime_owner_epoch','runtime_profile',
 'trigger_event_id','budget','not_after')

def command_binding(command):
    # Credential reference and Artifact reference cannot enter their own digest.
    if type(command) is not dict or any(key not in command for key in COMMAND_FIELDS):
        raise ValueError('context_command_invalid')
    return {key:command[key] for key in COMMAND_FIELDS}


def _object_schema(properties):
    return {'type':'object','properties':properties,'required':sorted(properties),'additionalProperties':False}

ID={'type':'string','pattern':'^[a-f0-9]{64}$'}
REV={'type':'integer','minimum':1,'maximum':9007199254740991}
FACT=_object_schema({'type':{'enum':['Consumer','Goal','PlanStep','EffectControl']},
 'id':ID,'revision':REV,'provenance':{'type':'string','pattern':'^eios:object:[a-f0-9]{64}$'}})
PACK_SCHEMA=_object_schema({
 'schema_version':{'const':'nexloop.context-pack.v1'},
 'bindings':_object_schema({'tenant_id':{'type':'string','format':'uuid'},'world_id':{'const':'real'},
  'run_id':{'type':'string','format':'uuid'},'source_principal':{'type':'string','minLength':1,'maxLength':512},
  'context_id':{'type':'string','format':'uuid'},'namespace':{'type':'string','pattern':'^[a-f0-9]{64}$'},'artifact_id':{'type':'string','pattern':'^[a-f0-9]{32}$'},'command_digest':{'type':'string','pattern':'^[a-f0-9]{64}$'}}),
 'user_statement':_object_schema({'message_id':ID,'conversation_id':ID,'sequence':REV,
  'body':{'type':'string','maxLength':32768},'provenance':{'type':'string','pattern':'^eios:object:[a-f0-9]{64}$'}}),
 'formal_facts':{'type':'array','items':FACT,'minItems':4,'maxItems':4},
 'current_constraints':_object_schema({'action':{'const':'nexloop.service.request:1'},
  'allow_effect':{'const':True},'budget_units':{'type':'integer','minimum':0,'maximum':9007199254740991},
  'reserved_units':{'type':'integer','minimum':0,'maximum':9007199254740991},'valid_until':{'type':'string','format':'date-time'},
  'executor_principal':{'type':'string','minLength':1,'maxLength':512}}),
})


def encode_pack(snapshot,command):
    """Validate protected snapshot and inject only the explicit command digest.

    No model, embedding, semantic snapshot or policy revision is fabricated.
    User text stays data in its own section; formal facts contain actual object
    identities and revisions rather than treating user statements as facts.
    """
    body=__import__('copy').deepcopy(snapshot)
    body['bindings']['command_digest']=hashlib.sha256(canonical_payload(command_binding(command)).encode()).hexdigest()
    Draft202012Validator(PACK_SCHEMA,format_checker=Draft202012Validator.FORMAT_CHECKER).validate(body)
    if len({row['type'] for row in body['formal_facts']})!=4:raise ValueError('context_facts_invalid')
    for row in body['formal_facts']:
        if row['provenance']!='eios:object:'+row['id']:raise ValueError('context_provenance_invalid')
    if body['user_statement']['provenance']!='eios:object:'+body['user_statement']['message_id']:raise ValueError('context_provenance_invalid')
    if body['current_constraints']['reserved_units']>body['current_constraints']['budget_units']:raise ValueError('context_budget_invalid')
    for field in ('tenant_id','world_id','run_id'):
        if body['bindings'][field]!=command[field]:raise ValueError('context_command_invalid')
    text=canonical_payload(body)
    if len(text.encode())>MAX_PACK_BYTES:raise ValueError('context_pack_too_large')
    return text
