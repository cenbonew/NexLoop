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
            service_secrets_file=paths['secrets'],credential_expires_at=options.get('expiry',datetime.now(UTC)+timedelta(hours=2)))
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
