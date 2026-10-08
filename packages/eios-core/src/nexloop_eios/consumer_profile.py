"""Fresh-tenant profile declaration. Profile never grants dispatch authority.

Verified external identity and contact-control refs have no current adapter;
nonempty refs are explicitly refused rather than marking caller values verified.
"""
from zoneinfo import available_timezones,ZoneInfo
from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType,PropertyTypeDescriptor,PropertyTypeKind


def consumer_profile_schema():
    names=['display_name','locale','timezone','lifecycle_status']
    values=[PropertyDefinition(property_name=n,value_type=PropertyValueType('string')) for n in names]
    for name in ['external_id_refs','contact_preferences']:
        values.append(PropertyDefinition(property_name=name,value_type=PropertyValueType('json'),type_descriptor=PropertyTypeDescriptor(kind=PropertyTypeKind.ARRAY,element=PropertyTypeDescriptor(kind=PropertyTypeKind.STRING)),description='Reference-only profile; no dispatch authority. Nonempty values require a future verified authority adapter.'))
    return ObjectTypeDefinition(type_name='Consumer',version=1,only_edit_via_actions=True,properties=tuple(values))


def consumer_creation_schema():
    fields={n:{'type':'string','minLength':1,'maxLength':256} for n in ['display_name','locale','lifecycle_status']}
    fields['timezone']={'type':'string','enum':sorted(available_timezones())}
    fields.update({n:{'type':'array','maxItems':0,'items':{'type':'string'}} for n in ['external_id_refs','contact_preferences']})
    return {'type':'object','properties':{'request_id':{'type':'string','minLength':1},'type_name':{'const':'Consumer'},'properties':{'type':'object','properties':fields,'additionalProperties':False}},'required':['request_id','type_name','properties'],'additionalProperties':False}


def validated_profile(values):
    from jsonschema import Draft202012Validator
    payload={'request_id':'profile-validation','type_name':'Consumer','properties':values}
    Draft202012Validator(consumer_creation_schema()).validate(payload)
    if 'timezone' in values:
        name=values['timezone']
        if name not in available_timezones() or name.startswith('/') or '..' in name:raise ValueError('timezone must be a named IANA zone')
        ZoneInfo(name)
    return dict(values)
