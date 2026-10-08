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
def configured(pg,admin,tmp_path):
    bootstrap(admin)
    admin.execute('set log_parameter_max_length=0')
    admin.execute('alter system set log_parameter_max_length=0')
    admin.execute('alter system set log_parameter_max_length_on_error=0')
    admin.execute('select pg_reload_conf()')
    with psycopg.connect(pg) as clean:
        assert clean.execute('show log_parameter_max_length').fetchone()[0]=='0'
        assert clean.execute('show log_parameter_max_length_on_error').fetchone()[0]=='0'
    admin.execute('alter role nexloop_configurator login')
    tenant=str(uuid.uuid4());target='eios:artifact:explicit-configuration-source'
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
