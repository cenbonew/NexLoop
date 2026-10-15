"""NX-032 assembly dry run, end to end on a clean catalog PostgreSQL (see tests/assembly_dryrun_fixture.py for the path and the
test-only pieces). Each step runs the production tool; a step that cannot run yet is a gap of the stage run, recorded in
docs/release/assembly-dryrun-gaps.md with the assertion that pins it here."""
import json
import secrets as random
from datetime import UTC, datetime, timedelta

import pytest

from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A
from test_stage_secrets import S, tree  # noqa: F401  (S puts scripts/deploy on sys.path)
import assembly_dryrun_fixture as A  # noqa: E402

TENANT = TENANT_A


@pytest.fixture
def stage(pg, admin, tree, tmp_path):  # noqa: F811
    import shutil
    out = tmp_path / 'stage'
    shutil.copytree(tree['out'], out)  # generate + setup-credentials (module fixture)
    bootstrap(admin)
    st = A.Stage(pg, tmp_path, out, TENANT)
    expires = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    assert S.main(['register', '--out', str(out), '--tenant', TENANT, '--bootstrap-dsn-file', str(st.bootstrap_dsn), '--signing-key-id', 'stage-1',
                   '--credential-expires-at', expires]) == 0
    seal = A.private(tmp_path, 'identity-seal', random.token_hex(32))
    assert S.main(['browser', '--out', str(out), '--tenant', TENANT, '--browser-application-id', 'stage-webchat', '--workbench-application-id',
                   'stage-workbench', '--identity-seal-file', str(seal), '--bootstrap-dsn-file', str(st.bootstrap_dsn), '--signing-key-id', 'stage-1']) == 0
    st.seal = seal
    st.published = A.publish_system_catalog(out, tenant=TENANT, bootstrap_dsn_file=st.bootstrap_dsn, signing_key_id='stage-1')
    return st


def create_human(st):
    """identity-human.example.json through initial_identity --create (the browser registration's operator path)."""
    from configurator_window import write_private  # noqa: F401
    now = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    body = A.filled('identity-human.example.json', TENANT_ID=TENANT, BROWSER_APPLICATION_ID='stage-webchat', NEW_UUID=A.new_uuid(),
                    OPERATOR_LABEL='stage-owner', SUBJECT_ID='dryrun-human-' + A.new_uuid(), NOW_UTC_ISO=now, PRINCIPAL_ID='dryrun-principal-' + A.new_uuid(),
                    LOCAL_ACCOUNT_ID='dryrun-account-' + A.new_uuid(), USERNAME='dryrun-consumer', EMAIL='consumer@example.invalid')
    manifest = A.private(st.root, 'identity-human.json', json.dumps(body))
    identity_dsn = A.private(st.root, 'identity-dsn', A.make_conninfo(st.pg, user='nexloop_identity'))
    password = A.private(st.root, 'human-password', A.token())
    code, out, err = A.run_tool('nexloop_eios.initial_identity', ['--manifest', str(manifest), '--create', '--identity-database-url-file', str(identity_dsn),
                                                                  '--password-file', str(password), '--idempotency-key-file', str(st.seal)])
    assert code == 0, (out, err)
    return body['membership']['principal_id']


def test_publish_then_business_catalog_and_enterprise_setup(stage, admin):
    st = stage
    assert st.published['actions'] > 30 and st.published['functions'] >= 1
    human = create_human(st)
    end = A.end_of_trial()
    business = A.private(st.root, 'business-recipe.json', json.dumps(A.filled('business-recipe.example.json', NEW_UUID=A.new_uuid(),
                                                                              CONSUMER_PRINCIPAL_ID=human, TRIAL_PERIOD_END_UTC_ISO=end)))
    code, out, err = A.run_tool('nexloop_eios.business_setup', [*st.backend_args(), '--owner-credential-file', str(st.setup_file('business-owner-credential')),
                                                                '--executor-credential-file', str(st.setup_file('business-executor-credential')),
                                                                '--recipe-file', str(business)])
    assert code == 0, (out, err)
    setup = json.loads(out)
    catalog = A.private(st.root, 'catalog-recipe.json', json.dumps(A.filled('catalog-recipe.example.json', NEW_UUID=A.new_uuid(),
                                                                            CONSUMER_ID_FROM_BUSINESS_SETUP=setup['consumer_id'], TRIAL_PERIOD_END_UTC_ISO=end)))
    code, out, err = A.run_tool('nexloop_eios.catalog_setup', [*st.backend_args(), '--maintainer-credential-file', str(st.setup_file('catalog-maintainer-credential')),
                                                               '--source-credential-file', str(st.setup_file('catalog-source-credential')),
                                                               '--recipe-file', str(catalog)])
    assert code == 0, (out, err)
    offering = json.loads(out)
    st.result = {'business': setup, 'catalog': offering}
    enterprise = A.private(st.root, 'enterprise-recipe.json', json.dumps(A.filled(
        'enterprise-setup-recipe.example.json', NEW_UUID=A.new_uuid(), CONSUMER_ID_FROM_BUSINESS_SETUP=setup['consumer_id'],
        SERVICE_CODE_SUFFIX='dryrun', OFFERING_TITLE='Synthetic enterprise offering', CNY_PRICE_AMOUNT='10.00', TRIAL_PERIOD_END_UTC_ISO=end)))
    code, out, err = A.run_tool('nexloop_eios.enterprise_setup', [*st.backend_args(), '--owner-credential-file', str(st.setup_file('business-owner-credential')),
                                                                  '--executor-credential-file', str(st.setup_file('business-executor-credential')),
                                                                  '--maintainer-credential-file', str(st.setup_file('catalog-maintainer-credential')),
                                                                  '--source-credential-file', str(st.setup_file('catalog-source-credential')),
                                                                  '--recipe-file', str(enterprise)])
    assert code == 0, (out, err)
    slot = json.loads(out)
    st.result['enterprise'] = slot
    # Relay enrollment by the EffectControl owner (business_owner), with the enterprise slot.
    manifest, _ = S.merged_manifest()
    relay_principal = next(p['principal_id'] for p in manifest['principals'] if p['role'] == 'message_relay')
    enrollment = A.filled('relay-enrollment-enterprise.example.json', CONSUMER_ID_FROM_BUSINESS_SETUP=setup['consumer_id'],
                          CONTROL_ID_FROM_BUSINESS_SETUP=setup['control_id'], OFFERING_ID_FROM_CATALOG_SETUP=offering['offering_id'],
                          BINDING_ID_FROM_CATALOG_SETUP=offering['binding_id'], RELAY_SERVICE_PRINCIPAL_ID=relay_principal, NEW_UUID=A.new_uuid(),
                          **{k: v for k, v in slot_placeholders(slot).items()})
    enroll = A.private(st.root, 'relay-enrollment.json', json.dumps(enrollment))
    code, out, err = A.run_tool('nexloop_eios.relay_enrollment', ['--database-url-file', str(st.api_dsn), '--signing-key-file', str(st.signing_key),
                                                                  '--signing-key-id', 'stage-1', '--artifact-root', str(st.artifacts),
                                                                  '--owner-credential-file', str(st.setup_file('business-owner-credential')),
                                                                  '--enrollment-file', str(enroll)])
    assert code == 0, (out, err)
    st.result['enrollment'] = json.loads(out)
    print(json.dumps(st.result, sort_keys=True))


def slot_placeholders(slot):
    return {'ENTERPRISE_CONTROL_ID_FROM_ENTERPRISE_SETUP': slot['enterprise_control_id'], 'ENTERPRISE_OFFERING_ID_FROM_ENTERPRISE_SETUP': slot['enterprise_offering_id'],
            'ENTERPRISE_BINDING_ID_FROM_ENTERPRISE_SETUP': slot['enterprise_binding_id']}
