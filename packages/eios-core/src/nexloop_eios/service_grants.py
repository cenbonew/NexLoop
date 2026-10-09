"""NX-048 / ADR-020 §3: versioned service-principal grants via trusted configuration.

The public manifest (deploy/authorization/service-grants.v<N>.json) names, for each
NexLoop service principal, the Action/queue authority it needs, why, and which task
introduced it. This module compiles it into ordinary EIOS authority facts and writes
them only through the 0050 configurator definer (`control.nexloop_configure_manifest`)
under the `nexloop_configurator` role: no direct table writes, no superuser runtime
connection. Facts are deterministic, so re-applying an unchanged manifest writes
nothing. `doctor` diffs current service authority against the manifest and reports
grants outside it.

Policy exclusions (ADR-020 §3) reject the whole manifest: Human principals,
`ontology.schema.review`, real outbound channel effects and REAL_DISPATCH_ENABLED.
"""
import argparse,hashlib,json,re,sys,uuid
from datetime import UTC,datetime
from pathlib import Path

from eios.authz import facts as F
from eios.authz.applications import ApplicationMode,OperationRestriction,ResourceRestriction
from eios.authz.grants import GrantSubjectKind
from eios.authz.operations import Operation
from eios.authz.policy.canonical import canonicalize_policy_expression
from eios.authz.policy.models import ActionParameterRef,Eq,LiteralValue
from eios.authz.policy.registry import PolicyEffect
from eios.authz.resources import ResourceReference,ResourceType
from eios.identity.models import MembershipKind,SubjectKind
from nexloop_eios.authorization import WITNESS
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.private_configuration import read_private_text
from nexloop_eios.trusted_configuration import (FACT_MODELS,ConfigurationRejected,apply_manifest_with_secrets,
    configurator_connection,strict_json)

SCHEMA='nexloop-service-grants/1'
TOP={'schema_version','manifest_version','decision','valid_from','world','principals','grants','deferred','excluded_by_policy'}
PRINCIPAL={'role','subject_id','principal_id','credential_id','credential_reference','application_id','database_role','purpose','source_task'}
GRANT={'principal','resource_type','resource_id','operations','purpose','source_task'}
DATABASE_ROLES={'nexloop_api','nexloop_domain_worker','nexloop_action_worker','nexloop_scheduler'}
# Resource type -> the only operation a service principal may hold on it.
ALLOWED={'action':('execute',re.compile(r'eios:action:[A-Za-z0-9][A-Za-z0-9._-]{0,159}:[1-9][0-9]{0,8}')),
    'object_type':('read',re.compile(r'eios:object_type:[A-Z][A-Za-z0-9_]{0,63}'))}
# ADR-020 §3 boundary: never granted by this path, whatever the manifest says.
FORBIDDEN_MARKERS=('schema.review','real_dispatch','nexloop.service.request','nexloop.service.query',
    'nexloop.service.receipt_reconcile','nexloop.receipt.reconcile','channel.enable','external_effect')
ORDER=('subject','membership','actor','authentication','application','subject_authority','resource_graph','grants','scope','controls','policies','revision')
_ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}')
_ROLE=re.compile(r'[a-z][a-z0-9_]{0,63}')
NAMESPACE=uuid.UUID('9b7f3c52-2f43-4b0e-8f0e-4e5800480048')


class ServiceGrantsRejected(RuntimeError):
    def __init__(self,reason):
        super().__init__(reason);self.reason=reason


def _reject(reason):raise ServiceGrantsRejected(reason)


def validate(value):
    """Structural and policy validation of the public manifest (no I/O)."""
    if type(value) is not dict or set(value)!=TOP or value['schema_version']!=SCHEMA:_reject('manifest_shape')
    if type(value['manifest_version']) is not int or value['manifest_version']<1:_reject('manifest_version')
    if type(value['world']) is not str or re.fullmatch(r'[a-z][a-z0-9_-]{0,79}',value['world']) is None:_reject('world')
    try:valid_from=datetime.fromisoformat(value['valid_from'])
    except Exception:_reject('valid_from')
    if valid_from.tzinfo is None or valid_from.utcoffset().total_seconds()!=0:_reject('valid_from')
    for key in ('principals','grants','deferred','excluded_by_policy'):
        if type(value[key]) is not list or len(value[key])>512:_reject('manifest_shape')
    # Policy exclusions apply to everything that would become authority.
    authority_text=canonical_payload({'principals':value['principals'],'grants':value['grants']}).lower()
    for marker in FORBIDDEN_MARKERS:
        if marker in authority_text:_reject('excluded_by_policy:'+marker)
    principals={};identifiers=set()
    for item in value['principals']:
        if type(item) is not dict or set(item)!=PRINCIPAL:_reject('principal_shape')
        if any(type(item[k]) is not str or not item[k] for k in PRINCIPAL):_reject('principal_shape')
        if _ROLE.fullmatch(item['role']) is None or item['role'] in principals:_reject('principal_role')
        for k in ('subject_id','principal_id','credential_id','credential_reference'):
            if _ID.fullmatch(item[k]) is None or item[k] in identifiers:_reject('principal_identifier')
            identifiers.add(item[k])
        if re.fullmatch(r'eios:application:[A-Za-z0-9][A-Za-z0-9._-]{0,120}',item['application_id']) is None:_reject('principal_application')
        if item['database_role'] not in DATABASE_ROLES:_reject('principal_database_role')
        principals[item['role']]=item
    seen=set()
    for item in value['grants']:
        if type(item) is not dict or set(item)!=GRANT or item['principal'] not in principals:_reject('grant_shape')
        if any(type(item[k]) is not str or not item[k] for k in GRANT-{'operations'}):_reject('grant_shape')
        allowed=ALLOWED.get(item['resource_type'])
        if allowed is None:_reject('excluded_by_policy:resource_type')
        operation,pattern=allowed
        if pattern.fullmatch(item['resource_id']) is None:_reject('grant_resource')
        if item['operations']!=[operation]:_reject('excluded_by_policy:operation')
        key=(item['principal'],item['resource_id'])
        if key in seen:_reject('grant_duplicate')
        seen.add(key)
    for role in principals:
        if not any(g['principal']==role for g in value['grants']):_reject('principal_without_grant')
    return value


def load(path):
    try:
        raw=Path(path).read_text()
        if len(raw.encode())>4194304:raise ValueError()
        value=strict_json(raw)
    except Exception:raise ServiceGrantsRejected('manifest_unreadable') from None
    return validate(value)


def manifest_digest(manifest):return hashlib.sha256(canonical_payload(manifest).encode()).hexdigest()


def expected_grants(manifest):
    roles={p['role']:p for p in manifest['principals']}
    return {(roles[g['principal']]['principal_id'],g['resource_id'],op) for g in manifest['grants'] for op in g['operations']}


def _digest(value):return F.canonical_authority_digest(value)


def compile_principal(manifest,tenant,principal,*,credential_expires_at):
    """Deterministic EIOS authority facts and credential binding for one service principal."""
    world=manifest['world'];past=datetime.fromisoformat(manifest['valid_from'])
    grants=sorted((g for g in manifest['grants'] if g['principal']==principal['role']),key=lambda g:g['resource_id'])
    scopes=frozenset(f"{g['resource_type']}.{op}" for g in grants for op in g['operations'])
    subject,pid,app=principal['subject_id'],principal['principal_id'],principal['application_id']
    base=dict(tenant_id=tenant,repository_witness=WITNESS)
    version_digest=_digest({'schema':SCHEMA,'application':app,'version':'1','principal':pid,'world':world})
    role_id='nexloop-service-role:'+principal['role'];role_digest=_digest({'role':role_id})
    binding=F.CredentialAuthenticationBinding(tenant_id=tenant,credential_tenant_id=tenant,credential_id=principal['credential_id'],
        subject_id=subject,subject_kind=SubjectKind.SERVICE,subject_principal_id=pid,subject_revision=1,membership_revision=1,
        credential_revision=1,credential_epoch=1,caller_application_id=app,caller_application_version='1',
        caller_application_digest=version_digest,requested_scopes=scopes,credential_kind='api_key')
    facts=[
        ('subject',[subject],F.SubjectFacts(**base,subject_id=subject,kind=SubjectKind.SERVICE,status='active',revision=1)),
        ('membership',[subject,pid],F.MembershipFacts(**base,subject_id=subject,principal_id=pid,kind=MembershipKind.HOME,status='active',valid_from=past,valid_until=None,revision=1)),
        ('actor',[pid],F.ActorFacts(**base,actor_id=subject,actor_principal_id=pid,kind=SubjectKind.SERVICE,status='active',revision=1)),
        ('authentication',[principal['credential_id']],F.CredentialAuthenticationFacts(**binding.model_dump(),status='active',expires_at=credential_expires_at,repository_witness=WITNESS)),
        ('application',[app,'1'],F.ApplicationFacts(**base,application_id=app,application_revision=1,version='1',version_revision=1,
            version_digest=version_digest,record_digest=version_digest,application_status='active',version_status='published',eligible=True,
            mode=ApplicationMode.RESTRICTED,break_glass=False,
            resources=tuple(ResourceRestriction(tenant_id=tenant,resource_type=g['resource_type'],resource_id=g['resource_id']) for g in grants),
            operations=tuple(OperationRestriction(operation=Operation(op)) for op in sorted({op for g in grants for op in g['operations']})),
            published_at=past,revoked_at=None,expires_at=None)),
        ('subject_authority',[pid],F.SubjectAuthorityFacts(**base,subject_kind=GrantSubjectKind.SERVICE,principal_id=pid,groups=(),
            roles=(F.RevisionedAuthority(authority_id=role_id,revision=1,digest=role_digest),),attribute_revisions=(),clearances=(),revision=1,valid_until=None)),
    ]
    clearance=F.SubjectClearanceFact(clearance_id='service-clearance',subject_kind=GrantSubjectKind.SERVICE,principal_id=pid,
        kind=F.ControlKind.ORGANIZATION,namespace='nexloop',value='internal',rank=0,dominates=frozenset(),revision=1,
        digest=_digest({'clearance':'internal'}),valid_until=None)
    condition=Eq(op='eq',node_id='world-boundary',left=ActionParameterRef(kind='action_parameter',parameter_name='world'),right=LiteralValue(kind='literal',value=world))
    canonical=canonicalize_policy_expression(condition)
    for g in grants:
        kind=ResourceType(g['resource_type']);rid=g['resource_id']
        ref=ResourceReference(tenant_id=tenant,resource_id=rid,resource_type=kind,security_revision=1)
        ops=frozenset(Operation(op) for op in g['operations'])
        facts.append(('resource_graph',[rid],F.ResourceGraphFacts(**base,root=F.ResourceNodeFact(resource=ref,parent=None,active=True),ancestors=(),dependencies=(),
            registry_revision=1,registry_digest=_digest(ref),closure_complete=True,next_cursor=None)))
        grant=F.EffectiveGrantFact(grant_id=f"{principal['role']}:{rid}",subject_kind=GrantSubjectKind.SERVICE,subject_id=pid,role_id=role_id,
            role_revision=1,role_digest=role_digest,resource=ref,operations=ops,inherited=False,valid_from=past,valid_until=None,revision=1,
            digest=_digest({'grant':rid,'principal':pid,'operations':sorted(op.value for op in ops)}))
        facts.append(('grants',[pid,rid],F.GrantFacts(**base,subject_kind=GrantSubjectKind.SERVICE,principal_id=pid,grants=(grant,),valid_until=None,complete=True,next_cursor=None,revision=1)))
        for op in sorted(ops,key=lambda o:o.value):
            scope=f'{kind.value}.{op.value}'
            facts.append(('scope',[pid,rid,op.value],F.ScopeAuthorityFacts(**base,principal_id=pid,binding_id=principal['role']+'-route',catalog_id='nexloop-scopes',
                catalog_version='1',catalog_revision=1,catalog_digest=_digest({'scope':scope}),catalog_scopes=scopes,required_scopes=frozenset({scope}),
                authorities=(F.ScopeAuthority(scope=scope,resource_type=kind,operation=op,resource_id=rid),),
                risk=F.AuthorizationRisk.READ_ONLY if op is Operation.READ else F.AuthorizationRisk.SENSITIVE,authorized_scopes=scopes,revision=1,valid_until=None)))
            requirement=F.ResourceControlRequirementFact(requirement_id='internal-resource',resource=ref,operation=op,kind=F.ControlKind.ORGANIZATION,
                namespace='nexloop',value='internal',minimum_rank=0,dominance=F.ControlDominance.EXACT,revision=1,digest=_digest({'requirement':'internal'}),valid_until=None)
            facts.append(('controls',[pid,rid,op.value],F.ControlFacts(**base,subject_clearances=(clearance,),resource_requirements=(requirement,),complete=True,valid_until=None,revision=1)))
            rule=F.PolicyRuleFact(binding_id='world-policy',binding_digest=_digest({'world':world}),resource=ref,operation=op,policy_set_id='service-world',
                active_version=1,version_digest=canonical.sha256,canonical_ast_utf8=canonical.utf8,canonical_ast_sha256=canonical.sha256,effect=PolicyEffect.REQUIRE,
                condition=condition,activation_head_revision=1,activation_head_digest=canonical.sha256,registry_revision=1,registry_digest=canonical.sha256,
                activation_witness=canonical.sha256,published_at=past)
            facts.append(('policies',[pid,rid,op.value],F.PolicyFacts(**base,rules=(rule,),complete=True,valid_until=None,revision=1)))
    return binding,facts


def _revision_fact(tenant):
    return ('revision',['catalog'],F.RevisionSourceFacts(tenant_id=tenant,repository_witness=WITNESS,schema_revision=1,
        schema_digest=_digest({'schema':'nexloop-authority-v1'}),event_high_water=1,revision=1))


def _model(kind,payload):
    try:return FACT_MODELS[kind].model_validate_json(json.dumps(payload))
    except Exception:return None


def _service_grant_triples(inventory):
    triples=set()
    for row in inventory['facts']:
        if row['kind']!='grants' or row['payload'].get('subject_kind') not in ('service','agent'):continue
        for grant in row['payload'].get('grants',[]):
            for op in grant.get('operations',[]):
                triples.add((row['payload']['principal_id'],grant['resource']['resource_id'],op))
    return triples


def plan(manifest,tenant,inventory,*,credential_expires_at=None):
    """Pure diff: facts to write (incl. revocations), credentials to create, findings."""
    human=set(inventory.get('human_identifiers',[]))
    current={(r['kind'],tuple(r['key'])):r['payload'] for r in inventory['facts']}
    credentials={c['credential_id']:c for c in inventory['credentials']}
    writes=[];new_credentials=[];findings=[]
    for principal in manifest['principals']:
        if {principal['principal_id'],principal['subject_id']}&human:_reject('excluded_by_policy:human_principal')
        existing=credentials.get(principal['credential_id'])
        if existing is not None:expiry=datetime.fromisoformat(existing['expires_at'])
        elif credential_expires_at is not None:expiry=credential_expires_at
        else:expiry=None
        binding,facts=compile_principal(manifest,tenant,principal,
            credential_expires_at=expiry or datetime(2000,1,1,tzinfo=UTC))
        if existing is None:
            findings.append({'credential':principal['credential_reference'],'state':'missing'})
            new_credentials.append((principal,binding,expiry))
        else:
            stored=_binding(existing['binding'])
            if stored!=binding:findings.append({'credential':principal['credential_reference'],'state':'credential_rotation_required'})
            elif existing['status']!='active' or expiry<=datetime.now(UTC):findings.append({'credential':principal['credential_reference'],'state':'inactive_or_expired'})
            if existing['worlds']!=[manifest['world']]:findings.append({'credential':principal['credential_reference'],'state':'world_mismatch'})
        for kind,key,fact in facts:
            stored=current.get((kind,tuple(key)))
            if stored is None or _model(kind,stored)!=fact:writes.append((kind,key,fact))
        # Grants previously applied for this principal but no longer in the manifest are revoked (empty grant set).
        listed={g['resource_id'] for g in manifest['grants'] if g['principal']==principal['role']}
        for (kind,key),payload in current.items():
            if kind=='grants' and key[0]==principal['principal_id'] and key[1] not in listed and payload.get('grants'):
                writes.append(('grants',list(key),F.GrantFacts(tenant_id=tenant,repository_witness=WITNESS,subject_kind=GrantSubjectKind.SERVICE,
                    principal_id=principal['principal_id'],grants=(),valid_until=None,complete=True,next_cursor=None,revision=1)))
    if ('revision',('catalog',)) not in current:writes.append(_revision_fact(tenant))
    unique={}
    for kind,key,fact in writes:unique[(kind,tuple(key))]=(kind,key,fact)
    writes=sorted(unique.values(),key=lambda w:(ORDER.index(w[0]),w[1]))
    expected=expected_grants(manifest);actual=_service_grant_triples(inventory)
    managed={p['principal_id'] for p in manifest['principals']}
    extra=[{'principal_id':p,'resource_id':r,'operation':o,'manifest_principal':p in managed} for p,r,o in sorted(actual-expected)]
    missing=[{'principal_id':p,'resource_id':r,'operation':o} for p,r,o in sorted(expected-actual)]
    return {'writes':writes,'new_credentials':new_credentials,'findings':findings,'extra_grants':extra,'missing_grants':missing}


def _binding(raw):
    try:return F.CredentialAuthenticationBinding.model_validate_json(json.dumps(raw))
    except Exception:return None


def inventory(database_url_file,tenant):
    try:db=configurator_connection(database_url_file)
    except Exception:raise ServiceGrantsRejected('configurator_role_required') from None
    with db,db.transaction():
        db.execute("set local statement_timeout='10000ms'")
        return db.execute('select control.nexloop_service_grant_inventory(%s)',(tenant,)).fetchone()[0]


def doctor(manifest,tenant,*,database_url_file):
    """Read-only comparison of current service authority with the manifest."""
    current=inventory(database_url_file,tenant)
    result=plan(manifest,tenant,current)
    drift=[{'kind':k,'key':key} for k,key,_ in result['writes'] if k!='revision']
    report={'tenant':tenant,'manifest_digest':manifest_digest(manifest),'authority_revision':current['authority_revision'],
        'missing_grants':result['missing_grants'],'extra_grants':result['extra_grants'],'fact_drift':drift,'credentials':result['findings']}
    report['in_sync']=not (report['missing_grants'] or report['extra_grants'] or drift or result['findings'])
    return report


def apply(manifest,tenant,*,database_url_file,signing_key_file,signing_key_id,service_secrets_file,credential_expires_at=None):
    """Idempotent apply through the 0050 configurator; returns an auditable change report."""
    current=inventory(database_url_file,tenant)
    result=plan(manifest,tenant,current,credential_expires_at=credential_expires_at)
    blocking=[f for f in result['findings'] if f['state'] in ('credential_rotation_required','world_mismatch')]
    if blocking:_reject('credential_rotation_required')
    if result['new_credentials'] and any(expiry is None for _,_,expiry in result['new_credentials']):_reject('credential_expiry_required')
    report={'tenant':tenant,'manifest_digest':manifest_digest(manifest),'changed':False,
        'facts_written':[{'kind':k,'key':key} for k,key,_ in result['writes']],
        'credentials_created':[p['credential_reference'] for p,_,_ in result['new_credentials']],
        'grants_added':result['missing_grants'],
        'grants_revoked':[{'principal_id':key[0],'resource_id':key[1]} for k,key,f in result['writes'] if k=='grants' and not f.grants],
        'extra_grants_outside_manifest':[g for g in result['extra_grants'] if not g['manifest_principal']],
        'authority_revision':current['authority_revision']}
    if not result['writes'] and not result['new_credentials']:return report
    secrets={}
    if result['new_credentials']:
        try:loaded=strict_json(read_private_text(service_secrets_file,maximum=1048576))
        except Exception:raise ServiceGrantsRejected('service_secrets_unreadable') from None
        for principal,_,_ in result['new_credentials']:
            secret=loaded.get(principal['credential_reference']) if type(loaded) is dict else None
            if type(secret) is not str:_reject('service_secret_missing')
            secrets[principal['credential_reference']]=secret
    body={'schema_version':'1.0','tenant_id':tenant,'expected_revision':current['authority_revision'],
        'tenant_status':current['tenant_status'] or 'active','object_types':[],'actions':[],'functions':[],
        'authority_facts':[{'kind':k,'key':list(key),'payload':f.model_dump(mode='json')} for k,key,f in result['writes']],
        'service_credentials':[{'reference':p['credential_reference'],'binding':b.model_dump(mode='json'),'worlds':[manifest['world']],
            'expires_at':expiry.isoformat(),'status':'active'} for p,b,expiry in result['new_credentials']],
        'browser_applications':[],'browser_business_applications':[],'browser_rate_policies':[],'identity_allowances':[]}
    # Stable manifest id: replaying the same change against the same revision is a no-op in 0050.
    body['manifest_id']=str(uuid.uuid5(NAMESPACE,tenant+':'+report['manifest_digest']+':'+str(current['authority_revision'])+':'+canonical_payload(body)))
    try:configured=apply_manifest_with_secrets(body,database_url_file=database_url_file,signing_key_file=signing_key_file,
            signing_key_id=signing_key_id,service_secrets=secrets)
    except ConfigurationRejected:raise ServiceGrantsRejected('configuration_rejected') from None
    report.update(changed=True,authority_revision=configured['authority_revision'],configuration_digest=configured['manifest_digest'])
    return report


def main(argv=None):
    p=argparse.ArgumentParser(description='Apply/inspect NexLoop service-principal grants through trusted configuration (ADR-020 §3)')
    p.add_argument('--manifest',type=Path,required=True);p.add_argument('--tenant')
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check',action='store_true');mode.add_argument('--doctor',action='store_true');mode.add_argument('--apply',action='store_true')
    p.add_argument('--database-url-file',type=Path);p.add_argument('--signing-key-file',type=Path);p.add_argument('--signing-key-id')
    p.add_argument('--service-secrets-file',type=Path);p.add_argument('--credential-expires-at')
    a=p.parse_args(argv)
    try:
        manifest=load(a.manifest)
        if a.check:
            print(canonical_payload({'checked':True,'manifest_digest':manifest_digest(manifest),'principals':len(manifest['principals']),'grants':len(manifest['grants'])}));return 0
        if a.tenant is None or a.database_url_file is None:_reject('arguments')
        if a.doctor:
            report=doctor(manifest,a.tenant,database_url_file=a.database_url_file);print(canonical_payload(report));return 0 if report['in_sync'] else 1
        if any(v is None for v in (a.signing_key_file,a.signing_key_id,a.service_secrets_file)):_reject('arguments')
        expiry=None
        if a.credential_expires_at:
            expiry=datetime.fromisoformat(a.credential_expires_at)
            if expiry.tzinfo is None:_reject('credential_expiry_required')
        print(canonical_payload(apply(manifest,a.tenant,database_url_file=a.database_url_file,signing_key_file=a.signing_key_file,
            signing_key_id=a.signing_key_id,service_secrets_file=a.service_secrets_file,credential_expires_at=expiry)));return 0
    except ServiceGrantsRejected as error:
        print('service_grants_rejected:'+error.reason,file=sys.stderr);return 2
    except Exception:
        print('service_grants_unavailable',file=sys.stderr);return 3


if __name__=='__main__':raise SystemExit(main())
