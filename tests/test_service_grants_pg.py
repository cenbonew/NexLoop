"""NX-048 actual PostgreSQL: versioned service grants through trusted configuration.

After a clean bootstrap the repository manifest is applied by the production
entry point as `nexloop_configurator`; the NX-019 scheduler/worker, NX-020 matcher,
runtime worker and message relay then authenticate with their configured service
credentials and pass their real authorization paths with no test-fixture seeding
of authority. The admin connection only bootstraps, enables the configurator
login (deployment step) and inspects/injects drift for doctor tests.
"""
from datetime import UTC,datetime,timedelta
import copy,json,secrets
from pathlib import Path
import psycopg,pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service,authorization_service
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.claim_extraction_jobs import ClaimExtractionScheduler,ClaimExtractionWorker,ClaimFeed
from nexloop_eios.durable_queue import PostgresDurableQueue
from nexloop_eios.postgres_artifacts import AuthoritySigner
from nexloop_eios import service_grants as G

MANIFEST=Path(__file__).resolve().parents[1]/'deploy/authorization/service-grants.v1.json'
OWNER=Path(__file__).resolve().parents[1]/'deploy/authorization/owner-property-restrictions.json'
TENANT='synthetic-a'


class Private(dict):
    def __repr__(self):return '<redacted service-grant configuration>'


def private(root,name,value):
    path=root/name;path.write_text(value);path.chmod(0o600);return path


@pytest.fixture
def deployment(pg,admin,tmp_path):
    bootstrap(admin)
    admin.execute('alter role nexloop_configurator login')
    manifest=G.load(MANIFEST)
    tokens={p['credential_reference']:secrets.token_urlsafe(48) for p in manifest['principals']}
    key=secrets.token_bytes(32)
    paths=Private(dsn=private(tmp_path,'configurator-dsn',make_conninfo(pg,user='nexloop_configurator')),
        signing=private(tmp_path,'signing-key',key.hex()),secrets=private(tmp_path,'service-secrets',json.dumps(tokens)),
        superuser=private(tmp_path,'superuser-dsn',pg))
    def apply(body=manifest,**options):
        return G.apply(body,TENANT,database_url_file=paths['dsn'],signing_key_file=paths['signing'],signing_key_id='nx048-deploy',
            service_secrets_file=paths['secrets'],credential_expires_at=options.get('expiry',datetime.now(UTC)+timedelta(hours=2)),
            owner_restrictions=options.get('owner',G.load_owner_restrictions(OWNER)))
    with open_core(make_conninfo(pg,user='nexloop_api')) as api,open_core(make_conninfo(pg,user='nexloop_domain_worker')) as worker,\
         open_core(make_conninfo(pg,user='nexloop_scheduler')) as scheduler:
        yield Private(pg=pg,manifest=manifest,tokens=tokens,signer=AuthoritySigner('nx048-deploy',key),paths=paths,apply=apply,
            pools={'nexloop_api':api,'nexloop_domain_worker':worker,'nexloop_scheduler':scheduler})


def session(d,role):
    principal=next(p for p in d['manifest']['principals'] if p['role']==role)
    pool=d['pools'][principal['database_role']]
    return pool,authenticate_service(pool,d['tokens'][principal['credential_reference']],world='real')


def decide(pool,s,resource,kind=ResourceType.ACTION,op=Operation.EXECUTE):
    # A resource without configured facts fails closed (unavailable) in EIOS: treated as not allowed.
    try:return authorization_service(pool,s).decide(s.query(resource_id=resource,resource_type=kind,operation=op)).allowed
    except (AuthorizationUnavailable,F.AuthorizationFactDenied):return False


def test_clean_bootstrap_apply_lets_nx019_nx020_services_authorize_without_seed(deployment,admin):
    d=deployment
    before=admin.execute('select count(*) from authz.nexloop_authority_facts').fetchone()[0]
    assert before==0
    report=d['apply']()
    assert report['changed'] is True and sorted(report['credentials_created'])==sorted(d['tokens'])
    assert len(report['grants_added'])==len(d['manifest']['grants'])
    # The write is an audited trusted-configuration publication by the configurator.
    assert admin.execute('select count(*),min(operator_role) from control.nexloop_configuration_publications').fetchone()==(1,'nexloop_configurator')
    # NX-019 scheduler: real feed + queue authority paths (empty feed, nothing due).
    pool,s=session(d,'claim_extraction_scheduler')
    assert ClaimExtractionScheduler(pool,s,d['signer']).run_once()==[]
    assert ClaimFeed(pool,s,d['signer']).backlog() is not None
    # NX-019 worker: real queue claim under its own credential.
    pool,s=session(d,'claim_extraction_worker')
    assert ClaimExtractionWorker(pool,s,d['signer'],provider=None).run_once()=='idle'
    assert decide(pool,s,'eios:action:nexloop.claim.extract:1')
    # NX-020 matcher: match Action and type-level reads; nothing beyond the manifest.
    pool,s=session(d,'claim_matcher')
    assert decide(pool,s,'eios:action:nexloop.claim.match:1')
    assert decide(pool,s,'eios:object_type:Consumer',ResourceType.OBJECT_TYPE,Operation.READ)
    assert not decide(pool,s,'eios:action:nexloop.claim.extract:1')
    assert not decide(pool,s,'eios:action:nexloop.service.request:1')
    # S1/S2 services.
    pool,s=session(d,'runtime_worker')
    assert PostgresDurableQueue(pool,s,d['signer'],queue='operations').claim() is None
    assert not decide(pool,s,'eios:action:NexLoop.queue.claim-extraction:1')
    pool,s=session(d,'message_relay')
    assert decide(pool,s,'eios:action:nexloop.conversation.route:1')
    # Credentials are stored as digests only.
    stored=json.dumps(admin.execute('select array_agg(token_digest) from authz.nexloop_service_credentials').fetchone()[0])
    assert not any(token in stored for token in d['tokens'].values())


def test_reapply_is_noop_and_doctor_in_sync(deployment,admin):
    d=deployment;first=d['apply']()
    revision=admin.execute("select authority_revision from control.nexloop_tenants where tenant_id=%s",(TENANT,)).fetchone()[0]
    assert first['authority_revision']==revision
    again=d['apply']()
    assert again['changed'] is False and again['facts_written']==[] and again['credentials_created']==[]
    assert admin.execute("select authority_revision from control.nexloop_tenants where tenant_id=%s",(TENANT,)).fetchone()[0]==revision
    assert admin.execute('select count(*) from control.nexloop_configuration_publications').fetchone()==(1,)
    report=G.doctor(d['manifest'],TENANT,database_url_file=d['paths']['dsn'])
    assert report['in_sync'] is True and report['extra_grants']==[] and report['missing_grants']==[]
    # CLI entry points (stdout is a canonical report; no secrets).
    assert G.main(['--manifest',str(MANIFEST),'--check'])==0
    assert G.main(['--manifest',str(MANIFEST),'--tenant',TENANT,'--doctor','--database-url-file',str(d['paths']['dsn'])])==0


def test_doctor_reports_grants_outside_manifest_and_apply_revokes_managed_drift(deployment,admin):
    from multi_authority_fixture import seed_multi_authority
    d=deployment;d['apply']()
    # Out-of-band authority (e.g. a leftover test/legacy service) is reported, never silently accepted.
    seed_multi_authority(admin,d['pools']['nexloop_api'],[('eios:action:Consumer.create:1',ResourceType.ACTION,Operation.EXECUTE)],identity_suffix='-legacy-service')
    # Drift on a managed principal: an extra grant injected outside the manifest.
    scheduler=next(p for p in d['manifest']['principals'] if p['role']=='claim_extraction_scheduler')
    extra=admin.execute("select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and entity_key=%s",
        (TENANT,[scheduler['principal_id'],'eios:action:nexloop.claim.extract:1'])).fetchone()[0]
    extra['grants'][0]['resource']['resource_id']='eios:action:nexloop.claim.match:1';extra.pop('snapshot_digest',None)
    admin.execute("insert into authz.nexloop_authority_facts values(%s,'grants',%s,%s)",(TENANT,[scheduler['principal_id'],'eios:action:nexloop.claim.match:1'],Jsonb(extra)))
    report=G.doctor(d['manifest'],TENANT,database_url_file=d['paths']['dsn'])
    assert report['in_sync'] is False
    extras={(g['principal_id'],g['resource_id'],g['manifest_principal']) for g in report['extra_grants']}
    assert ('synthetic-a-legacy-service-principal','eios:action:Consumer.create:1',False) in extras
    assert (scheduler['principal_id'],'eios:action:nexloop.claim.match:1',True) in extras
    assert G.main(['--manifest',str(MANIFEST),'--tenant',TENANT,'--doctor','--database-url-file',str(d['paths']['dsn'])])==1
    fixed=d['apply']()
    assert {'principal_id':scheduler['principal_id'],'resource_id':'eios:action:nexloop.claim.match:1'} in fixed['grants_revoked']
    assert any(g['principal_id']=='synthetic-a-legacy-service-principal' for g in fixed['extra_grants_outside_manifest'])
    after=G.doctor(d['manifest'],TENANT,database_url_file=d['paths']['dsn'])
    assert [g for g in after['extra_grants'] if g['manifest_principal']]==[]
    assert all(not g['manifest_principal'] for g in after['extra_grants'])  # foreign principal stays reported only


def test_removed_grant_is_revoked_on_next_apply(deployment):
    d=deployment;d['apply']()
    reduced=copy.deepcopy(d['manifest'])
    reduced['grants']=[g for g in reduced['grants'] if not (g['principal']=='claim_matcher' and g['resource_id']=='eios:action:Product.create:1')]
    report=d['apply'](G.validate(reduced))
    matcher=next(p for p in d['manifest']['principals'] if p['role']=='claim_matcher')
    assert {'principal_id':matcher['principal_id'],'resource_id':'eios:action:Product.create:1'} in report['grants_revoked']
    pool,s=session(d,'claim_matcher')  # re-authenticate after the directory change
    assert not decide(pool,s,'eios:action:Product.create:1') and decide(pool,s,'eios:action:nexloop.claim.match:1')


def _mutated(change):
    body=json.loads(MANIFEST.read_text());change(body);return body


@pytest.mark.parametrize('name,change',[
    ('schema_review',lambda b:b['grants'].append({'principal':'claim_matcher','resource_type':'action','resource_id':'eios:action:ontology.schema.review:1','operations':['execute'],'purpose':'x','source_task':'NX-045'})),
    ('real_effect',lambda b:b['grants'].append({'principal':'message_relay','resource_type':'action','resource_id':'eios:action:nexloop.service.request:1','operations':['execute'],'purpose':'send','source_task':'NX-016'})),
    ('real_dispatch_flag',lambda b:b['grants'][0].update(purpose='turn on REAL_DISPATCH_ENABLED')),
    ('human_kind',lambda b:b['principals'][0].update(subject_kind='human')),
    ('identity_user',lambda b:b['grants'].append({'principal':'claim_matcher','resource_type':'identity_user','resource_id':'eios:identity_user:x','operations':['identity.user.invite'],'purpose':'x','source_task':'x'})),
    ('external_effect',lambda b:b['grants'].append({'principal':'claim_matcher','resource_type':'external_effect','resource_id':'eios:external_effect:mail','operations':['execute'],'purpose':'x','source_task':'x'})),
    ('approve_operation',lambda b:b['grants'][0].update(operations=['approve'])),
    ('object_write',lambda b:b['grants'].append({'principal':'claim_matcher','resource_type':'object_type','resource_id':'eios:object_type:Consumer','operations':['edit'],'purpose':'x','source_task':'x'})),
])
def test_policy_exclusions_reject_whole_manifest(name,change,tmp_path):
    path=tmp_path/'grants.json';path.write_text(json.dumps(_mutated(change)))
    with pytest.raises(G.ServiceGrantsRejected):G.load(path)
    assert G.main(['--manifest',str(path),'--check'])==2


def test_human_principal_reuse_and_superuser_connection_are_rejected(deployment,admin,identity):
    d=deployment
    human=copy.deepcopy(d['manifest']);human['principals'][0]['principal_id']=identity[2].principal_id
    with pytest.raises(G.ServiceGrantsRejected,match='human_principal'):d['apply'](G.validate(human))
    assert admin.execute('select count(*) from control.nexloop_configuration_publications').fetchone()==(0,)
    with pytest.raises(G.ServiceGrantsRejected,match='configurator_role_required'):
        G.apply(d['manifest'],TENANT,database_url_file=d['paths']['superuser'],signing_key_file=d['paths']['signing'],signing_key_id='nx048-deploy',
            service_secrets_file=d['paths']['secrets'],credential_expires_at=datetime.now(UTC)+timedelta(hours=1))
    assert admin.execute('select count(*) from authz.nexloop_authority_facts where tenant_id=%s',(TENANT,)).fetchone()==(0,)
    for role in ('nexloop_api','nexloop_domain_worker'):
        with psycopg.connect(make_conninfo(d['pg'],user=role)) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('select control.nexloop_service_grant_inventory(%s)',(TENANT,))


from test_browser_identity_reads import identity  # noqa: E402  (real Human directory row)


def _raw_configure(d,admin,facts):
    """Bypass the Python validators: the 0084 configure_manifest shape checks themselves must refuse."""
    import hashlib,uuid
    from nexloop_eios.postgres_artifacts import canonical_payload
    revision=admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(TENANT,)).fetchone()[0]
    body={'schema_version':'1.0','manifest_id':str(uuid.uuid4()),'tenant_id':TENANT,'expected_revision':revision,'tenant_status':'active',
        'object_types':[],'actions':[],'functions':[],'authority_facts':facts,'service_credentials':[],'browser_applications':[],
        'browser_business_applications':[],'browser_rate_policies':[],'identity_allowances':[]}
    text=canonical_payload(body)
    with psycopg.connect(make_conninfo(d['pg'],user='nexloop_configurator')) as db,db.transaction():
        return db.execute('select control.nexloop_configure_manifest(%s,%s,%s,%s,%s,%s::jsonb)',
            (TENANT,text,hashlib.sha256(text.encode()).hexdigest(),'nx048-deploy',d['signer'].material,'{}')).fetchone()[0]


def test_property_access_rule_and_owner_restriction_are_written_only_by_trusted_configuration(deployment,admin):
    d=deployment;report=d['apply']()
    matcher=next(p for p in d['manifest']['principals'] if p['role']=='claim_matcher')
    stored=dict(admin.execute("select fact_kind,payload from authz.nexloop_authority_facts where fact_kind in ('property_access_rule','property_group_restriction') and payload->>'type_name'='Consumer'").fetchall())
    assert stored['property_access_rule']['principal_id']==matcher['principal_id'] and stored['property_access_rule']['include_review_published'] is True
    # Owner decision: the matcher may derive every ADR-019 §2 group (contract enum of candidate-definition property_group).
    from nexloop_eios.claim_matching import GROUPS
    assert stored['property_access_rule']['operations']==['edit','read'] and stored['property_access_rule']['property_groups']==sorted(GROUPS)
    # Owner decision (2026-10-09): no Consumer group is restricted; the fact still exists so derivation is enabled.
    assert stored['property_group_restriction']['restricted_groups']==[] and 'project owner' in stored['property_group_restriction']['decision']
    assert {'kind':'property_access_rule','key':[matcher['principal_id'],'Consumer']} in report['facts_written'] and report['property_access']==[]
    # NX-026: the keeper's lifecycle-only Commitment rule, under the owner's Commitment decision (no group restricted).
    keeper=next(p for p in d['manifest']['principals'] if p['role']=='commitment_keeper')
    commitment=dict(admin.execute("select fact_kind,payload from authz.nexloop_authority_facts where fact_kind in ('property_access_rule','property_group_restriction') and payload->>'type_name'='Commitment'").fetchall())
    assert commitment['property_access_rule']['principal_id']==keeper['principal_id'] and commitment['property_access_rule']['property_groups']==['commitment_lifecycle']
    assert commitment['property_access_rule']['include_review_published'] is False and commitment['property_group_restriction']['restricted_groups']==[]
    # NX-027: the recorder's state-only CommercialRecord rule, under the owner's CommercialRecord decision (no group restricted).
    recorder=next(p for p in d['manifest']['principals'] if p['role']=='commercial_recorder')
    commercial=dict(admin.execute("select fact_kind,payload from authz.nexloop_authority_facts where fact_kind in ('property_access_rule','property_group_restriction') and payload->>'type_name'='CommercialRecord'").fetchall())
    assert commercial['property_access_rule']['principal_id']==recorder['principal_id'] and commercial['property_access_rule']['property_groups']==['commercial_state']
    assert commercial['property_access_rule']['include_review_published'] is False and commercial['property_group_restriction']['restricted_groups']==[]
    assert G.doctor(d['manifest'],TENANT,database_url_file=d['paths']['dsn'])['in_sync'] is True
    # A rule listing a restricted group is reported, not rejected; the group is still never derived (SQL).
    # Synthetic owner restriction: the mechanism is exercised although the real decision restricts nothing.
    synthetic={'schema_version':G.OWNER_SCHEMA,'decided_by':'synthetic owner','decision':'synthetic restriction',
        'restrictions':[{'type_name':'Consumer','restricted_groups':['synthetic_restricted']}]}
    listed=copy.deepcopy(d['manifest']);listed['property_access_rules'][0]['property_groups']=sorted(listed['property_access_rules'][0]['property_groups']+['synthetic_restricted'])
    doctor=G.doctor(G.validate(listed),TENANT,database_url_file=d['paths']['dsn'],owner_restrictions=G.validate_owner_restrictions(synthetic))
    assert {'state':'restricted_group_listed','principal_id':matcher['principal_id'],'type_name':'Consumer','groups':['synthetic_restricted']} in doctor['property_access']
    # Removing the rule deactivates it (never deletes it).
    removed=copy.deepcopy(d['manifest']);removed.pop('property_access_rules')
    assert {'kind':'property_access_rule','key':[matcher['principal_id'],'Consumer']} in d['apply'](G.validate(removed))['facts_written']
    assert admin.execute("select array_agg(payload->>'active' order by payload->>'type_name') from authz.nexloop_authority_facts where fact_kind='property_access_rule'").fetchone()==(['false','false','false'],)


def test_rule_without_owner_restriction_is_reported_and_service_manifest_cannot_carry_restrictions(deployment,admin):
    d=deployment
    report=d['apply'](owner=None)
    matcher=next(p for p in d['manifest']['principals'] if p['role']=='claim_matcher')
    keeper=next(p for p in d['manifest']['principals'] if p['role']=='commitment_keeper')
    recorder=next(p for p in d['manifest']['principals'] if p['role']=='commercial_recorder')
    assert report['property_access']==[{'state':'owner_restriction_missing','principal_id':matcher['principal_id'],'type_name':'Consumer'},
        {'state':'owner_restriction_missing','principal_id':keeper['principal_id'],'type_name':'Commitment'},
        {'state':'owner_restriction_missing','principal_id':recorder['principal_id'],'type_name':'CommercialRecord'}]
    assert admin.execute("select count(*) from authz.nexloop_authority_facts where fact_kind='property_group_restriction'").fetchone()==(0,)
    assert G.doctor(d['manifest'],TENANT,database_url_file=d['paths']['dsn'])['in_sync'] is False
    smuggled=copy.deepcopy(d['manifest']);smuggled['property_group_restrictions']=[]
    with pytest.raises(G.ServiceGrantsRejected,match='manifest_shape'):G.validate(smuggled)
    implicit=copy.deepcopy(d['manifest']);implicit['property_access_rules'][0].pop('include_review_published')
    with pytest.raises(G.ServiceGrantsRejected,match='property_access_rule_shape'):G.validate(implicit)
    untyped=copy.deepcopy(d['manifest']);untyped['grants']=[g for g in untyped['grants'] if g['resource_id']!='eios:object_type:Consumer']
    with pytest.raises(G.ServiceGrantsRejected,match='requires_type_read'):G.validate(untyped)
    with pytest.raises(G.ServiceGrantsRejected,match='owner_restrictions_shape'):
        G.validate_owner_restrictions({'schema_version':G.OWNER_SCHEMA,'decided_by':'x','decision':'y','restrictions':[{'type_name':'Consumer','restricted_groups':['spending_power','demographics']}]})


def test_configure_manifest_refuses_malformed_property_facts(deployment,admin):
    d=deployment;d['apply']()
    matcher=next(p for p in d['manifest']['principals'] if p['role']=='claim_matcher')['principal_id']
    rule={'tenant_id':TENANT,'principal_id':matcher,'type_name':'Consumer','operations':['read'],'property_groups':['preference'],
        'include_review_published':True,'basis_schema_version':1,'active':True,'valid_until':'2027-01-01T00:00:00+00:00'}
    restriction={'tenant_id':TENANT,'type_name':'Consumer','restricted_groups':['demographics'],'decision':'owner decision'}
    bad_rules=[rule|{'extra':1},rule|{'operations':['delete']},rule|{'property_groups':['Bad-Group']},rule|{'include_review_published':'true'},
        {k:v for k,v in rule.items() if k!='include_review_published'},rule|{'basis_schema_version':0},rule|{'valid_until':None},
        rule|{'principal_id':'synthetic-a-unknown-principal'}]
    for payload in bad_rules:
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            _raw_configure(d,admin,[{'kind':'property_access_rule','key':[payload['principal_id'],'Consumer'],'payload':payload}])
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        _raw_configure(d,admin,[{'kind':'property_access_rule','key':[matcher,'Product'],'payload':rule}])
    for payload in (restriction|{'extra':1},restriction|{'restricted_groups':['Bad']},restriction|{'decision':''},{k:v for k,v in restriction.items() if k!='decision'}):
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            _raw_configure(d,admin,[{'kind':'property_group_restriction','key':['Consumer'],'payload':payload}])
    # The exact shapes are accepted through the same audited path.
    assert _raw_configure(d,admin,[{'kind':'property_access_rule','key':[matcher,'Consumer'],'payload':rule},
        {'kind':'property_group_restriction','key':['Consumer'],'payload':restriction}])['configured'] is True
