"""Narrow formal supply catalog. User statements never become entitlements.

Publication and CREATE use existing EIOS governed object Actions. Catalog READ
requires genuine source identity and object plus every projected property READ.
The companion append-only migration is the dispatch authority, not this parser.
"""
from datetime import UTC,datetime
from jsonschema import Draft202012Validator
from nexloop_eios.postgres_artifacts import canonical_payload

SERVICE_CODE='local.json-export'
OFFERING_FIELDS=('service_code','title','delivery_action','content_kind','price_amount','currency',
 'eligibility','allowed_guarantees','allowed_discounts','evidence_kind','active','valid_until')
BINDING_FIELDS=('consumer_id','offering_id','offering_revision','source_principal','active')


def offering_schemas():
    from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
    kinds={'ServiceOffering':{field:('boolean' if field=='active' else 'json' if field in ('allowed_guarantees','allowed_discounts') else 'string') for field in OFFERING_FIELDS},
     'ConsumerServiceOffering':{field:('integer' if field=='offering_revision' else 'boolean' if field=='active' else 'string') for field in BINDING_FIELDS}}
    return tuple(ObjectTypeDefinition(type_name=name,version=1,only_edit_via_actions=True,
      properties=tuple(PropertyDefinition(property_name=key,value_type=PropertyValueType(kind)) for key,kind in fields.items())) for name,fields in kinds.items())


def json_export_example(*,valid_until):
    """An explicit free example, not a pricing or permission default."""
    return {'service_code':SERVICE_CODE,'title':'本地 JSON 文本导出','delivery_action':'nexloop.service.request:1',
     'content_kind':'json-message-export','price_amount':'0','currency':'CNY','eligibility':'current_consumer_plan',
     'allowed_guarantees':[],'allowed_discounts':[],'evidence_kind':'fsynced_json_export','active':True,'valid_until':valid_until}


REQUEST_SCOPE_SCHEMA={'type':'object','properties':{'offering_id':{'type':'string','pattern':'^[a-f0-9]{64}$'},
 'offering_revision':{'type':'integer','minimum':1,'maximum':9007199254740991},
 'requested_guarantees':{'type':'array','items':{'type':'string','minLength':1,'maxLength':128},'maxItems':16,'uniqueItems':True},
 'requested_discounts':{'type':'array','items':{'type':'string','minLength':1,'maxLength':128},'maxItems':16,'uniqueItems':True}},
 'required':['offering_id','offering_revision','requested_guarantees','requested_discounts'],'additionalProperties':False}


def delivery_scope(offering):
    """Public structured scope; never reinterpret ordinary message text."""
    if type(offering) is not dict or set(offering)!=set(OFFERING_FIELDS):raise ValueError('offering_unavailable')
    if offering['service_code']!=SERVICE_CODE or offering['delivery_action']!='nexloop.service.request:1' or offering['content_kind']!='json-message-export' or offering['evidence_kind']!='fsynced_json_export':raise ValueError('offering_unavailable')
    if type(offering['active']) is not bool or not offering['active']:raise ValueError('offering_unavailable')
    if offering['eligibility']!='current_consumer_plan' or offering['price_amount']!='0' or offering['currency']!='CNY' or offering['allowed_guarantees']!=[] or offering['allowed_discounts']!=[]:raise ValueError('offering_unavailable')
    expiry=datetime.fromisoformat(offering['valid_until'].replace('Z','+00:00'))
    if expiry.tzinfo is None or expiry<=datetime.now(UTC):raise ValueError('offering_unavailable')
    return {'service_code':SERVICE_CODE,'deliverable':'固定私有目录内可核验的 JSON 文本导出文件',
     'price_amount':offering['price_amount'],'currency':offering['currency'],'guarantees':[],'discounts':[],
     'limitations':['不提供目录外保证或折扣','不代表第三方渠道送达、付款或问题解决'],'evidence_kind':offering['evidence_kind']}


def assess_request_scope(offering,request_scope,*,offering_id,revision):
    Draft202012Validator(REQUEST_SCOPE_SCHEMA).validate(request_scope)
    scope=delivery_scope(offering)
    if request_scope['offering_id']!=offering_id or type(revision) is not int or request_scope['offering_revision']!=revision:raise ValueError('offering_revision_conflict')
    authorized=request_scope['requested_guarantees']==[] and request_scope['requested_discounts']==[]
    return {'allowed':authorized,'reason':'within_catalog' if authorized else 'outside_catalog_terms','scope':scope}


def governed_create_catalog(maintainer,*,delivery_source,intent_id,consumer_id,valid_until):
    """Trusted provisioning: separate genuine authenticated service identities.

    The maintainer owns governed CREATE; delivery_source is the registered
    same-tenant recipient of READ/EXECUTE, never impersonated for CREATE.
    """
    from nexloop_eios.authorization import _identity
    from nexloop_eios.assembly import verify_application_role
    with delivery_source._backend._pool.connection() as db,db.transaction():
        verify_application_role(db)
        current=_identity(db,delivery_source._session.token_digest,delivery_source._session.world)
    auth=current.authentication;keeper=maintainer._session.authentication
    if (current.run_context is not None or current.world!='real'
        or maintainer._session.run_context is not None or maintainer._session.world!='real'
        or auth.tenant_id!=keeper.tenant_id or auth.subject_principal_id==keeper.subject_principal_id):
        raise ValueError('offering_unavailable')
    offered=maintainer.create_object(action_name='ServiceOffering.create',action_version=1,intent_id=intent_id+'-offering',
       type_name='ServiceOffering',properties=json_export_example(valid_until=valid_until))
    link=maintainer.create_object(action_name='ConsumerServiceOffering.create',action_version=1,intent_id=intent_id+'-binding',
       type_name='ConsumerServiceOffering',properties={'consumer_id':consumer_id,'offering_id':offered['object_id'],
       'offering_revision':1,'source_principal':auth.subject_principal_id,'active':True})
    return {'offering_id':offered['object_id'],'binding_id':link['object_id'],'consumer_id':consumer_id}


def read_catalog(source,*,offering_id,binding_id,consumer_id):
    """Actual object/property READ; results are not sufficient to dispatch."""
    link=source.read_object(type_name='ConsumerServiceOffering',object_id=binding_id,fields=BINDING_FIELDS)
    props=link['properties']
    if props!={'consumer_id':consumer_id,'offering_id':offering_id,'offering_revision':props.get('offering_revision'),
      'source_principal':source._session.authentication.subject_principal_id,'active':True}:raise ValueError('offering_unavailable')
    offering=source.read_object(type_name='ServiceOffering',object_id=offering_id,fields=OFFERING_FIELDS)
    if type(props['offering_revision']) is not int or offering['revision']!=props['offering_revision']:raise ValueError('offering_revision_conflict')
    return {'offering_id':offering_id,'revision':offering['revision'],'scope':delivery_scope(offering['properties']),
      'properties':offering['properties'],'provenance':'eios:object:'+offering_id}


def _read_envelope(source,type_name,object_id,fields):
    """Exact existing public signed READ protocol, evaluated from genuine source."""
    from nexloop_eios.object_reads import AuthorizedObjectReader
    from eios.authz.resources import ResourceType
    reader=AuthorizedObjectReader(source._backend._pool,source._session,source._backend._signer)
    claims=reader._authority(ResourceType.OBJECT,type_name+'/'+object_id)
    fields=tuple(sorted(fields))
    claims.update(protocol='nexloop-object-read-v1',key_id=reader.signer.key_id,type_name=type_name,object_id=object_id,
     fields=fields,property_authorities=[reader._authority(ResourceType.PROPERTY,type_name+'/'+object_id+'/'+field) for field in fields])
    import hmac
    text=canonical_payload(claims)
    signature=hmac.new(reader.signer.material,('nexloop-object-read-v1:'+text).encode(),'sha256').hexdigest()
    return {'text':text,'signature':signature}


def _catalog_envelope(source,*,offering_id,binding_id,consumer_id,request_scope):
    """Protected PG READ-only scope evaluation; never dispatches an effect.

    Consumer binding, offering revision and all READ proofs are rechecked in
    one transaction after locks. Runtime integration must reuse this same
    verifier inside its submit/send transaction; this standalone call is not
    a dispatch permit and is explicitly labeled as such.
    """
    import hashlib,hmac
    from nexloop_eios.assembly import verify_application_role
    if source._session.run_context is not None or source._session.world!='real':raise ValueError('offering_unavailable')
    Draft202012Validator(REQUEST_SCOPE_SCHEMA).validate(request_scope)
    if request_scope['offering_id']!=offering_id:raise ValueError('offering_unavailable')
    reads={'offering':_read_envelope(source,'ServiceOffering',offering_id,OFFERING_FIELDS),
      'binding':_read_envelope(source,'ConsumerServiceOffering',binding_id,BINDING_FIELDS)}
    body=canonical_payload({'offering_id':offering_id,'binding_id':binding_id,'consumer_id':consumer_id,'request_scope':request_scope,'reads':reads})
    signer=source._backend._signer
    claims=canonical_payload({'protocol':'nexloop-service-catalog-v1','key_id':signer.key_id,'parameters_digest':hashlib.sha256(body.encode()).hexdigest()})
    signature=hmac.new(signer.material,('nexloop-service-catalog-v1:'+claims).encode(),'sha256').hexdigest()
    return claims,signature,body


def governed_scope(source,**parameters):
    envelope=_catalog_envelope(source,**parameters)
    from nexloop_eios.assembly import verify_application_role
    with source._backend._lock:
        source._backend._assert_open()
        with source._backend._pool.connection() as db,db.transaction():
            verify_application_role(db)
            return db.execute('select authz.nexloop_service_catalog_scope(%s,%s,%s,%s,%s)',
              (source._session.token_digest,source._session.world,*envelope)).fetchone()[0]


def catalog_envelope_from_hint(pool,signer,world,hint,*,request_scope=None):
    """Private kernel helper; hint has already passed signed current Run chain.

    A lightweight holder transports a genuine `_identity` result to the shared
    reader. It creates no credential, grant, facts or Human/service conversion.
    """
    from types import SimpleNamespace
    from nexloop_eios.authorization import _identity
    from nexloop_eios.assembly import verify_application_role
    with pool.connection() as db,db.transaction():
        verify_application_role(db)
        source=_identity(db,hint['_source_digest'],world)
    supply=hint['supply']
    scope=request_scope if request_scope is not None else {'offering_id':supply['offering_id'],'offering_revision':supply['offering_revision'],'requested_guarantees':[],'requested_discounts':[]}
    holder=SimpleNamespace(_session=source,_backend=SimpleNamespace(_pool=pool,_signer=signer))
    return dict(zip(('text','signature','payload'),_catalog_envelope(holder,offering_id=supply['offering_id'],binding_id=supply['binding_id'],consumer_id=hint['consumer_id'],request_scope=scope)))


class CatalogScopeDenied(PermissionError):
    def __init__(self,scope):
        super().__init__('outside_catalog_terms')
        self.scope=scope


def preflight_scope(pool,world,hint,envelope):
    # Separate short READ transaction: no catalog locks survive into the
    # Plan/intent write transaction. Positive results are never dispatch grants;
    # the signed SQL write wrapper repeats all current locks/proofs at its tail.
    with pool.connection() as db,db.transaction():
        result=db.execute('select authz.nexloop_service_catalog_scope(%s,%s,%s,%s,%s)',
          (hint['_source_digest'],world,envelope['text'],envelope['signature'],envelope['payload'])).fetchone()[0]
    if result['allowed'] is False:
        scope=result['scope']
        if scope!=delivery_scope(result['supply']['properties']):raise ValueError('offering_unavailable')
        raise CatalogScopeDenied(scope)
    if result['allowed'] is not True or result['dispatch_permit'] is not False:raise ValueError('offering_unavailable')
