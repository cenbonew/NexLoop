"""Draft pure contract tests, no PG/bootstrap/authority stub."""
import json,uuid
import pytest
from nexloop_eios.trusted_configuration import validate_manifest,strict_json,ConfigurationRejected


def empty_manifest():
    return {'schema_version':'1.0','manifest_id':str(uuid.uuid4()),'tenant_id':'synthetic-config-tenant','expected_revision':0,
        'tenant_status':'active','object_types':[],'actions':[],'functions':[],'authority_facts':[],'service_credentials':[],'browser_applications':[],'browser_business_applications':[],'browser_rate_policies':[],'identity_allowances':[]}


def test_explicit_configuration_empty_manifest_is_valid_no_generated_authority():
    result=validate_manifest(empty_manifest())
    assert result['authority_facts']==[] and result['service_credentials']==[]


@pytest.mark.parametrize('extra',['objects','consumer','goal','run_token','MODEL_API_KEY','database_url'])
def test_business_or_secret_fields_never_enter_public_manifest(extra):
    body=empty_manifest();body[extra]='synthetic-rejected'
    with pytest.raises(ConfigurationRejected):validate_manifest(body)


def test_unknown_and_duplicate_json_fields_rejected_without_echo():
    with pytest.raises(ConfigurationRejected,match='^configuration_rejected$'):strict_json('{"tenant_id":"one","tenant_id":"two"}')
    with pytest.raises(ConfigurationRejected):strict_json('{"value":NaN}')


@pytest.mark.parametrize('revision',[True,-1,'0',None])
def test_expected_revision_is_strict_real_integer(revision):
    body=empty_manifest();body['expected_revision']=revision
    with pytest.raises(ConfigurationRejected):validate_manifest(body)


def test_fake_human_service_credential_cannot_be_configured():
    body=empty_manifest();body['service_credentials']=[{'reference':'synthetic-human','binding':{'subject_kind':'human'},'worlds':['real'],'expires_at':'2099-01-01T00:00:00Z','status':'active'}]
    with pytest.raises(ConfigurationRejected):validate_manifest(body)


# Actual PG draft acceptance, not executed/passed:
# - fresh restricted configurator LOGIN role from disposable reviewed bootstrap,
#   apply typed canonical manifest -> actual service authenticate/fullproof.
# - raw application/runtime/identity roles cannot execute definer/direct tables.
# - equal manifest+secret refs replay, payload/secret conflict rollback all rows.
# - expected tenant epoch race rejects, no partial schemas/authority/credentials.
# - actual sameversion Action/Function conflict immutability and epoch invalidation.
# - dedicated identity_operator creates a real Human; configurator may configure
#   its canonical grants only after actual directory existence, never createHuman.
# - createConsumer/Goal/PlanStep/EffectControl/Ownership through existing governed
#   actions using actually provisioned service grants, never config table writes.
# - stdout/stderr/public audit contain no service token/signing material/DSN.


def initial_identity_manifest():
    from datetime import UTC,datetime,timedelta
    now=(datetime.now(UTC)-timedelta(seconds=1)).isoformat()
    return {'schema_version':'1.0','tenant_id':'synthetic-initial-tenant','application_id':'synthetic-login','request_id':str(uuid.uuid4()),'operator_label':'explicit-technical-owner',
        'subject':{'subject_id':'explicit-human','kind':'human','status':'active','created_at':now,'updated_at':now,'revision':1},
        'membership':{'tenant_id':'synthetic-initial-tenant','subject_id':'explicit-human','principal_id':'explicit-principal','kind':'home','status':'active','valid_from':now,'valid_until':None,'trusted_attributes':{},'revision':1},
        'account':{'tenant_id':'synthetic-initial-tenant','local_account_id':'explicit-account','subject_id':'explicit-human','username':'operator-chosen-username','verified_email':'operator@example.invalid','status':'active','failed_attempts':0,'lockout_level':0,'locked_until':None,'must_change_password':True,'session_epoch':1,'created_at':now,'updated_at':now,'revision':1}}


def test_initial_identity_requires_explicit_actual_human_contract_no_credential_in_manifest():
    from nexloop_eios.initial_identity import validate_identity_manifest
    value=validate_identity_manifest(initial_identity_manifest())
    assert value['subject']['kind']=='human' and 'password_hash' not in value['account'] and 'password_history' not in value['account']


@pytest.mark.parametrize('field',['password_hash','password_history'])
def test_initial_identity_public_password_fields_denied(field):
    from nexloop_eios.initial_identity import validate_identity_manifest
    body=initial_identity_manifest();body['account'][field]='synthetic-rejected'
    with pytest.raises(ConfigurationRejected):validate_identity_manifest(body)


def test_initial_identity_cannot_create_service_as_human_or_trust_self_claimed_attributes():
    from nexloop_eios.initial_identity import validate_identity_manifest
    body=initial_identity_manifest();body['subject']['kind']='service'
    with pytest.raises(ConfigurationRejected):validate_identity_manifest(body)
    body=initial_identity_manifest();body['membership']['trusted_attributes']={'administrator':True}
    with pytest.raises(ConfigurationRejected):validate_identity_manifest(body)


def test_browser_config_binding_and_allowance_are_strict():
    body=empty_manifest();body['browser_applications']=[{'application_id':'explicit-login','active':True}]
    body['identity_allowances']=[{'application_id':'explicit-login','enabled':True,'maximum_accounts':1,'operator_label':'operator-explicit','idempotency_key_digest':'a'*64}]
    validate_manifest(body)
    body['identity_allowances'][0]['maximum_accounts']=True
    with pytest.raises(ConfigurationRejected):validate_manifest(body)


# Additional REAL PG tests pending registration/approval, not executed:
# configurator publishes actual browser appmap/ratepolicies; restricted identity
# operator creates explicitly declared Human subject/membership/account once,
# actual original LocalAccountService password login succeeds and has no Grant;
# same request/same private password HMAC replay adds no account/audit; wrong
# password, changed payload, wrong seal key, foreigntenant and identityrole deny;
# quota consumed/explicitly closed allowance forbids further initial accounts;
# admin cannot reopen/reset quota by manifest revision; all partial failures roll
# back identity rows/receipt/quota/audit. Argon2 hash is only private account column;
# password fingerprint is HMAC with owned external random32byte idempotency key,
# context tenant/application/request/password; raw password/key never PG/log/audit.
