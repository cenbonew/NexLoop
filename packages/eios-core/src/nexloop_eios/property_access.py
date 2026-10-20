"""Governed derivation of a service principal's per-object property READ/EDIT.

docs/implementation/property-grant-derivation.md (approved 2026-10-09). Trusted
configuration publishes one ``property_access_rule`` per (service principal, type)
and the owner publishes ``property_group_restriction`` per type. At each use SQL
re-derives READ/EDIT on ``Type/<oid>``, ``Type/<oid>/<property>`` and READ on
``Type/<property>`` from the rule, the restriction, the object's current schema
version, the property's group and (for properties added after the rule) the human
review decision that published it. Configured grants of the same principal always
win. This module only builds the typed proof; it grants nothing itself.
"""
from datetime import UTC,datetime,timedelta
from nexloop_eios.signing import SIGNED_COMMAND_TTL
import re
from typing import Literal

from pydantic import AwareDatetime,BaseModel,ConfigDict,Field,field_validator

from nexloop_eios.assembly import verify_application_role

DERIVATION='type-property-v1'
_GROUP=re.compile(r'[a-z][a-z0-9_]{0,63}')
_TYPE=re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}')


class PropertyAccessRule(BaseModel):
    """Trusted-configuration ``property_access_rule`` payload (key: [principal_id, type_name])."""
    model_config=ConfigDict(extra='forbid',frozen=True)
    tenant_id:str
    principal_id:str
    type_name:str
    operations:tuple[Literal['read','edit'],...]=Field(min_length=1,max_length=2)
    property_groups:tuple[str,...]=Field(min_length=1,max_length=32)
    include_review_published:bool
    basis_schema_version:int=Field(ge=1)
    active:bool
    valid_until:AwareDatetime

    @field_validator('type_name')
    @classmethod
    def _type(cls,value):
        if not _TYPE.fullmatch(value):raise ValueError('type_name')
        return value

    @field_validator('operations')
    @classmethod
    def _operations(cls,value):
        if len(set(value))!=len(value) or list(value)!=sorted(value):raise ValueError('operations must be sorted and unique')
        return value

    @field_validator('property_groups')
    @classmethod
    def _groups(cls,value):
        if any(not _GROUP.fullmatch(g) for g in value) or len(set(value))!=len(value) or list(value)!=sorted(value):
            raise ValueError('property_groups must be sorted unique identifiers')
        return value


class PropertyGroupRestriction(BaseModel):
    """Owner-decided ``property_group_restriction`` payload (key: [type_name]); no rule overrides it."""
    model_config=ConfigDict(extra='forbid',frozen=True)
    tenant_id:str
    type_name:str
    restricted_groups:tuple[str,...]=Field(max_length=32)
    decision:str=Field(min_length=1,max_length=500)

    @field_validator('restricted_groups')
    @classmethod
    def _groups(cls,value):
        if any(not _GROUP.fullmatch(g) for g in value) or len(set(value))!=len(value) or list(value)!=sorted(value):
            raise ValueError('restricted_groups must be sorted unique identifiers')
        return value


def property_access_basis(pool,session,resource_id,operation):
    """Which path applies: {'mode':'configured'} or {'mode':'derived',...basis}. Grants nothing."""
    with pool.connection() as db,db.transaction():
        verify_application_role(db)
        return db.execute('select authz.nexloop_property_access_basis(%s,%s,%s,%s)',
            (session.token_digest,session.world,resource_id,operation)).fetchone()[0]


def derived_claims(session,resource_id,operation,basis,type_read):
    """Typed derived proof with the same outer shape as a configured authority proof."""
    authentication=session.authentication
    expires=min(datetime.fromisoformat(basis['rule_valid_until']),datetime.fromisoformat(type_read['expires_at']),
        datetime.now(UTC)+SIGNED_COMMAND_TTL).isoformat()
    return {'tenant_id':authentication.tenant_id,'principal_id':authentication.subject_principal_id,
        'credential_id':authentication.credential_id,'directory_hash':session.directory_hash,'world':session.world,
        'resource_id':resource_id,'target_resource':resource_id,'operation':operation,'expires_at':expires,'facts':[],
        'derivation':DERIVATION,'derivation_basis':{k:basis[k] for k in ('rule_hash','restriction_hash','type_name','schema_version','property_group','review_decision_id')}
            |{'type_read':type_read}}


RELAY_SOURCE_DERIVATION='relay-source-v1'


class RelaySourceReadRule(BaseModel):
    """Trusted-configuration ``relay_source_read_rule`` payload (key: [source principal]; 0161).

    The Source principal that prepares the message Contexts of ``relay_principal``. SQL derives, at each read, READ on the
    Consumer, ServiceOffering and ConsumerServiceOffering of consumers actively enrolled for that relay (0160) whose binding
    names this Source; nothing per object is configured."""
    model_config=ConfigDict(extra='forbid',frozen=True)
    tenant_id:str
    principal_id:str=Field(min_length=1,max_length=320)
    relay_principal:str=Field(min_length=1,max_length=320)
    active:bool
    valid_until:AwareDatetime


def relay_source_claims(session,resource_id,basis):
    """Typed relay-source proof (READ only; no nested type READ): SQL re-derives it at every use."""
    authentication=session.authentication
    expires=min(datetime.fromisoformat(basis['rule_valid_until']),datetime.now(UTC)+SIGNED_COMMAND_TTL).isoformat()
    return {'tenant_id':authentication.tenant_id,'principal_id':authentication.subject_principal_id,
        'credential_id':authentication.credential_id,'directory_hash':session.directory_hash,'world':session.world,
        'resource_id':resource_id,'target_resource':resource_id,'operation':'read','expires_at':expires,'facts':[],
        'derivation':RELAY_SOURCE_DERIVATION,
        'derivation_basis':{k:basis[k] for k in ('rule_hash','type_name','consumer_id','enrollment_revision','restriction_hash')}}


ROLE_SOURCE_DERIVATION='role-source-v1'


class RoleSourceReadRule(BaseModel):
    """Trusted-configuration ``role_source_read_rule`` payload (key: [source principal]; 0211, G-ROLE-READ).

    The Source principal of plan reevaluation Role Runs. SQL derives, at each read, READ on the objects of a current, active
    plan whose PlanStep lists this Source as a submitter (the recipe's step, role, link, ceiling, scope, control, offering and
    binding, the plan's Consumer and the step's Goal), fixed properties only; nothing per object is configured."""
    model_config=ConfigDict(extra='forbid',frozen=True)
    tenant_id:str
    principal_id:str=Field(min_length=1,max_length=320)
    active:bool
    valid_until:AwareDatetime


def role_source_claims(session,resource_id,basis):
    """Typed role-source proof (READ only; no nested type READ): SQL re-derives it at every use."""
    authentication=session.authentication
    expires=min(datetime.fromisoformat(basis['rule_valid_until']),datetime.now(UTC)+SIGNED_COMMAND_TTL).isoformat()
    return {'tenant_id':authentication.tenant_id,'principal_id':authentication.subject_principal_id,
        'credential_id':authentication.credential_id,'directory_hash':session.directory_hash,'world':session.world,
        'resource_id':resource_id,'target_resource':resource_id,'operation':'read','expires_at':expires,'facts':[],
        'derivation':ROLE_SOURCE_DERIVATION,
        'derivation_basis':{k:basis[k] for k in ('rule_hash','type_name','consumer_id','plan_id','plan_version','restriction_hash')}}
