"""Independent explicit Human-message Context wire v4; v1/v2/v3 unchanged."""
import copy,hashlib
from jsonschema import Draft202012Validator
from nexloop_eios.context_pack import PACK_SCHEMA,MAX_PACK_BYTES,command_binding,_object_schema,ID,REV
from nexloop_eios.postgres_artifacts import canonical_payload
REF={'type':'string','pattern':'^eios:object:[A-Za-z][A-Za-z0-9_]*/[a-f0-9]{64}$'}
ITEM=_object_schema({'assessment_ref':{'type':'string','pattern':'^eios:object:RelationshipAssessment/[a-f0-9]{64}$'},'revision':REV,'relation_type_ref':{'type':['string','null'],'pattern':'^eios:link_type:[A-Za-z][A-Za-z0-9_]*:[1-9][0-9]*$'},'source_ref':REF,'target_ref':REF,'epistemic_kind':{'enum':['hypothesis','user_statement']},'resolution_state':{'enum':['resolved','awaiting_definition','unresolved']},'conclusion':{'type':'string','minLength':1,'maxLength':8192},'valid_from':{'type':'string','format':'date-time'},'valid_to':{'type':['string','null'],'format':'date-time'},'source_message_ref':{'type':['string','null'],'pattern':'^eios:object:Message/[a-f0-9]{64}$'},'source_content_hash':{'type':['string','null'],'pattern':'^[a-f0-9]{64}$'}})
PACK_SCHEMA_V4=copy.deepcopy(PACK_SCHEMA)
PACK_SCHEMA_V4['properties']['schema_version']={'const':'nexloop.context-pack.v4'}
PACK_SCHEMA_V4['properties']['relationship_context']=_object_schema({'current_statements':{'type':'array','items':ITEM,'maxItems':4},'evidence':{'type':'array','items':ITEM,'maxItems':4}})
PACK_SCHEMA_V4['required'].append('relationship_context')
def encode_pack(snapshot,command):
    body=copy.deepcopy(snapshot);body['bindings']['command_digest']=hashlib.sha256(canonical_payload(command_binding(command)).encode()).hexdigest()
    Draft202012Validator(PACK_SCHEMA_V4,format_checker=Draft202012Validator.FORMAT_CHECKER).validate(body)
    base={k:v for k,v in body.items() if k!='relationship_context'};base['schema_version']='nexloop.context-pack.v2'
    from nexloop_eios.context_pack import encode_pack as encode_base
    encode_base(base,command)
    zone=body['relationship_context'];rows=zone['current_statements']+zone['evidence']
    if not 1<=len(rows)<=4 or len({r['assessment_ref'] for r in rows})!=len(rows):raise ValueError('relationship_context_ids_invalid')
    for item in zone['current_statements']:
        if item['epistemic_kind']!='user_statement' or item['resolution_state']!='resolved' or not item['relation_type_ref'] or not item['source_message_ref'] or not item['source_content_hash']:raise ValueError('relationship_statement_invalid')
    for item in rows:
        if item['epistemic_kind']=='hypothesis' and (item['source_message_ref'] is not None or item['source_content_hash'] is not None):raise ValueError('hypothesis_provenance_invalid')
    text=canonical_payload(body)
    if len(text.encode())>MAX_PACK_BYTES:raise ValueError('context_pack_too_large')
    return text
