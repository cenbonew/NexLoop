"""NX-032 assembly dry run, end to end on a clean catalog PostgreSQL (see tests/assembly_dryrun_fixture.py for the path and the
test-only pieces). Each step runs the production tool; a step that cannot run yet is a gap of the stage run, recorded in
docs/release/assembly-dryrun-gaps.md with the assertion that pins it here."""
import json
import secrets as random
from datetime import UTC, datetime, timedelta

import pytest

from nexloop_eios.bootstrap import bootstrap
from support.budgets import approved_budget
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


def create_human(st, *, application='stage-webchat', username='dryrun-consumer', label='human'):
    """identity-human.example.json through initial_identity --create (the browser registration's operator path). The consumer is
    'human' (identity-human.json, human-password); another Human gets its own files (identity-<label>.json, <label>-password)."""
    from configurator_window import write_private  # noqa: F401
    now = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    body = A.filled('identity-human.example.json', TENANT_ID=TENANT, BROWSER_APPLICATION_ID=application, NEW_UUID=A.new_uuid(),
                    OPERATOR_LABEL='stage-owner', SUBJECT_ID=f'dryrun-{label}-' + A.new_uuid(), NOW_UTC_ISO=now, PRINCIPAL_ID=f'dryrun-{label}-principal-' + A.new_uuid(),
                    LOCAL_ACCOUNT_ID=f'dryrun-{label}-account-' + A.new_uuid(), USERNAME=username, EMAIL=f'{label}@example.invalid')
    manifest = A.private(st.root, 'identity-human.json' if label == 'human' else f'identity-{label}.json', json.dumps(body))
    identity_dsn = st.root / 'identity-dsn'
    if not identity_dsn.exists():
        A.private(st.root, identity_dsn.name, A.make_conninfo(st.pg, user='nexloop_identity'))
    password = A.private(st.root, 'human-password' if label == 'human' else f'{label}-password', A.token())
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


def assemble_role(st):
    """G-ROLE: role-assembly.example.json through nexloop_eios.role_assembly as role_assembler, then the owner enrolls the consumer again
    with role_id, link_id and step_id (relay-enrollment example plus the three role keys). Once per dry run: a later call returns the
    same Role (run_workers configures the workers again)."""
    r = st.result
    if 'role' in r:
        return r['role']
    recipe = A.filled('role-assembly.example.json', NEW_UUID=A.new_uuid(), CONSUMER_ID_FROM_BUSINESS_SETUP=r['business']['consumer_id'],
                      CONTROL_ID_FROM_BUSINESS_SETUP=r['business']['control_id'], NOW_UTC_ISO=(datetime.now(UTC) - timedelta(seconds=5)).replace(microsecond=0).isoformat(),
                      TRIAL_PERIOD_END_UTC_ISO=A.end_of_trial(), ROLE_NAME='operations', ROLE_RESPONSIBILITY='reply to the consumer within the plan',
                      PER_RUN_COST_CAP='2', TRIAL_CURRENCY='CNY')
    path = A.private(st.root, 'role-assembly.json', json.dumps(recipe))
    argv = [*st.backend_args(), '--assembler-credential-file', str(st.setup_file('role-assembler-credential')), '--recipe-file', str(path)]
    code, out, err = A.run_tool('nexloop_eios.role_assembly', argv)
    assert code == 0, (out, err)
    role = json.loads(out)
    again = A.run_tool('nexloop_eios.role_assembly', argv)  # an identical replay returns the same objects
    assert again[0] == 0 and json.loads(again[1]) == role, again
    manifest, _ = S.merged_manifest()
    relay_principal = next(p['principal_id'] for p in manifest['principals'] if p['role'] == 'message_relay')
    enrollment = A.filled('relay-enrollment-enterprise.example.json', CONSUMER_ID_FROM_BUSINESS_SETUP=r['business']['consumer_id'],
                          CONTROL_ID_FROM_BUSINESS_SETUP=r['business']['control_id'], OFFERING_ID_FROM_CATALOG_SETUP=r['catalog']['offering_id'],
                          BINDING_ID_FROM_CATALOG_SETUP=r['catalog']['binding_id'], RELAY_SERVICE_PRINCIPAL_ID=relay_principal, NEW_UUID=A.new_uuid(),
                          **slot_placeholders(r['enterprise']))
    enrollment.update(role_id=role['role_id'], link_id=role['link_id'], step_id=role['step_id'])
    enroll = A.private(st.root, 'relay-enrollment-role.json', json.dumps(enrollment))
    code, out, err = A.run_tool('nexloop_eios.relay_enrollment', ['--database-url-file', str(st.api_dsn), '--signing-key-file', str(st.signing_key),
                                                                  '--signing-key-id', 'stage-1', '--artifact-root', str(st.artifacts),
                                                                  '--owner-credential-file', str(st.setup_file('business-owner-credential')),
                                                                  '--enrollment-file', str(enroll)])
    assert code == 0, (out, err)
    st.result['role'] = role
    return role


def tenant_configuration(st, admin, capsys):
    """tenant_config recall-types and relay-run-profile, the latter from the Role assembled by role_assembly (G-ROLE)."""
    from test_tenant_config_pg import T
    role = assemble_role(st)['role_id']
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


def apply_webchat_consumer_role(st, admin):
    """G-WEBCHAT-AUTH: scripts/deploy/webchat_consumers.py through the configurator window, with the identity Human of the dry run as
    the one simulated consumer (deploy/examples/nx037/webchat-consumers.example.json)."""
    human = json.loads((st.root / 'identity-human.json').read_text())
    recipe = st.root / 'webchat-consumers.json'
    if not recipe.exists():
        A.private(st.root, recipe.name, json.dumps(A.filled(
            'webchat-consumers.example.json', TENANT_ID=TENANT, BROWSER_APPLICATION_ID='stage-webchat',
            PRINCIPAL_ID_FROM_IDENTITY_HUMAN=human['membership']['principal_id'], SUBJECT_ID_FROM_IDENTITY_HUMAN=human['subject']['subject_id'])))
    import subprocess
    import sys
    argv = [sys.executable, str(A.ROOT / 'scripts/deploy/webchat_consumers.py'), '--role', str(A.ROOT / 'deploy/authorization/webchat-consumer-role.v1.json'),
            '--recipe', str(recipe), '--apply', '--bootstrap-dsn-file', str(st.bootstrap_dsn), '--signing-key-file',
            str(st.out / 'management' / 'signing.key.hex'), '--signing-key-id', 'stage-1']
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert admin.execute("select rolcanlogin, rolpassword is null from pg_authid where rolname='nexloop_configurator'").fetchone() == (False, True)
    return json.loads(done.stdout), human


def test_the_webchat_consumer_opens_a_conversation_and_writes_after_the_consumer_role_is_applied(assembled, admin, capsys):
    """G-WEBCHAT-AUTH, closed: before the consumer role the assembled consumer has no business authority (no 0045 application row, no
    grant facts); webchat_consumers registers both through 0050, and the consumer then opens a conversation and writes a message
    through the real /api/v1 routes of the stage API, and the relay's first round takes it as input."""
    st = assembled
    assert admin.execute('select count(*) from control.nexloop_browser_business_applications where tenant_id=%s', (TENANT,)).fetchone() == (0,)
    first, human = apply_webchat_consumer_role(st, admin)
    again, _ = apply_webchat_consumer_role(st, admin)  # same input, same state
    principal = human['membership']['principal_id']
    assert first['configured'] and first['granted'] == 1 and first['personas'] == ['simulated_agent'] and again['granted'] == 1
    assert admin.execute('select caller_application_id, application_version, requested_scopes from control.nexloop_browser_business_applications where tenant_id=%s',
                         (TENANT,)).fetchall() == [('eios:application:nexloop-webchat', '1', ['action.execute', 'function.execute'])]
    granted = {row[0] for row in admin.execute("select entity_key[2] from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and entity_key[1]=%s",
                                               (TENANT, principal)).fetchall()}
    assert granted == set(json.loads((A.ROOT / 'deploy/authorization/webchat-consumer-role.v1.json').read_text())['resources'])
    message = webchat_message(st, admin, capsys)
    assert admin.execute("select count(*) from ontology.objects where tenant_id=%s and type_name='Message' and object_id=%s", (TENANT, message)).fetchone() == (1,)
    results = run_workers(st, admin, capsys, ('message-relay',))
    code, detail = results['message-relay']
    with capsys.disabled():
        print(f'\nWEBCHAT relay: {code} {detail[:400]}')
    # The relay's first round takes the message as input: it claims it, creates the Goal, the PlanSteps and the MessageAssignment,
    # and issues the Run (all through governed Actions and 0049).
    assignments = admin.execute("select count(*) from ontology.objects where tenant_id=%s and type_name='MessageAssignment' and properties->>'message_id'=%s",
                                (TENANT, message)).fetchone()
    assert assignments == (1,) and '"status":"idle"' not in detail, (assignments, detail)
    # Without the tenant's Context strategy the round then stops at the v6 context preparation (the relay names
    # recent_plus_required) and gives the message back; the owner publishes it in
    # test_the_owner_publishes_the_relay_strategy_and_the_relay_issues_the_run (G-CONTEXT-STRATEGY).
    assert code == 1 and 'Message relay unavailable' in detail, detail
    assert admin.execute('select count(*) from control.nexloop_context_strategies where tenant_id=%s', (TENANT,)).fetchone() == (0,)


def webchat_message(st, admin, capsys):
    """The stage API (compose api-1, disabled execution profile) on a loopback port; the consumer logs in, opens a conversation and
    writes one message through /api/v1. Returns the Message id."""
    import httpx
    port = A.free_port()
    origin = 'https://127.0.0.1:' + str(port)
    variables = {'API_EXECUTION_PROFILE': 'disabled', 'HOST_INTERNAL_PORT': str(A.free_port()), 'API_INTERNAL_PORT': str(port), 'BROWSER_TENANT_ID': TENANT,
                 'BROWSER_APPLICATION_ID': 'stage-webchat', 'WORKBENCH_APPLICATION_ID': 'stage-workbench', 'BROWSER_ORIGIN': origin}
    argv, env = A.worker_command(st, 'api-1', A.compose_services(), st.root / 'config', st.root / 'w-api-1', variables, None,
                                 {'--web-root': str(A.ROOT / 'apps/web/dist')})
    servers = []
    try:
        started = start_server(argv, env, port, servers)
        assert started == (0, 'listening'), started
        with httpx.Client(base_url=origin, verify=False, trust_env=False, timeout=10) as client:  # loopback test API, synthetic certificate
            # The stage API trusts the login rate limit's client address only from the gateway (--trusted-proxy-peer 127.0.0.1, L6 C11):
            # this loopback client stands in for Caddy, which always writes X-NexLoop-Client-IP.
            login = client.post('/api/v1/auth/login', headers={'Origin': origin, 'X-NexLoop-Client-IP': '127.0.0.1'},
                                json={'username': 'dryrun-consumer', 'password': (st.root / 'human-password').read_text()})
            assert login.status_code == 200, login.text
            headers = {'Origin': origin, 'X-CSRF-Token': login.json()['csrf_token'], 'Idempotency-Key': 'dryrun-conversation-' + A.new_uuid()}
            conversation = client.post('/api/v1/conversations', headers=headers, json={})
            assert conversation.status_code == 200, conversation.text
            headers['Idempotency-Key'] = 'dryrun-message-' + A.new_uuid()
            sent = client.post('/api/v1/conversations/' + conversation.json()['id'] + '/messages', headers=headers, json={'body': 'synthetic first message'})
            assert sent.status_code == 202 and sent.json()['created'] is True and sent.json()['message']['status'] == 'accepted', sent.text
            return sent.json()['message']['id']
    finally:
        for process in servers:
            process.terminate()
            process.wait(timeout=10)


def test_the_assembled_role_is_what_a_role_run_binds(assembled, admin, capsys):
    """G-ROLE: role_assembly (role_assembler, ontology.roles.manage) creates the Role objects a Role Run binds, consistent with each
    other and with the consumer; the relay enrollment carries role_id, link_id and step_id (0180 checks them); tenant_config names
    that Role in the run profile. (plan-reevaluator's first round: test_each_worker_first_round, which now uses this Role.)"""
    st = assembled
    files = tenant_configuration(st, admin, capsys)
    role = st.result['role']
    objects = {r[0]: (r[1], r[2]) for r in admin.execute("select object_id, type_name, properties from ontology.objects where tenant_id=%s", (TENANT,))}
    assert objects[role['role_id']][0] == 'RoleDefinition' and objects[role['role_id']][1]['ceiling_ref'] == role['ceiling_id']
    link = objects[role['link_id']][1]
    assert (link['consumer_id'], link['role_id'], link['scope'], link['active']) == (role['consumer_id'], role['role_id'], role['scope_id'], True)
    scope, ceiling = objects[role['scope_id']][1], objects[role['ceiling_id']][1]
    assert (scope['role_id'], scope['consumer_id'], scope['goal_ids'], scope['step_ids']) == (role['role_id'], role['consumer_id'], [role['goal_id']], [role['step_id']])
    assert (ceiling['consumer_ids'], ceiling['goal_ids'], ceiling['step_ids']) == ([role['consumer_id']], [role['goal_id']], [role['step_id']])
    step = objects[role['step_id']][1]
    assert (step['control_id'], step['submitter_principals']) == (st.result['business']['control_id'], ['nexloop-service-role-source-principal'])
    assert admin.execute("select role_id, link_id, step_id from control.nexloop_relay_enrollments where tenant_id=%s order by enrollment_revision desc limit 1",
                         (TENANT,)).fetchone() == (role['role_id'], role['link_id'], role['step_id'])
    assert json.loads(files['relay-run-profile.json'].read_text())['role_ref'] == 'role:' + role['role_id']


def owner_workbench(st, admin):
    """G-WORKBENCH-AUTH: an owner Human on the workbench application, configured by scripts/deploy/workbench_roles.py --apply through
    the configurator window (members, grant facts, the workbench's 0045 business row)."""
    import subprocess
    import sys
    principal = create_human(st, application='stage-workbench', username='dryrun-owner', label='owner')
    owner = json.loads((st.root / 'identity-owner.json').read_text())
    members = A.private(st.root, 'workbench-members.json', json.dumps({
        'schema_version': 'nexloop-workbench-members/1', 'tenant_id': TENANT, 'application_id': 'stage-workbench',
        'caller_application_id': 'eios:application:stage-workbench',
        'members': [{'principal_id': principal, 'subject_id': owner['subject']['subject_id'], 'role': 'owner'}]}))
    argv = [sys.executable, str(A.ROOT / 'scripts/deploy/workbench_roles.py'), '--roles', str(A.ROOT / 'deploy/authorization/workbench-roles.v1.json'),
            '--members', str(members), '--apply', '--bootstrap-dsn-file', str(st.bootstrap_dsn), '--signing-key-file',
            str(st.out / 'management' / 'signing.key.hex'), '--signing-key-id', 'stage-1']
    for _ in range(2):  # same input, same state
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, (done.stdout, done.stderr)
        assert admin.execute("select rolcanlogin, rolpassword is null from pg_authid where rolname='nexloop_configurator'").fetchone() == (False, True)
    assert admin.execute("select caller_application_id from control.nexloop_browser_business_applications where tenant_id=%s and application_id='stage-workbench'",
                         (TENANT,)).fetchone() == ('eios:application:stage-workbench',)
    return principal


def publish_strategy(st):
    """G-CONTEXT-STRATEGY: nexloop_eios.context_strategy_setup, the owner signing in with private username and password files."""
    recipe = st.root / 'context-strategy-setup.json'
    if not recipe.exists():
        A.private(st.root, recipe.name, json.dumps(A.filled('context-strategy-setup.example.json', TENANT_ID=TENANT, WORKBENCH_APPLICATION_ID='stage-workbench')))
    username = st.root / 'owner-username'
    if not username.exists():
        A.private(st.root, username.name, 'dryrun-owner')
    password = (st.root / 'owner-password').read_text()
    code, out, err = A.run_tool('nexloop_eios.context_strategy_setup', [
        '--recipe', str(recipe), '--strategies-file', str(A.ROOT / 'deploy/configuration/context-strategies.v1.json'),
        *st.backend_args()[:6], '--artifact-root', str(st.artifacts), '--identity-database-url-file', str(st.root / 'identity-dsn'),
        '--username-file', str(username), '--password-file', str(st.root / 'owner-password'),
        '--browser-rate-key-file', str(st.out / 'app' / 'secrets' / 'api' / 'browser-rate.key'), '--browser-origin', 'https://127.0.0.1:8443'])
    assert code == 0, (out, err)
    assert password not in out + err and 'dryrun-owner' not in out + err  # credentials are never printed
    return json.loads(out)


def test_the_owner_publishes_the_relay_strategy_and_the_relay_issues_the_run(assembled, admin, capsys):
    """G-CONTEXT-STRATEGY and G-WORKBENCH-AUTH: the owner (workbench roles v11, applied with workbench_roles --apply) publishes
    recent_plus_required through context_strategy_setup (again: replayed); the owner's workbench session also submits the trial budget
    (nexloop.budget.loosen, real_tenant_init step 4; NX-056 budget approval); then the consumer's message passes the relay's v6 context preparation and its Run is issued
    and accepted (relay first round: queued)."""
    st = assembled
    owner_workbench(st, admin)
    first, again = publish_strategy(st), publish_strategy(st)
    assert first['strategy_ref'] == again['strategy_ref'] == 'context-strategy:recent_plus_required@1'
    assert first['replayed'] is False and again['replayed'] is True and first['outcome_id'] == again['outcome_id']
    assert admin.execute('select strategy_id, version, published_by from control.nexloop_context_strategies where tenant_id=%s', (TENANT,)).fetchall() == [
        ('recent_plus_required', 1, json.loads((st.root / 'identity-owner.json').read_text())['membership']['principal_id'])]
    set_trial_budget(st, admin)
    apply_webchat_consumer_role(st, admin)
    message = webchat_message(st, admin, capsys)
    results = run_workers(st, admin, capsys, ('message-relay',))
    code, detail = results['message-relay']
    with capsys.disabled():
        print(f'\nSTRATEGY relay: {code} {detail[:400]}')
    assert code == 0 and '"status":"queued"' in detail, detail
    run = admin.execute("select count(*) from runtime.nexloop_message_routes where tenant_id=%s and message_id=%s", (TENANT, message)).fetchone()
    assert run == (1,), run


def budget_cli(st, *, username='dryrun-owner', password_file=None):
    """G-BUDGET-CLI: scripts/nx037/real_tenant_init.py budget (nexloop.budget.loosen on the owner's own workbench session). Returns (code, out, err)."""
    import subprocess
    import sys
    catalog = st.root / 'value-catalog.json'
    if not catalog.exists():
        A.private(st.root, catalog.name, '{}')
    recipe = st.root / 'budget-recipe.json'
    if not recipe.exists():
        A.private(st.root, recipe.name, json.dumps({'schema_version': 'nexloop-budget-setup/1', 'tenant_id': TENANT,
                                                    'workbench_application_id': 'stage-workbench', 'browser_origin': 'https://127.0.0.1:8443'}))
    names = st.root / ('username-' + username)
    if not names.exists():
        A.private(st.root, names.name, username)
    if not hasattr(st, 'budget_period'):
        now = datetime.now(UTC).replace(microsecond=0)
        st.budget_period = ((now - timedelta(hours=1)).isoformat(), (now + timedelta(days=3)).isoformat())
    argv = [sys.executable, str(A.ROOT / 'scripts/nx037/real_tenant_init.py'), 'budget', '--tenant', TENANT, '--owner-principal', 'owner-principal-synthetic',
            '--value-catalog', str(catalog), '--channel-ref', 'channel:webchat:synthetic', '--period-start', st.budget_period[0],
            '--period-end', st.budget_period[1], '--recipe', str(recipe), '--username-file', str(names),
            '--password-file', str(password_file or st.root / 'owner-password'), '--database-url-file', str(st.api_dsn),
            '--identity-database-url-file', str(st.root / 'identity-dsn'), '--browser-rate-key-file', str(st.out / 'app' / 'secrets' / 'api' / 'browser-rate.key'),
            '--signing-key-file', str(st.signing_key), '--signing-key-id', 'stage-1', '--artifact-root', str(st.artifacts)]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60, env={**__import__('os').environ, 'PYTHONPATH': str(A.ROOT / 'packages/eios-core/src')})
    return done.returncode, done.stdout, done.stderr


def model_budget(admin):
    return admin.execute("select authz.nexloop_nx037_tenant_state(%s,'real')", (TENANT,)).fetchone()[0]['budgets'].get('model')


def set_trial_budget(st, admin):
    """real_tenant_init step 4 through `real_tenant_init.py budget` after workbench_roles --apply. NX-056 budget approval: the first
    model budget is a loosening (nexloop.budget.loosen) another owner approves; the assembly applies no approval policy, so the handler
    fails closed (`budget: approval_unavailable`, nothing written; with the policy the same call opens the approval,
    tests/test_budget_approval_pg.py). The approved write then stands in for the approval."""
    import sys
    sys.path.insert(0, str(A.ROOT / 'scripts/nx037'))
    import real_tenant_init as I
    code, out, err = budget_cli(st)
    assert code == 2 and json.loads(out) == {'ok': False, 'detail': 'budget: approval_unavailable'}, (code, out, err)
    password = (st.root / 'owner-password').read_text()
    assert password not in out + err and 'dryrun-owner' not in out + err  # credentials are never printed
    assert model_budget(admin) is None
    p = I.parameters(tenant=TENANT, owner_principal='owner-principal-synthetic', value_catalog=str(st.root / 'value-catalog.json'),
                     channel_ref='channel:webchat:synthetic', period_start=st.budget_period[0], period_end=st.budget_period[1],
                     cumulative=None, per_run=None, monthly=None, trial=I.TRIAL)
    request = I.budget_request(p)
    approved_budget(admin, TENANT, 'model', request['unit'], request['limit_amount'], request['period_start'], request['period_end'])
    budget = model_budget(admin)
    assert float(budget['limit']) == float(request['limit_amount']), budget
    return {'executed': True, **request}


def test_budget_cli_refuses_a_non_owner_and_a_password_file_that_is_not_private(assembled, admin, capsys):
    """G-BUDGET-CLI negatives: a workbench Human who is not an owner member (no Budget.set grant) is refused; a password file that is
    not private (mode 0644) is refused before any login. Nothing is written in either case."""
    import os
    st = assembled
    owner_workbench(st, admin)
    create_human(st, application='stage-workbench', username='dryrun-other', label='other')
    code, out, err = budget_cli(st, username='dryrun-other', password_file=st.root / 'other-password')
    assert code == 2 and json.loads(out)['ok'] is False, (code, out, err)
    assert model_budget(admin) is None
    loose = st.root / 'owner-password-loose'
    loose.write_text((st.root / 'owner-password').read_text())
    os.chmod(loose, 0o644)
    code, out, err = budget_cli(st, password_file=loose)
    assert code == 2 and json.loads(out) == {'ok': False, 'detail': 'PermissionError'}, (code, out, err)
    assert model_budget(admin) is None
    # The owner itself succeeds afterwards (the refusals left no state behind).
    assert set_trial_budget(st, admin)['executed'] is True


def workbench_members(st, *labels):
    """A members file with the given workbench Humans as owners (identity-<label>.json from create_human)."""
    members = []
    for label in labels:
        body = json.loads((st.root / f'identity-{label}.json').read_text())
        members.append({'principal_id': body['membership']['principal_id'], 'subject_id': body['subject']['subject_id'], 'role': 'owner'})
    path = st.root / ('workbench-members-' + '-'.join(labels) + '.json')
    if path.exists():
        path.unlink()
    A.private(st.root, path.name, json.dumps({'schema_version': 'nexloop-workbench-members/1', 'tenant_id': TENANT, 'application_id': 'stage-workbench',
                                              'caller_application_id': 'eios:application:stage-workbench', 'members': members}))
    return path


def workbench_roles(st, members, *extra):
    import subprocess
    import sys
    argv = [sys.executable, str(A.ROOT / 'scripts/deploy/workbench_roles.py'), '--roles', str(A.ROOT / 'deploy/authorization/workbench-roles.v1.json'),
            '--members', str(members), '--apply', *extra, '--bootstrap-dsn-file', str(st.bootstrap_dsn), '--signing-key-file',
            str(st.out / 'management' / 'signing.key.hex'), '--signing-key-id', 'stage-1']
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    return done.returncode, done.stdout, done.stderr


def strategy_as(st, label, request_id):
    """context_strategy_setup as the workbench Human <label> (username dryrun-<label>), with its own request id."""
    recipe = st.root / f'context-strategy-{label}.json'
    if not recipe.exists():
        body = A.filled('context-strategy-setup.example.json', TENANT_ID=TENANT, WORKBENCH_APPLICATION_ID='stage-workbench')
        A.private(st.root, recipe.name, json.dumps({**body, 'request_id': request_id}))
    username = st.root / f'username-dryrun-{label}'
    if not username.exists():
        A.private(st.root, username.name, f'dryrun-{label}')
    return A.run_tool('nexloop_eios.context_strategy_setup', [
        '--recipe', str(recipe), '--strategies-file', str(A.ROOT / 'deploy/configuration/context-strategies.v1.json'),
        *st.backend_args()[:6], '--artifact-root', str(st.artifacts), '--identity-database-url-file', str(st.root / 'identity-dsn'),
        '--username-file', str(username), '--password-file', str(st.root / f'{label}-password'),
        '--browser-rate-key-file', str(st.out / 'app' / 'secrets' / 'api' / 'browser-rate.key'), '--browser-origin', 'https://127.0.0.1:8443'])


def test_a_revoked_workbench_owner_is_refused_and_the_remaining_owner_is_not(assembled, admin, capsys):
    """G-WB-REVOKE: two owners applied with workbench_roles --apply; --revoke-member removes one (empty grant sets first, then the
    smaller member list). The revoked owner's Budget.set and nexloop.context.strategy.publish are refused, the remaining owner performs
    both; a rerun gives the same state; a members file that still lists the revoked owner is refused without any write."""
    st = assembled
    for label in ('owner', 'owner2'):
        create_human(st, application='stage-workbench', username=f'dryrun-{label}', label=label)
    both = workbench_members(st, 'owner', 'owner2')
    code, out, err = workbench_roles(st, both)
    assert code == 0, (out, err)
    revoked = json.loads((st.root / 'identity-owner.json').read_text())['membership']['principal_id']
    remaining = workbench_members(st, 'owner2')
    publications = 'select count(*) from control.nexloop_configuration_publications where tenant_id=%s'
    # Negative: the members file still lists the revoked owner -> refused before any write.
    before = admin.execute(publications, (TENANT,)).fetchone()[0]
    code, out, err = workbench_roles(st, both, '--revoke-member', revoked)
    assert code == 2 and 'still lists a revoked member' in err, (code, out, err)
    assert admin.execute(publications, (TENANT,)).fetchone()[0] == before
    for _ in range(2):  # the rerun gives the same state
        code, out, err = workbench_roles(st, remaining, '--revoke-member', revoked)
        assert code == 0, (out, err)
        result = json.loads(out)
        assert result['revoked'] == [revoked] and result['members'] == 1 and result['revoked_grant_sets'] > 0
        assert admin.execute("select rolcanlogin, rolpassword is null from pg_authid where rolname='nexloop_configurator'").fetchone() == (False, True)
    grants = admin.execute("select payload->'grants' from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and entity_key[1]=%s",
                           (TENANT, revoked)).fetchall()
    assert grants and all(g == [] for (g,) in grants)
    # The revoked owner: Budget.set and the strategy publication refused, nothing written.
    code, out, err = budget_cli(st, username='dryrun-owner', password_file=st.root / 'owner-password')
    # Signed in (the password is right), refused by authorization: the grant sets are empty.
    assert code == 2 and json.loads(out) == {'ok': False, 'detail': 'ActionAuthorizationDenied'}, (code, out, err)
    assert model_budget(admin) is None
    code, out, err = strategy_as(st, 'owner', 'context-strategy-revoked-owner')
    assert code == 2 and json.loads(err) == {'ok': False, 'reason': 'ActionAuthorizationDenied'}, (code, out, err)
    assert admin.execute('select count(*) from control.nexloop_context_strategies where tenant_id=%s', (TENANT,)).fetchone() == (0,)
    # The remaining owner performs both (the budget handler is reached: authorized; without an approval policy it fails closed).
    code, out, err = budget_cli(st, username='dryrun-owner2', password_file=st.root / 'owner2-password')
    assert code == 2 and json.loads(out) == {'ok': False, 'detail': 'budget: approval_unavailable'}, (code, out, err)
    code, out, err = strategy_as(st, 'owner2', 'context-strategy-remaining-owner')
    assert code == 0 and json.loads(out)['strategy_ref'] == 'context-strategy:recent_plus_required@1', (code, out, err)
