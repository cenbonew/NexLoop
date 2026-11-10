"""REAL PG draft tests; not collected/applied/executed until Root approval.

Configuration artifacts are passed to production CLI ports; tests never seed
authority/configuration tables to make those ports succeed. Generated declaration
records are synthetic input only, not fake authentication decisions.
"""
import hashlib,json,secrets,uuid
from datetime import UTC,datetime,timedelta
from pathlib import Path
import psycopg,pytest
from psycopg.conninfo import make_conninfo
from eios.authz import facts as F
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from eios.authz.service import AuthorizationDecisionService
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.backend import open_backend
from nexloop_eios.authorization import PostgresAuthorityProvider
from nexloop_eios.initial_identity import create_initial_identity
from nexloop_eios.trusted_configuration import apply_manifest,ConfigurationRejected
from authority_fixture import authority_records
from test_browser_session_reads import authenticate,create
from nexloop_eios.browser_sessions import PostgresBrowserSessionUnitOfWork
from nexloop_eios.browser_identity import open_browser_identity

class PrivateConfiguration(dict):
    def __repr__(self):return '<PrivateConfiguration redacted>'


def private(root,name,value):
    path=root/name;path.write_text(value);path.chmod(0o600);return path


@pytest.fixture
def configured(pg,admin,tmp_path,request):
    bootstrap(admin)
    admin.execute('set log_parameter_max_length=0')
    admin.execute('alter system set log_parameter_max_length=0')
    admin.execute('alter system set log_parameter_max_length_on_error=0')
    admin.execute('select pg_reload_conf()')
    with psycopg.connect(pg) as clean:
        assert clean.execute('show log_parameter_max_length').fetchone()[0]=='0'
        assert clean.execute('show log_parameter_max_length_on_error').fetchone()[0]=='0'
    admin.execute('alter role nexloop_configurator login')
    # NX-037 test stack (marker nx037_test_stack): the synthetic tenant constant, which the run entry's test stack requires.
    from support.tenants import TENANT_A
    tenant=TENANT_A if request is not None and request.node.get_closest_marker('nx037_test_stack') else str(uuid.uuid4());target='eios:artifact:explicit-configuration-source'
    binding,_,rows=authority_records(tenant,target,operation=Operation.READ,resource_type=ResourceType.ARTIFACT)
    token=secrets.token_urlsafe(48);key=secrets.token_hex(32);seal=secrets.token_hex(32)
    identityapp='explicit-local-login';expiry=(datetime.now(UTC)+timedelta(hours=1)).isoformat()
    manifest={'schema_version':'1.0','manifest_id':str(uuid.uuid4()),'tenant_id':tenant,'expected_revision':0,'tenant_status':'active',
        'object_types':[],'actions':[],'functions':[],
        'authority_facts':[{'kind':kind,'key':key,'payload':fact.model_dump(mode='json')} for kind,key,fact in rows],
        'service_credentials':[{'reference':'source','binding':binding.model_dump(mode='json'),'worlds':['real'],'expires_at':expiry,'status':'active'}],
        'browser_applications':[{'application_id':identityapp,'active':True}],'browser_business_applications':[],
        'browser_rate_policies':[{'action':'local_login','operator_principal_id':'nexloop_identity','maximum_attempts':10,'window_seconds':60,'active':True}],
        'identity_allowances':[{'application_id':identityapp,'enabled':True,'maximum_accounts':1,'operator_label':'explicit-technical-owner','idempotency_key_digest':hashlib.sha256(bytes.fromhex(seal)).hexdigest()}]}
    paths=PrivateConfiguration(dsn=private(tmp_path,'configuration-dsn',make_conninfo(pg,user='nexloop_configurator')),signing=private(tmp_path,'signing-key',key),
        secrets=private(tmp_path,'service-secrets',json.dumps({'source':token})),identity=private(tmp_path,'identity-dsn',make_conninfo(pg,user='nexloop_identity')),
        seal=private(tmp_path,'identity-idempotency-key',seal),password=private(tmp_path,'human-password',secrets.token_urlsafe(24)))
    raw_signing=tmp_path/'backend-signing-key';raw_signing.write_bytes(bytes.fromhex(key));raw_signing.chmod(0o600);paths['backend_signing']=raw_signing
    result=apply_manifest(manifest,database_url_file=paths['dsn'],signing_key_file=paths['signing'],signing_key_id='explicit-configuration',service_secrets_file=paths['secrets'])
    yield PrivateConfiguration(pg=pg,tenant=tenant,manifest=manifest,paths=paths,result=result,token=token,application=identityapp,target=target,binding=binding)


def apply(f,body):
    p=f['paths'];return apply_manifest(body,database_url_file=p['dsn'],signing_key_file=p['signing'],signing_key_id='explicit-configuration',service_secrets_file=p['secrets'])


def identity_manifest(f):
    now=(datetime.now(UTC)-timedelta(seconds=1)).isoformat();tenant=f['tenant']
    return {'schema_version':'1.0','tenant_id':tenant,'application_id':f['application'],'request_id':str(uuid.uuid4()),'operator_label':'explicit-technical-owner',
        'subject':{'subject_id':'explicit-human-'+str(uuid.uuid4()),'kind':'human','status':'active','created_at':now,'updated_at':now,'revision':1},
        'membership':{'tenant_id':tenant,'subject_id':'REPLACED','principal_id':'explicit-principal-'+str(uuid.uuid4()),'kind':'home','status':'active','valid_from':now,'valid_until':None,'trusted_attributes':{},'revision':1},
        'account':{'tenant_id':tenant,'local_account_id':'explicit-account-'+str(uuid.uuid4()),'subject_id':'REPLACED','username':'explicit-user','verified_email':'owner@example.invalid','status':'active','failed_attempts':0,'lockout_level':0,'locked_until':None,'must_change_password':False,'session_epoch':1,'created_at':now,'updated_at':now,'revision':1}}


def create_human(f,body):
    body['membership']['subject_id']=body['subject']['subject_id'];body['account']['subject_id']=body['subject']['subject_id']
    p=f['paths'];return create_initial_identity(body,database_url_file=p['identity'],password_file=p['password'],idempotency_key_file=p['seal'])


def test_actual_configured_service_authenticates_current_full_eios_permission(configured,tmp_path):
    f=configured
    with open_backend(database_url=make_conninfo(f['pg'],user='nexloop_api'),artifact_root=tmp_path/'artifacts',signing_key_file=f['paths']['backend_signing'],signing_key_id='explicit-configuration') as backend:
        service=backend.authenticate(f['token'],world='real')
        query=service._session.query(resource_id=f['target'],resource_type=ResourceType.ARTIFACT,operation=Operation.READ)
        context=F.AuthorizationFactsResolver(PostgresAuthorityProvider(backend._pool,service._session)).resolve(query)
        assert AuthorizationDecisionService().decide_resolved(context).allowed is True


def test_manifest_same_replay_and_changed_payload_or_secret_rejected_without_partial_rows(configured,admin):
    f=configured;before=admin.execute('select count(*) from control.nexloop_configuration_publications').fetchone()[0]
    assert apply(f,f['manifest'])==f['result']
    changed={**f['manifest'],'tenant_status':'suspended'}
    with pytest.raises(ConfigurationRejected):apply(f,changed)
    f['paths']['secrets'].write_text(json.dumps({'source':secrets.token_urlsafe(48)}))
    with pytest.raises(ConfigurationRejected):apply(f,f['manifest'])
    assert admin.execute('select count(*) from control.nexloop_configuration_publications').fetchone()[0]==before


def test_actual_explicit_human_create_password_login_and_no_business_grant(configured,admin):
    f=configured;body=identity_manifest(f);created=create_human(f,body);assert created['created'] is True
    assert create_human(f,body)['created'] is False
    assert admin.execute("select count(*) from authz.nexloop_authority_facts where fact_kind='grants' and entity_key[1]=%s",(body['membership']['principal_id'],)).fetchone()[0]==0
    from nexloop_eios.browser_identity import decode_local_account
    with open_browser_identity(make_conninfo(f['pg'],user='nexloop_identity')) as pool:
        uow=PostgresBrowserSessionUnitOfWork(pool,tenant_id=f['tenant'],application_id=f['application'])
        identity=(make_conninfo(f['pg'],user='nexloop_identity'),uow.get_subject(created['subject_id']),uow.get_membership(f['tenant'],created['principal_id']),uow.get_local_account(f['tenant'],created['account_id']),f['paths']['password'].read_text())
        evidence=authenticate(uow,identity)
        issued=create(uow,identity,evidence.evidence)
        assert issued.session.principal_id==created['principal_id']


def test_wrong_password_replay_quota_closed_and_crosstenant_deny(configured,admin):
    f=configured;body=identity_manifest(f);create_human(f,body)
    f['paths']['password'].write_text(secrets.token_urlsafe(24))
    with pytest.raises(ConfigurationRejected):create_human(f,body)
    another=identity_manifest(f)
    with pytest.raises(ConfigurationRejected):create_human(f,another)
    foreign=identity_manifest(f);foreign['tenant_id']=str(uuid.uuid4());foreign['membership']['tenant_id']=foreign['tenant_id'];foreign['account']['tenant_id']=foreign['tenant_id']
    with pytest.raises(ConfigurationRejected):create_human(f,foreign)
    assert admin.execute('select count(*) from control.nexloop_initial_identity_receipts').fetchone()[0]==1
    assert admin.execute('select enabled,created_accounts from control.nexloop_initial_identity_allowances').fetchone()==(False,1)



def test_a_full_allowance_is_never_raised_or_reopened_and_a_new_application_adds_accounts_that_sign_in_through_the_first(configured,admin):
    # run-add-synthetic-customer step 0: the WebChat allowance on stage is full and closed; more accounts come from a new application id.
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'/'deploy'))
    import stage_secrets as S
    f=configured;create_human(f,identity_manifest(f))
    assert admin.execute('select enabled,created_accounts,maximum_accounts from control.nexloop_initial_identity_allowances').fetchone()==(False,1,1)
    from nexloop_eios.trusted_configuration import apply_manifest_with_secrets
    def apply(f,manifest):  # as stage_secrets.apply_browser publishes: no service credential, an empty secret map
        return apply_manifest_with_secrets(manifest,database_url_file=f['paths']['dsn'],signing_key_file=f['paths']['signing'],signing_key_id='explicit-configuration',service_secrets={})
    def body(**categories):
        revision=admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(f['tenant'],)).fetchone()[0]
        empty={k:[] for k in ('object_types','actions','functions','authority_facts','service_credentials','browser_applications','browser_business_applications','browser_rate_policies','identity_allowances')}
        return {'schema_version':'1.0','manifest_id':str(uuid.uuid4()),'tenant_id':f['tenant'],'expected_revision':revision,'tenant_status':'active',**empty,**categories}
    allowance=f['manifest']['identity_allowances'][0]
    for changed in ({**allowance,'maximum_accounts':2},{**allowance,'enabled':True}):
        with pytest.raises(ConfigurationRejected):apply(f,body(identity_allowances=[changed]))
    assert admin.execute('select enabled,created_accounts,maximum_accounts from control.nexloop_initial_identity_allowances').fetchone()==(False,1,1)
    entries=S.additional_application_entries(application_id='explicit-local-login-b',browser_application_id=f['application'],workbench_application_id='explicit-workbench',
        seal_digest=allowance['idempotency_key_digest'],accounts=5,operator_label=allowance['operator_label'])
    apply(f,body(**entries))
    assert admin.execute("select enabled,created_accounts,maximum_accounts from control.nexloop_initial_identity_allowances where application_id='explicit-local-login-b'").fetchone()==(True,0,5)
    second=identity_manifest(f);second['application_id']='explicit-local-login-b';second['account']['username']='explicit-user-b'
    created=create_human(f,second);assert created['created'] is True
    assert admin.execute("select created_accounts from control.nexloop_initial_identity_allowances where application_id='explicit-local-login-b'").fetchone()[0]==1
    with open_browser_identity(make_conninfo(f['pg'],user='nexloop_identity')) as pool:
        uow=PostgresBrowserSessionUnitOfWork(pool,tenant_id=f['tenant'],application_id=f['application'])
        identity=(make_conninfo(f['pg'],user='nexloop_identity'),uow.get_subject(created['subject_id']),uow.get_membership(f['tenant'],created['principal_id']),uow.get_local_account(f['tenant'],created['account_id']),f['paths']['password'].read_text())
        issued=create(uow,identity,authenticate(uow,identity).evidence)
        assert issued.session.principal_id==created['principal_id']

def test_browser_rates_publishes_only_the_missing_rate_policies_and_leaves_applications_and_a_closed_allowance_alone(configured,admin):
    # Stage 11-10: G1 registered local_login only; NX-058 invitation_accept / password_reset_complete / oidc_start failed closed (0032 42501).
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'/'deploy'))
    import stage_secrets as S
    from nexloop_eios.trusted_configuration import apply_manifest_with_secrets
    f=configured;create_human(f,identity_manifest(f))
    def snapshot():
        return (admin.execute('select application_id,active from control.nexloop_browser_applications order by 1').fetchall(),
                admin.execute('select application_id,enabled,maximum_accounts,created_accounts,operator_label,idempotency_key_digest from control.nexloop_initial_identity_allowances order by 1').fetchall())
    def rates():
        return dict((a,(m,w,act)) for a,m,w,act in admin.execute("select action,maximum_attempts,window_seconds,active from control.nexloop_browser_rate_policies where operator_principal_id='nexloop_identity'"))
    def state():
        return S.browser_state(admin,f['tenant'])
    # As on stage: local_login 20/60 only (the fixture publishes local_login alone), the allowance full and closed.
    admin.execute("update control.nexloop_browser_rate_policies set maximum_attempts=20 where action='local_login'")
    assert set(rates())=={'local_login'}
    before=snapshot();assert before[1][0][1:4]==(False,1,1)
    entries=S.rate_policy_entries()
    pending,conflicts=S._browser_pending(entries,state())
    assert conflicts==[] and sorted(p['action'] for p in pending['browser_rate_policies'])==['invitation_accept','oidc_start','password_reset_complete']
    revision=admin.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(f['tenant'],)).fetchone()[0]
    import uuid as _u
    body={'schema_version':'1.0','manifest_id':str(_u.uuid4()),'tenant_id':f['tenant'],'expected_revision':revision,'tenant_status':'active',
          'object_types':[],'actions':[],'functions':[],'authority_facts':[],'service_credentials':[],'browser_business_applications':[],**pending}
    apply_manifest_with_secrets(body,database_url_file=f['paths']['dsn'],signing_key_file=f['paths']['signing'],signing_key_id='explicit-configuration',service_secrets={})
    assert rates()=={'local_login':(20,60,True),'password_reset_complete':(10,300,True),'invitation_accept':(10,300,True),'oidc_start':(20,300,True)}
    assert snapshot()==before  # no application and no allowance touched; the closed allowance stays closed
    assert S._browser_pending(entries,state())==({'browser_applications':[],'browser_rate_policies':[],'identity_allowances':[]},[])  # a rerun publishes nothing


@pytest.mark.parametrize('role',['nexloop_api','nexloop_identity','nexloop_domain_worker','nexloop_action_worker','nexloop_scheduler','nexloop_runtime'])
def test_runtime_and_service_roles_cannot_publish_or_read_configuration(configured,role):
    with psycopg.connect(make_conninfo(configured['pg'],user=role)) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege),db.transaction():db.execute('select * from control.nexloop_configuration_publications')
        with pytest.raises(psycopg.errors.InsufficientPrivilege),db.transaction():db.execute("select control.nexloop_configure_manifest('x','{}','x','x',decode(repeat('00',32),'hex'),'{}')")


@pytest.mark.parametrize('key',['missing','wrong','public_digest_only'])
def test_initial_identity_requires_actual_private_key_possession(configured,admin,key):
    f=configured;body=identity_manifest(f)
    body['membership']['subject_id']=body['subject']['subject_id'];body['account']['subject_id']=body['subject']['subject_id']
    from argon2 import PasswordHasher
    from nexloop_eios.initial_identity import validate_identity_manifest
    from nexloop_eios.postgres_artifacts import canonical_payload
    body=validate_identity_manifest(body)
    supplied=None if key=='missing' else (bytes.fromhex(f['manifest']['identity_allowances'][0]['idempotency_key_digest']) if key=='public_digest_only' else secrets.token_bytes(32))
    # Actual ordinary identity-role connection, not the private-key CLI. Neither
    # its public manifest nor arbitrary Argon2/fingerprint permits enrollment.
    with psycopg.connect(make_conninfo(f['pg'],user='nexloop_identity')) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege),db.transaction():
            db.execute('select control.nexloop_initial_identity_create(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s)',
             (f['tenant'],f['application'],'nexloop_identity',body['request_id'],body['operator_label'],canonical_payload(body['subject']),canonical_payload(body['membership']),canonical_payload(body['account']),PasswordHasher().hash('synthetic-password-not-a-credential'), '0'*64,supplied))
    assert admin.execute('select count(*) from control.nexloop_initial_identity_receipts').fetchone()[0]==0
    assert admin.execute('select enabled,created_accounts from control.nexloop_initial_identity_allowances').fetchone()==(True,0)


def test_cli_missing_or_wrong_private_key_rejected_without_creation(configured,admin):
    f=configured;body=identity_manifest(f);p=f['paths'];original=p['seal'].read_text()
    p['seal'].write_text(secrets.token_hex(32))
    with pytest.raises(ConfigurationRejected):create_human(f,body)
    p['seal'].unlink()
    with pytest.raises(ConfigurationRejected):create_human(f,body)
    assert admin.execute('select count(*) from control.nexloop_initial_identity_receipts').fetchone()[0]==0


def test_identity_children_failure_rolls_back_receipt_quota_and_all_identity_rows(configured,admin):
    f=configured
    admin.execute("create function public.reject_initial_account() returns trigger language plpgsql as $$begin raise exception 'synthetic account rejection';end$$")
    admin.execute('create trigger reject_initial_account before insert on control.nexloop_browser_accounts for each row execute function public.reject_initial_account()')
    with pytest.raises(ConfigurationRejected):create_human(f,identity_manifest(f))
    for table in ('initial_identity_receipts','browser_subjects','browser_memberships','browser_accounts'):
        assert admin.execute('select count(*) from control.nexloop_'+table).fetchone()[0]==0
    assert admin.execute('select enabled,created_accounts from control.nexloop_initial_identity_allowances').fetchone()==(True,0)


def test_ordinary_identity_role_cannot_insert_subject_or_forge_initial_receipt(configured):
    with psycopg.connect(make_conninfo(configured['pg'],user='nexloop_identity')) as db:
        for statement in ("insert into control.nexloop_browser_subjects values('forged','{}')",'select * from control.nexloop_initial_identity_receipts'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege),db.transaction():db.execute(statement)


def test_an_erased_members_initial_identity_receipt_is_refused_with_42501(configured,admin):
    """NX-058 slice E (L6 review of E1): the same request id run again for a subject the staff erasure took is refused outright
    (42501 'initial identity erased'), never answered with the old ids and never the retryable 40001 conflict."""
    import hmac
    from argon2 import PasswordHasher
    from nexloop_eios.initial_identity import validate_identity_manifest
    from nexloop_eios.postgres_artifacts import canonical_payload
    f=configured;body=identity_manifest(f);created=create_human(f,body);assert created['created'] is True
    admin.execute("select set_config('eios.tenant_id',%s,false)",(f['tenant'],))
    admin.execute("insert into control.nexloop_workbench_member_erasures(tenant_id,subject_id,principals,erased_at) values(%s,%s,%s,clock_timestamp())",
                  (f['tenant'],created['subject_id'],[created['principal_id']]))
    with pytest.raises(ConfigurationRejected):create_human(f,body)  # the operator's tool: refused
    value=validate_identity_manifest(body);password=f['paths']['password'].read_text();seal=bytes.fromhex(f['paths']['seal'].read_text().strip())
    fingerprint=hmac.new(seal,canonical_payload({'domain':'nexloop-initial-password-v1','tenant_id':value['tenant_id'],'application_id':value['application_id'],
        'request_id':value['request_id'],'password':password}).encode(),'sha256').hexdigest()
    with psycopg.connect(make_conninfo(f['pg'],user='nexloop_identity')) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege,match='initial identity erased'),db.transaction():
            db.execute('select control.nexloop_initial_identity_create(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s)',
             (value['tenant_id'],value['application_id'],'nexloop_identity',value['request_id'],value['operator_label'],canonical_payload(value['subject']),
              canonical_payload(value['membership']),canonical_payload(value['account']),PasswordHasher().hash(password),fingerprint,seal))
