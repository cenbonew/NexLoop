"""Typed restrictions for governed Role policies; never authorization grants."""
from decimal import Decimal
import re
from nexloop_eios.role_mapping import _validity

BUDGET_KEYS=('maximum_model_turns','maximum_tool_calls','active_timeout_seconds','maximum_cost','currency')
CEILING_FIELDS=('active','action_resources','consumer_ids','goal_ids','step_ids','budget','effect_units','valid_from','valid_until')
SCOPE_FIELDS=('active','role_id','consumer_id','goal_ids','step_ids','valid_from','valid_until')

def _ids(value,*,actions=False):
    if type(value) is not list or not 1<=len(value)<=32 or any(type(v) is not str or not 1<=len(v)<=512 for v in value) or len(set(value))!=len(value):raise ValueError('bounded unique explicit allowlist required')
    if actions and any(re.fullmatch(r'eios:action:[A-Za-z][A-Za-z0-9_.-]*:[1-9][0-9]*',v) is None for v in value):raise ValueError('exact Action resources required')
    if any(v=='*' for v in value):raise ValueError('wildcard unavailable')

def validate_budget(value):
    if type(value) is not dict or set(value)!=set(BUDGET_KEYS):raise ValueError('exact budget required')
    for key,maximum in [('maximum_model_turns',64),('maximum_tool_calls',128),('active_timeout_seconds',3600)]:
        if type(value[key]) is not int or not 1<=value[key]<=maximum:raise ValueError('bounded budget required')
    if type(value['maximum_cost']) is not str or re.fullmatch(r'\d+(?:\.\d{1,8})?',value['maximum_cost']) is None:raise ValueError('decimal budget required')
    if type(value['currency']) is not str or re.fullmatch('[A-Z]{3}',value['currency']) is None:raise ValueError('explicit currency required')

def validate_policy(kind,properties):
    fields=CEILING_FIELDS if kind=='RoleExecutionCeiling' else SCOPE_FIELDS if kind=='RoleAssignmentScope' else ()
    if not fields or type(properties) is not dict or set(properties)!=set(fields):raise ValueError('exact policy required')
    if type(properties['active']) is not bool:raise ValueError('explicit active required')
    _validity(properties['valid_from'],properties['valid_until'])
    for field in ('goal_ids','step_ids'):_ids(properties[field])
    if kind=='RoleExecutionCeiling':
        _ids(properties['action_resources'],actions=True);_ids(properties['consumer_ids']);validate_budget(properties['budget'])
        if type(properties['effect_units']) is not int or not 1<=properties['effect_units']<=1000000:raise ValueError('bounded effect units required')
    else:
        for field in ('role_id','consumer_id'):
            if type(properties[field]) is not str or not 1<=len(properties[field])<=512 or properties[field]=='*':raise ValueError('explicit scope endpoint required')
    return properties

def assert_budget_within(request,ceiling):
    validate_budget(request);validate_budget(ceiling)
    if request['currency']!=ceiling['currency'] or Decimal(request['maximum_cost'])>Decimal(ceiling['maximum_cost']):raise ValueError('Role cost ceiling exceeded')
    if any(request[k]>ceiling[k] for k in BUDGET_KEYS[:3]):raise ValueError('Role Run ceiling exceeded')


def role_policy_schemas():
    from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType as V
    output=[]
    for kind,fields in [('RoleExecutionCeiling',CEILING_FIELDS),('RoleAssignmentScope',SCOPE_FIELDS)]:
        def value_type(name):
            if name=='active':return V.BOOLEAN
            if name=='effect_units':return V.INTEGER
            if name in ('action_resources','consumer_ids','goal_ids','step_ids','budget'):return V.JSON
            return V.STRING
        output.append(ObjectTypeDefinition(type_name=kind,version=1,only_edit_via_actions=True,properties=tuple(PropertyDefinition(property_name=n,value_type=value_type(n),required=True) for n in fields)))
    return tuple(output)

class RolePolicyPort:
    def __init__(self,service):self.service=service
    def create(self,*,kind,intent_id,properties):
        validate_policy(kind,properties)
        return self.service.create_object(action_name=kind+'.create',action_version=1,intent_id=intent_id,type_name=kind,properties=properties)
    def edit(self,*,kind,intent_id,object_id,expected_revision,properties):
        validate_policy(kind,properties)
        return self.service.edit_object(action_name=kind+'.edit',action_version=1,intent_id=intent_id,type_name=kind,object_id=object_id,expected_revision=expected_revision,properties=properties)


def policy_envelope(source,*,run_id,role_id,link_id,consumer_id,goal_id,step_id,ceiling_id,scope_id,budget):
    """Signed own-Source READ recipe; the SQL verifier is execution authority."""
    import hashlib,hmac
    from nexloop_eios.service_offerings import _read_envelope
    from nexloop_eios.postgres_artifacts import canonical_payload
    if source._session.run_context is not None or source._session.world!='real':raise PermissionError('Source policy selection required')
    validate_budget(budget)
    reads={'ceiling':_read_envelope(source,'RoleExecutionCeiling',ceiling_id,CEILING_FIELDS),
           'scope':_read_envelope(source,'RoleAssignmentScope',scope_id,SCOPE_FIELDS)}
    payload=canonical_payload(dict(run_id=run_id,role_id=role_id,link_id=link_id,consumer_id=consumer_id,goal_id=goal_id,step_id=step_id,ceiling_id=ceiling_id,scope_id=scope_id,budget=budget,reads=reads))
    signer=source._backend._signer
    claims=canonical_payload(dict(protocol='nexloop-role-policy-v1',key_id=signer.key_id,parameters_digest=hashlib.sha256(payload.encode()).hexdigest()))
    signature=hmac.new(signer.material,('nexloop-role-policy-v1:'+claims).encode(),'sha256').hexdigest()
    return dict(text=claims,signature=signature,payload=payload)


def bind_policy_run(source,*,role_parameters,policy_parameters):
    from nexloop_eios.role_runs import role_binding_envelope
    from nexloop_eios.assembly import verify_application_role
    role=role_binding_envelope(source,**role_parameters)
    policy=policy_envelope(source,**policy_parameters)
    with source._backend._pool.connection() as db,db.transaction():
        verify_application_role(db)
        return db.execute('select authz.nexloop_role_policy_bind(%s,%s,%s,%s,%s,%s,%s,%s)',
            (source._session.token_digest,source._session.world,*role,policy['text'],policy['signature'],policy['payload'])).fetchone()[0]


def policy_envelope_for_run(pool,signer,world,run_digest):
    """Kernel-owned genuine Run resolver, not a caller-selected Source identity."""
    from types import SimpleNamespace
    from nexloop_eios.authorization import _identity
    from nexloop_eios.assembly import verify_application_role
    with pool.connection() as db,db.transaction():
        verify_application_role(db)
        hint=db.execute('select authz.nexloop_role_policy_hint(%s,%s)',(run_digest,world)).fetchone()[0]
        if hint is None:return None
        session=_identity(db,hint.pop('_source_digest'),world)
    holder=SimpleNamespace(_session=session,_backend=SimpleNamespace(_pool=pool,_signer=signer))
    return policy_envelope(holder,**hint)


def policy_envelope_for_role(pool,signer,session,role):
    """Own authenticated caller resolves only technical same-tenant Run refs.

    Reused only within one guarded request (role_runs.request_envelope_scope); SQL
    re-validates current policy, revision and deadline at every use.
    """
    from nexloop_eios.role_runs import scoped_envelope
    return scoped_envelope(('policy',id(pool),signer.key_id,session.world,role['signature']),
        lambda:_policy_envelope_for_role(pool,signer,session,role))


def _policy_envelope_for_role(pool,signer,session,role):
    import json
    from nexloop_eios.assembly import verify_application_role
    run_id=json.loads(role['payload'])['run_id']
    with pool.connection() as db,db.transaction():
        verify_application_role(db)
        digest=db.execute('select authz.nexloop_role_policy_run_digest(%s,%s,%s)',(session.token_digest,session.world,run_id)).fetchone()[0]
    return policy_envelope_for_run(pool,signer,session.world,digest)


def issue_role_run(source,*,action_resources,role_parameters,policy_parameters,ttl_seconds=300):
    """Atomic real Run issuance + restrictive Role policy binding, no auto-grant."""
    from datetime import datetime
    from nexloop_eios.run_credentials import _prepare_run_credential,RunCredential,AUDIENCE
    from nexloop_eios.role_runs import role_binding_envelope
    from nexloop_eios.assembly import verify_application_role
    run_id,text,signature=_prepare_run_credential(source._backend._pool,source._session,source._backend._signer,action_resources=action_resources,ttl_seconds=ttl_seconds)
    role=role_binding_envelope(source,run_id=run_id,**role_parameters)
    policy=policy_envelope(source,run_id=run_id,**policy_parameters)
    with source._backend._pool.connection() as db,db.transaction():
        if verify_application_role(db)!='nexloop_api':raise PermissionError('trusted API issuer required')
        result=db.execute('select authz.nexloop_issue_role_policy_run(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (source._session.token_digest,source._session.world,text,signature,*role,policy['text'],policy['signature'],policy['payload'])).fetchone()[0]
    return RunCredential(result['run_id'],AUDIENCE,datetime.fromisoformat(result['expires_at']),result['token'])
