"""Explicit technical administrator configuration, never a business Governor.

No synthetic identity/permission generation. Public manifests contain typed
contracts and declared authority facts. Secrets arrive through private files,
are hashed in memory, and never enter public manifests, summaries or logs.
The pending 0050 configurator definer is required for --apply; no table fallback.
"""
import argparse,hashlib,json,re,sys,uuid
from datetime import UTC,datetime
from pathlib import Path
import psycopg
from eios.authz import facts as F
from eios.ontology.models import ObjectTypeDefinition
from eios.ontology.definitions import ActionDefinition,FunctionDefinition
from eios.ontology.semantics import schema_contract_digest
from eios.ontology.version_resolution import CapabilityContractSnapshot,validate_capability_binding
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.private_configuration import read_private_text
from nexloop_eios.message_read import MessageReadRule
from nexloop_eios.property_access import PropertyAccessRule,PropertyGroupRestriction

FACT_MODELS={'subject':F.SubjectFacts,'membership':F.MembershipFacts,'actor':F.ActorFacts,
 'authentication':F.CredentialAuthenticationFacts,'application':F.ApplicationFacts,
 'subject_authority':F.SubjectAuthorityFacts,'resource_graph':F.ResourceGraphFacts,'grants':F.GrantFacts,
 'scope':F.ScopeAuthorityFacts,'controls':F.ControlFacts,'policies':F.PolicyFacts,'revision':F.RevisionSourceFacts,
 'message_read_rule':MessageReadRule,'property_access_rule':PropertyAccessRule,'property_group_restriction':PropertyGroupRestriction}
FIELDS={'schema_version','manifest_id','tenant_id','expected_revision','tenant_status','object_types','actions','functions','authority_facts','service_credentials','browser_applications','browser_business_applications','browser_rate_policies','identity_allowances'}

class ConfigurationRejected(RuntimeError):
    def __init__(self):super().__init__('configuration_rejected')


def strict_json(text):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ConfigurationRejected()
            result[key]=value
        return result
    return json.loads(text,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ConfigurationRejected()))


def validate_manifest(value):
    try:
        if type(value) is not dict or set(value)!=FIELDS or value['schema_version']!='1.0':raise ValueError()
        uuid.UUID(value['manifest_id'])
        tenant=value['tenant_id']
        if type(tenant) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}',tenant) is None:raise ValueError()
        if type(value['expected_revision']) is not int or value['expected_revision']<0 or value['tenant_status'] not in ('active','suspended'):raise ValueError()
        if any(type(value[k]) is not list or len(value[k])>4096 for k in ('object_types','actions','functions','authority_facts','service_credentials','browser_applications','browser_business_applications','browser_rate_policies','identity_allowances')):raise ValueError()
        out={**value};refs={};seen=set();out['object_types']=[]
        for item in value['object_types']:
            definition=ObjectTypeDefinition.model_validate_json(canonical_payload(item));key=(definition.type_name,definition.version)
            if key in seen:raise ValueError()
            seen.add(key);refs[key]=schema_contract_digest(definition);out['object_types'].append(definition.model_dump(mode='json'))
        for category,model,kind in [('actions',ActionDefinition,'action'),('functions',FunctionDefinition,'function')]:
            out[category]=[]
            for item in value[category]:
                if type(item) is not dict or set(item)!={'definition','capability'}:raise ValueError()
                definition=model.model_validate_json(canonical_payload(item['definition']))
                capability=CapabilityContractSnapshot.model_validate_json(canonical_payload(item['capability']))
                validate_capability_binding(definition,capability)
                key=(kind,definition.stable_name,definition.version)
                if key in seen or definition.tenant_id!=tenant or definition.status.value!='published':raise ValueError()
                seen.add(key)
                if kind=='action':references=set(definition.object_types)|set(definition.governance.change_scope.object_types)
                else:
                    if definition.property_dependencies or definition.link_dependencies or definition.required_markings:raise ValueError()
                    references={r.object_type for r in definition.applies_to}
                    if None in references:raise ValueError()
                if any(r.tenant_id!=tenant or r.schema_type!='object_type' or refs.get((r.stable_name,r.version))!=r.schema_digest for r in references):raise ValueError()
                out[category].append({'definition':definition.model_dump(mode='json'),'capability':capability.model_dump(mode='json')})
        out['authority_facts']=[]
        for item in value['authority_facts']:
            if type(item) is not dict or set(item)!={'kind','key','payload'} or item['kind'] not in FACT_MODELS:raise ValueError()
            if type(item['key']) is not list or not 1<=len(item['key'])<=3 or any(type(k) is not str or not 0<len(k)<=320 for k in item['key']):raise ValueError()
            fact=FACT_MODELS[item['kind']].model_validate_json(canonical_payload(item['payload']))
            if fact.tenant_id!=tenant:raise ValueError()
            key=('fact',item['kind'],tuple(item['key']))
            if key in seen:raise ValueError()
            seen.add(key);out['authority_facts'].append({'kind':item['kind'],'key':item['key'],'payload':fact.model_dump(mode='json')})
        out['service_credentials']=[]
        for item in value['service_credentials']:
            if type(item) is not dict or set(item)!={'reference','binding','worlds','expires_at','status'}:raise ValueError()
            binding=F.CredentialAuthenticationBinding.model_validate_json(canonical_payload(item['binding']))
            if binding.tenant_id!=tenant or binding.credential_tenant_id!=tenant or binding.subject_kind.value not in ('service','agent') or binding.credential_kind!='api_key':raise ValueError()
            if type(item['reference']) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}',item['reference']) is None:raise ValueError()
            if item['reference'] in seen or ('credential',binding.credential_id) in seen:raise ValueError()
            seen.update([item['reference'],('credential',binding.credential_id)])
            if type(item['worlds']) is not list or not 1<=len(item['worlds'])<=16 or len(set(item['worlds']))!=len(item['worlds']) or any(type(w) is not str or not w or len(w)>80 for w in item['worlds']):raise ValueError()
            expiry=datetime.fromisoformat(item['expires_at'])
            if expiry.tzinfo is None or expiry<=datetime.now(UTC) or item['status'] not in ('active','revoked'):raise ValueError()
            out['service_credentials'].append({**item,'binding':binding.model_dump(mode='json')})
        for category,required in [('browser_applications',{'application_id','active'}),('browser_business_applications',{'application_id','caller_application_id','application_version','requested_scopes'}),('browser_rate_policies',{'action','operator_principal_id','maximum_attempts','window_seconds','active'}),('identity_allowances',{'application_id','enabled','maximum_accounts','operator_label','idempotency_key_digest'})]:
            out[category]=[]
            for item in value[category]:
                if type(item) is not dict or set(item)!=required:raise ValueError()
                for key in required-{'active','enabled','maximum_accounts','maximum_attempts','window_seconds','requested_scopes'}:
                    if type(item[key]) is not str or not 0<len(item[key])<=320 or item[key]!=item[key].strip():raise ValueError()
                if 'active' in item and type(item['active']) is not bool or 'enabled' in item and type(item['enabled']) is not bool:raise ValueError()
                for key,maximum in [('maximum_attempts',1000000),('window_seconds',3600),('maximum_accounts',100)]:
                    if key in item and (type(item[key]) is not int or not 1<=item[key]<=maximum):raise ValueError()
                if category=='browser_business_applications':
                    scopes=item['requested_scopes']
                    if not item['caller_application_id'].startswith('eios:application:') or type(scopes) is not list or not 1<=len(scopes)<=64 or len(set(scopes))!=len(scopes) or any(type(x) is not str or not x or len(x)>160 for x in scopes):raise ValueError()
                if category=='identity_allowances' and re.fullmatch('[0-9a-f]{64}',item['idempotency_key_digest']) is None:raise ValueError()
                if category=='browser_rate_policies' and item['operator_principal_id']!='nexloop_identity':raise ValueError()
                key=(category,item.get('application_id',item.get('action')))
                if key in seen:raise ValueError()
                seen.add(key);out[category].append(item)
        return out
    except Exception:raise ConfigurationRejected() from None


def check_manifest(path):
    try:
        with Path(path).open() as stream:raw=stream.read(16777217)
        if len(raw.encode())>16777216:raise ValueError()
        return validate_manifest(strict_json(raw))
    except Exception:raise ConfigurationRejected() from None


def apply_manifest(manifest,*,database_url_file,signing_key_file,signing_key_id,service_secrets_file):
    try:secrets=strict_json(read_private_text(service_secrets_file,maximum=1048576))
    except Exception:raise ConfigurationRejected() from None
    return apply_manifest_with_secrets(manifest,database_url_file=database_url_file,signing_key_file=signing_key_file,
        signing_key_id=signing_key_id,service_secrets=secrets)


def configurator_connection(database_url_file):
    """Technical configurator only; refuses superuser/BYPASSRLS/owner-member sessions."""
    db=psycopg.connect(read_private_text(database_url_file,maximum=16384),connect_timeout=5)
    try:
        role=db.execute('''select current_user,session_user,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication,
            exists(select 1 from pg_roles elevated where elevated.rolname<>current_user
             and (elevated.rolsuper or elevated.rolbypassrls or elevated.rolcreatedb or elevated.rolcreaterole or elevated.rolreplication or elevated.rolname='nexloop_owner')
             and pg_has_role(session_user,elevated.oid,'MEMBER')) from pg_roles where rolname=current_user''').fetchone()
        if not role or role[:2]!=('nexloop_configurator','nexloop_configurator') or any(role[2:]):raise ValueError()
        db.rollback()
        return db
    except Exception:
        db.close();raise


def apply_manifest_with_secrets(manifest,*,database_url_file,signing_key_file,signing_key_id,service_secrets):
    """Same as apply_manifest; `service_secrets` is an in-memory reference->secret map."""
    try:
        manifest=validate_manifest(manifest)
        secrets=service_secrets
        references={r['reference'] for r in manifest['service_credentials']}
        if type(secrets) is not dict or set(secrets)!=references or any(type(s) is not str or len(s)<32 or len(s)>4096 or s!=s.strip() for s in secrets.values()):raise ValueError()
        key=bytes.fromhex(read_private_text(signing_key_file,maximum=128))
        if len(key)!=32 or type(signing_key_id) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}',signing_key_id) is None:raise ValueError()
        credential_digests={ref:hashlib.sha256(secret.encode()).hexdigest() for ref,secret in secrets.items()}
        text=canonical_payload(manifest);digest=hashlib.sha256(text.encode()).hexdigest()
        with psycopg.connect(read_private_text(database_url_file,maximum=16384),connect_timeout=5) as db,db.transaction():
            db.execute("set local statement_timeout='10000ms'; set local lock_timeout='3000ms'")
            role=db.execute('''select current_user,session_user,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication,
                exists(select 1 from pg_roles elevated where elevated.rolname<>current_user
                 and (elevated.rolsuper or elevated.rolbypassrls or elevated.rolcreatedb or elevated.rolcreaterole or elevated.rolreplication or elevated.rolname='nexloop_owner')
                 and pg_has_role(session_user,elevated.oid,'MEMBER')) from pg_roles where rolname=current_user''').fetchone()
            if not role or role[:2]!=('nexloop_configurator','nexloop_configurator') or any(role[2:]):raise ValueError()
            result=db.execute('select control.nexloop_configure_manifest(%s,%s,%s,%s,%s,%s::jsonb)',
                (manifest['tenant_id'],text,digest,signing_key_id,key,canonical_payload(credential_digests))).fetchone()[0]
        if type(result) is not dict or set(result)!={'configured','manifest_digest','authority_revision'} or result['configured'] is not True or result['manifest_digest']!=digest or type(result['authority_revision']) is not int:raise ValueError()
        return result
    except Exception:raise ConfigurationRejected() from None


def main():
    p=argparse.ArgumentParser(description='Explicit technical configuration administrator; no business object creation')
    p.add_argument('--manifest',type=Path,required=True);mode=p.add_mutually_exclusive_group(required=True);mode.add_argument('--check',action='store_true');mode.add_argument('--apply',action='store_true')
    p.add_argument('--database-url-file',type=Path);p.add_argument('--signing-key-file',type=Path);p.add_argument('--signing-key-id');p.add_argument('--service-secrets-file',type=Path)
    a=p.parse_args()
    try:
        manifest=check_manifest(a.manifest)
        if a.apply:
            if any(v is None for v in (a.database_url_file,a.signing_key_file,a.signing_key_id,a.service_secrets_file)):raise ConfigurationRejected()
            result=apply_manifest(manifest,database_url_file=a.database_url_file,signing_key_file=a.signing_key_file,signing_key_id=a.signing_key_id,service_secrets_file=a.service_secrets_file)
        else:result={'checked':True,'manifest_digest':hashlib.sha256(canonical_payload(manifest).encode()).hexdigest(),'contract_count':len(manifest['actions'])+len(manifest['functions']),'authority_fact_count':len(manifest['authority_facts'])}
        print(canonical_payload(result));return 0
    except ConfigurationRejected:print('configuration_rejected',file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
