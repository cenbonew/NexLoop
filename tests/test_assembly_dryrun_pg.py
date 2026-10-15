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
    st.published = A.publish_catalog(out, tenant=TENANT, bootstrap_dsn_file=st.bootstrap_dsn, signing_key_id='stage-1')
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


def assemble(st):
    """publish (fixture) -> human -> business, catalog, enterprise setup -> relay enrollment; st.result holds each tool's output."""
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
    return st


@pytest.fixture
def assembled(stage):
    return assemble(stage)


def test_publish_then_business_catalog_and_enterprise_setup_and_enrollment(assembled, admin):
    r = assembled.result
    assert r['business']['configured'] and r['catalog']['configured'] and r['enterprise']['configured'] and r['enrollment']['enrolled']
    assert r['business']['owner_ref'] == 'principal:nexloop-service-business-owner-principal'
    assert r['business']['executor_ref'] == 'principal:nexloop-service-effect-executor-principal'
    assert r['catalog']['source_ref'] == 'principal:nexloop-service-relay-source-principal'
    # Only governed Actions wrote the formal objects (one Consumer, its two EffectControls, two offerings and bindings, ownership).
    counts = dict(admin.execute("select type_name, count(*) from ontology.objects where tenant_id=%s group by 1", (TENANT,)).fetchall())
    assert counts == {'Consumer': 1, 'EffectControl': 2, 'ServiceOffering': 2, 'ConsumerServiceOffering': 2, 'ConsumerOwnership': 1}


def slot_placeholders(slot):
    return {'ENTERPRISE_CONTROL_ID_FROM_ENTERPRISE_SETUP': slot['enterprise_control_id'], 'ENTERPRISE_OFFERING_ID_FROM_ENTERPRISE_SETUP': slot['enterprise_offering_id'],
            'ENTERPRISE_BINDING_ID_FROM_ENTERPRISE_SETUP': slot['enterprise_binding_id']}


def tenant_configuration(st, admin, capsys):
    """tenant_config recall-types and relay-run-profile. The RoleDefinition is inserted by the test administrator: no stage principal
    holds RoleDefinition.create (gap G-ROLE in the gap list)."""
    from test_tenant_config_pg import T
    now = datetime.now(UTC)
    role = 'd' * 64
    from psycopg.types.json import Jsonb
    admin.execute("insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,created_at,updated_at) values(%s,'real','RoleDefinition',%s,1,%s,now(),now())",
                  (TENANT, role, Jsonb({'name': 'operations', 'responsibility': 'reply to consumers', 'ceiling_ref': 'ceiling:stage', 'active': True,
                                        'valid_from': (now - timedelta(days=1)).isoformat(), 'valid_until': (now + timedelta(days=30)).isoformat()})))
    files = {'recall-types.json': st.root / 'recall-types.json', 'relay-run-profile.json': st.root / 'relay-run-profile.json'}
    common = ['--tenant', TENANT, '--bootstrap-dsn-file', str(st.bootstrap_dsn)]
    assert T.main(['recall-types', *common, '--out', str(files['recall-types.json'])]) == 0
    assert T.main(['relay-run-profile', *common, '--out', str(files['relay-run-profile.json']), '--role-id', role, '--maximum-cost', '2', '--currency', 'CNY']) == 0
    capsys.readouterr()
    return files


def run_workers(st, admin, capsys, names, replace=None):
    import subprocess
    compose = A.compose_services()
    config = st.root / 'config'
    A.install_config(config, tenant_configuration(st, admin, capsys))
    variables = {'WEBCHAT_DELIVERY_PORT': str(A.free_port()), 'GUARD_PORT': str(A.free_port()), 'HOST_INTERNAL_PORT': str(A.free_port())}
    overlay = A.manual_files(st, st.root / 'manual', variables)
    results, servers = {}, []
    try:
        for name in names:
            try:
                argv, env = A.worker_command(st, name, compose, config, st.root / ('w-' + name), variables, overlay,
                                                 (replace or {}).get(name))
            except A.Skip as skip:
                results[name] = ('skip', str(skip))
                continue
            if name in A.SERVERS:
                results[name] = start_server(argv, env, int(variables[A.SERVERS[name]]), servers)
                continue
            try:
                done = subprocess.run(argv, capture_output=True, text=True, timeout=90, env=env)
                results[name] = (done.returncode, (done.stdout[-600:] + '|' + done.stderr[-900:]).replace('\n', ' '))
            except subprocess.TimeoutExpired:
                results[name] = ('timeout', '')
    finally:
        for process in servers:
            process.terminate()
            process.wait(timeout=10)
    return results


def start_server(argv, env, port, servers):
    """A server service: started (kept running for the services after it); ready when its port accepts a connection (30 s)."""
    import socket
    import subprocess
    import time
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    servers.append(process)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and process.poll() is None:
        try:
            socket.create_connection(('127.0.0.1', port), timeout=0.5).close()
            return (0, 'listening')
        except OSError:
            time.sleep(0.2)
    if process.poll() is not None:
        return (process.returncode, (process.stdout.read()[-300:] + '|' + process.stderr.read()[-900:]).replace('\n', ' '))
    return ('timeout', '')


WORKERS = ('webchat-delivery', 'action-worker', 'runtime-worker', 'message-relay', 'outbound-recorder', 'claim-extraction-scheduler',
           'claim-extraction-worker', 'claim-matcher', 'recall-indexer', 'plan-reevaluator', 'reply-guarantor', 'commitment-keeper',
           'commercial-recorder', 'retention-keeper', 'alert-evaluator', 'incentive-issuer', 'domain-worker')


# Gap ids: docs/release/assembly-dryrun-gaps.md. Everything else must complete its first round (exit 0).
EXPECTED_GAPS = {
    'claim-extraction-worker': ('skip', 'secret not generated in phase A: claim_extraction_worker_model'),  # G5: model.env, phase C
    'claim-matcher': ('skip', 'secret not generated in phase A: claim_matcher_model'),                      # G5: model.env, phase C
}


def test_each_worker_first_round(assembled, admin, capsys):
    results = run_workers(assembled, admin, capsys, WORKERS)
    with capsys.disabled():
        for name, (code, detail) in results.items():
            print(f'\nWORKER {name}: {code} {detail[:400]}')
    for name in WORKERS:
        code, detail = results[name]
        if name in EXPECTED_GAPS:
            assert code == EXPECTED_GAPS[name][0] and EXPECTED_GAPS[name][1] in detail, (name, code, detail)
        else:
            assert code == 0, (name, detail)
    assert 'idle' in results['message-relay'][1] and '"indexed":1' in results['recall-indexer'][1]  # the Consumer indexed for recall


def test_runtime_worker_first_round_on_the_granted_queue(assembled, admin, capsys):
    """D-QUEUE: the same Compose command with --queue operations (the queue the runtime worker is granted and the relay and the
    Role queue accept into) completes its first round."""
    results = run_workers(assembled, admin, capsys, ('runtime-worker',), {'runtime-worker': {'--queue': 'operations'}})
    assert results['runtime-worker'][0] == 0 and '"status":"idle"' in results['runtime-worker'][1], results


def test_the_webchat_consumer_has_no_business_authority_after_assembly(assembled, admin):
    """G-WEBCHAT-AUTH: a webchat human's requests are authorized through the browser business authority snapshot (0045), which
    needs a control.nexloop_browser_business_applications row for the browser application and the human's grant facts. The stage
    tools register neither (stage_secrets browser: 'not registered here'; register/workbench_roles cover services and staff), so
    the assembled consumer cannot open a conversation or send a message yet."""
    assert admin.execute('select count(*) from control.nexloop_browser_business_applications where tenant_id=%s', (TENANT,)).fetchone() == (0,)
    human = admin.execute('select principal_id from control.nexloop_consumer_owners where tenant_id=%s', (TENANT,)).fetchone()[0]
    grants = admin.execute("select count(*) from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and %s = any(entity_key)",
                           (TENANT, human)).fetchone()
    assert grants == (0,)
    # The same query finds the grant facts of a service principal (the check is not vacuous).
    assert admin.execute("select count(*) from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and %s = any(entity_key)",
                         (TENANT, 'nexloop-service-business-owner-principal')).fetchone()[0] > 0
