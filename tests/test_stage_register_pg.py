"""NX-032 phase B: `stage_secrets.py register` gives every credential file of the stage Compose file a registered service
principal with exactly its declared grants, through the trusted configuration (configurator role only), idempotently.

Clean catalog PostgreSQL: bootstrap, the phase A tree of stage_secrets (temporary CA), register; then each credential file's token
authenticates as its principal, holds every grant of service-grants.v1.json plus stage-principals.v1.json for that principal and
nothing outside it (negative cases), the relay Source has its message_read_rule, and a second register changes nothing."""
import json
import secrets as random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from psycopg.conninfo import make_conninfo

from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service, authorization_service
from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A
from test_stage_secrets import S, tree  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
TENANT = TENANT_A
KINDS = {'action': (ResourceType.ACTION, Operation.EXECUTE), 'object_type': (ResourceType.OBJECT_TYPE, Operation.READ)}


def credential_files_of_compose():
    app = yaml.safe_load((ROOT / 'deploy/stage/compose.app.example.yaml').read_text())
    paths = [s['file'].split('/secrets/', 1)[1] for s in app['secrets'].values()]
    return {p for p in paths if p.endswith('service-credential') or (p.endswith('-credential') and 'provider' not in p)}


def test_every_compose_credential_file_has_exactly_one_principal_and_the_merge_has_no_duplicate():
    manifest, stage = S.merged_manifest()
    assert set(stage['credential_files']) == credential_files_of_compose()
    roles = [p['role'] for p in manifest['principals']]
    assert len(roles) == len(set(roles)) and set(stage['credential_files'].values()) == set(roles)
    keys = [(g['principal'], g['resource_id']) for g in manifest['grants']]
    assert len(keys) == len(set(keys))
    # Effect Actions only where declared (ADR-020 §3 keeps them out of the service grants manifest).
    from nexloop_eios.service_grants import FORBIDDEN_MARKERS
    assert {g['principal'] for g in manifest['grants'] if any(m in g['resource_id'] for m in FORBIDDEN_MARKERS)} <= set(stage['effect_roles'])
    # The relay Source is the Source of the relay_source_read_rule.
    source = next(p for p in manifest['principals'] if p['role'] == 'relay_source')
    assert manifest['relay_source_read_rules'][0]['source_principal_id'] == source['principal_id']


@pytest.fixture
def registered(pg, admin, tree):  # noqa: F811
    bootstrap(admin)
    admin.execute('alter role nexloop_configurator login')
    dsn = tree['tmp'] / 'configurator-dsn'
    dsn.write_text(make_conninfo(pg, user='nexloop_configurator'))
    dsn.chmod(0o600)
    expires = (datetime.now(UTC) + timedelta(days=30)).isoformat()

    def register(**kw):
        return S.register(tree['out'], tenant=TENANT, database_url_file=dsn, signing_key_id='stage-1', credential_expires_at=expires, **kw)
    first = register()
    roles = ('nexloop_api', 'nexloop_domain_worker', 'nexloop_scheduler', 'nexloop_action_worker')
    pools = {}
    with open_core(make_conninfo(pg, user=roles[0])) as a, open_core(make_conninfo(pg, user=roles[1])) as b, \
         open_core(make_conninfo(pg, user=roles[2])) as c, open_core(make_conninfo(pg, user=roles[3])) as d:
        pools.update(zip(roles, (a, b, c, d)))
        yield dict(tree=tree, first=first, register=register, pools=pools, manifest=S.merged_manifest()[0], stage=S.merged_manifest()[1], admin=admin)


def session(r, role):
    principal = next(p for p in r['manifest']['principals'] if p['role'] == role)
    tokens = json.loads((r['tree']['out'] / 'management' / 'service-secrets.json').read_text())
    path = next(p for p, owner in r['stage']['credential_files'].items() if owner == role)
    pool = r['pools'][principal['database_role']]
    return pool, authenticate_service(pool, tokens['app:' + path], world='real')


def allowed(pool, s, resource, kind=ResourceType.ACTION, op=Operation.EXECUTE):
    try:
        return authorization_service(pool, s).decide(s.query(resource_id=resource, resource_type=kind, operation=op)).allowed
    except (AuthorizationUnavailable, F.AuthorizationFactDenied):
        return False


def test_register_gives_every_credential_file_its_principal_and_exactly_the_declared_grants(registered):
    r = registered
    assert r['first']['changed'] is True and len(r['first']['credentials_created']) == len(r['manifest']['principals'])
    assert r['first']['message_read_rules_written'] == 1 and r['first']['extra_grants_outside_manifest'] == 0
    tokens = json.loads((r['tree']['out'] / 'management' / 'service-secrets.json').read_text())
    for path, role in r['stage']['credential_files'].items():
        principal = next(p for p in r['manifest']['principals'] if p['role'] == role)
        s = authenticate_service(r['pools'][principal['database_role']], tokens['app:' + path], world='real')
        assert s.authentication.subject_principal_id == principal['principal_id'], path
    for role in sorted({g['principal'] for g in r['manifest']['grants']}):
        pool, s = session(r, role)
        for g in (g for g in r['manifest']['grants'] if g['principal'] == role):
            kind = ResourceType(g['resource_type'])
            for op in g['operations']:
                assert allowed(pool, s, g['resource_id'], kind, Operation(op)), (role, g['resource_id'], op)
    # Nothing outside the declaration (least authority).
    for role, resource, kind, op in (('effect_executor', 'eios:action:nexloop.conversation.route:1', ResourceType.ACTION, Operation.EXECUTE),
                                     ('relay_planner', 'eios:action:nexloop.service.request:1', ResourceType.ACTION, Operation.EXECUTE),
                                     ('role_queue', 'eios:action:Goal.create:1', ResourceType.ACTION, Operation.EXECUTE),
                                     ('artifact_maintenance', 'eios:artifact:local_real', ResourceType.ARTIFACT, Operation.CREATE),
                                     ('role_planner', 'eios:action:PlanStep.create:1', ResourceType.ACTION, Operation.EXECUTE)):
        pool, s = session(r, role)
        assert not allowed(pool, s, resource, kind, op), (role, resource)
    source = next(p for p in r['manifest']['principals'] if p['role'] == 'relay_source')['principal_id']
    rule = r['admin'].execute("select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='message_read_rule' and entity_key=%s",
                              (TENANT, [source])).fetchone()
    assert rule and rule[0]['fields'] == ['actor', 'body'] and rule[0]['active'] is True
    # Only digests are stored.
    stored = json.dumps(r['admin'].execute('select array_agg(token_digest) from authz.nexloop_service_credentials').fetchone()[0])
    assert not any(t in stored for t in tokens.values())


def test_register_is_idempotent_and_dry_run_writes_nothing(registered):
    r = registered
    again = r['register']()
    assert again == {**again, 'changed': False, 'message_read_rules_written': 0, 'credentials_created': [], 'grants_added': 0, 'grants_revoked': 0}
    count = r['admin'].execute('select count(*) from control.nexloop_configuration_publications').fetchone()[0]
    dry = r['register'](dry_run=True)
    assert dry['dry_run'] is True and len(dry['principals']) == 21 and dry['credential_files'] == 28
    assert r['admin'].execute('select count(*) from control.nexloop_configuration_publications').fetchone()[0] == count


def test_the_credential_files_of_one_principal_must_hold_one_token(tree, tmp_path):  # noqa: F811
    import shutil
    out = tmp_path / 'copy'
    shutil.copytree(tree['out'], out)
    path = out / 'management' / 'service-secrets.json'
    tokens = json.loads(path.read_text())
    tokens['app:webchat-delivery/service-credential'] = random.token_urlsafe(48)
    path.write_text(json.dumps(tokens))
    with pytest.raises(S.Refused, match='same token'):
        S.register(out, tenant=TENANT, database_url_file=tmp_path / 'unused', signing_key_id='stage-1', credential_expires_at='2030-01-01T00:00:00+00:00')


def test_register_through_a_configurator_login_window_on_the_bootstrap_socket(pg, admin, tree, capsys):  # noqa: F811
    """Document §3b: the stage has no configurator login; register opens a socket-only window from the bootstrap DSN, writes as
    the configurator itself (operator_role, session_user), and leaves the role NOLOGIN without a password; SET ROLE is refused."""
    from psycopg.conninfo import conninfo_to_dict

    from nexloop_eios.trusted_configuration import configurator_connection
    bootstrap(admin)
    assert admin.execute("select rolcanlogin from pg_roles where rolname='nexloop_configurator'").fetchone() == (False,)
    assert str(conninfo_to_dict(pg)['host']).startswith('/')
    socket_dsn = tree['tmp'] / 'bootstrap-socket-dsn'
    socket_dsn.write_text(make_conninfo(pg, user='nexloop_bootstrap'))
    socket_dsn.chmod(0o600)
    set_role = tree['tmp'] / 'set-role-dsn'
    set_role.write_text(make_conninfo(pg, user='nexloop_bootstrap', options='-c role=nexloop_configurator'))
    set_role.chmod(0o600)
    with pytest.raises(Exception):
        configurator_connection(set_role)  # bootstrap with SET ROLE is not the configurator (session_user, elevated)
    expires = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    args = ['register', '--out', str(tree['out']), '--tenant', TENANT, '--bootstrap-dsn-file', str(socket_dsn), '--signing-key-id', 'stage-1',
            '--credential-expires-at', expires]
    tokens = json.loads((tree['out'] / 'management' / 'service-secrets.json').read_text())
    for expected_change in (True, False):
        assert S.main(args) == 0
        printed = capsys.readouterr().out
        assert json.loads(printed)['changed'] is expected_change and not any(t in printed for t in tokens.values())
        assert admin.execute("select rolcanlogin, rolpassword is null from pg_authid where rolname='nexloop_configurator'").fetchone() == (False, True)
    rows = admin.execute('select operator_role, count(*) from control.nexloop_configuration_publications group by 1').fetchall()
    assert [r[0] for r in rows] == ['nexloop_configurator'] and rows[0][1] >= 1
    assert not list((tree['out'] / 'management').glob('.*'))


def test_browser_registration_lets_webchat_and_workbench_log_in_through_the_api_and_is_idempotent(pg, admin, tree, tmp_path, capsys):  # noqa: F811
    """G1 (run-34 §2): after bootstrap and register, `stage_secrets.py browser` (configurator login window) registers both browser
    applications, the local_login rate policy and the initial identity allowances; an initial identity created per application
    logs in through http_api's real login routes (WebChat and workbench); a second run publishes nothing."""
    import hashlib

    from fastapi.testclient import TestClient

    from nexloop_eios.browser_http import BrowserConfiguration
    from nexloop_eios.http_api import ApiConfiguration, create_app
    from nexloop_eios.initial_identity import create_initial_identity

    def private(name, text):
        path = tmp_path / name
        path.write_text(text)
        path.chmod(0o600)
        return path
    bootstrap(admin)
    socket_dsn = private('bootstrap-dsn', make_conninfo(pg, user='nexloop_bootstrap'))
    expires = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    assert S.main(['register', '--out', str(tree['out']), '--tenant', TENANT, '--bootstrap-dsn-file', str(socket_dsn), '--signing-key-id', 'stage-1',
                   '--credential-expires-at', expires]) == 0
    seal = random.token_hex(32)
    seal_file = private('seal', seal)
    args = ['browser', '--out', str(tree['out']), '--tenant', TENANT, '--browser-application-id', 'stage-webchat', '--workbench-application-id', 'stage-workbench',
            '--identity-seal-file', str(seal_file), '--bootstrap-dsn-file', str(socket_dsn), '--signing-key-id', 'stage-1']
    capsys.readouterr()
    publications = 'select count(*) from control.nexloop_configuration_publications'
    before = admin.execute(publications).fetchone()[0]
    assert S.main(args) == 0 and json.loads(capsys.readouterr().out)['changed'] is True
    assert admin.execute(publications).fetchone()[0] == before + 1
    assert admin.execute("select rolcanlogin from pg_roles where rolname='nexloop_configurator'").fetchone() == (False,)
    assert admin.execute('select application_id, revision, active from control.nexloop_browser_applications where tenant_id=%s order by 1', (TENANT,)).fetchall() == \
        [('stage-webchat', 1, True), ('stage-workbench', 1, True)]
    assert admin.execute('select idempotency_key_digest from control.nexloop_initial_identity_allowances where tenant_id=%s', (TENANT,)).fetchall() == \
        [(hashlib.sha256(bytes.fromhex(seal)).hexdigest(),)] * 2
    # Idempotent: nothing is published again; dry-run prints the plan and no seal.
    assert S.main(args) == 0 and json.loads(capsys.readouterr().out)['changed'] is False
    assert admin.execute(publications).fetchone()[0] == before + 1
    assert S.main(args[:-4] + ['--dry-run']) == 0 and seal not in capsys.readouterr().out
    # A changed allowance is refused before anything is written (0050 never resets an allowance).
    assert S.main(args + ['--webchat-accounts', '11']) == 2 and 'never reset' in capsys.readouterr().out
    # One human per application through initial_identity (nexloop_identity), then the api's own login routes.
    identity_dsn = private('identity-dsn', make_conninfo(pg, user='nexloop_identity'))
    password = private('password', random.token_urlsafe(24))
    now = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    for app, user in (('stage-webchat', 'synthetic-consumer'), ('stage-workbench', 'synthetic-staff')):
        subject = 'synthetic-human-' + user
        create_initial_identity({'schema_version': '1.0', 'tenant_id': TENANT, 'application_id': app, 'request_id': str(__import__('uuid').uuid4()),
            'operator_label': 'stage-owner',
            'subject': {'subject_id': subject, 'kind': 'human', 'status': 'active', 'created_at': now, 'updated_at': now, 'revision': 1},
            'membership': {'tenant_id': TENANT, 'subject_id': subject, 'principal_id': 'synthetic-principal-' + user, 'kind': 'home', 'status': 'active',
                           'valid_from': now, 'valid_until': None, 'trusted_attributes': {}, 'revision': 1},
            'account': {'tenant_id': TENANT, 'local_account_id': 'synthetic-account-' + user, 'subject_id': subject, 'username': user,
                        'verified_email': user + '@example.invalid', 'status': 'active', 'failed_attempts': 0, 'lockout_level': 0, 'locked_until': None,
                        'must_change_password': False, 'session_epoch': 1, 'created_at': now, 'updated_at': now, 'revision': 1}},
            database_url_file=identity_dsn, password_file=password, idempotency_key_file=seal_file)
    origin = 'https://app.stage.invalid'
    rate = private('rate-key', random.token_hex(32))
    config = ApiConfiguration(tmp_path / 'missing', tmp_path / 'missing-key', tmp_path / 'unused', 'missing',
                              BrowserConfiguration(identity_dsn, rate, TENANT, 'stage-webchat', origin),
                              workbench=BrowserConfiguration(identity_dsn, rate, TENANT, 'stage-workbench', origin))
    with TestClient(create_app(config), base_url=origin) as client:
        for path, user in (('/api/v1/auth', 'synthetic-consumer'), ('/api/v1/workbench/auth', 'synthetic-staff')):
            ok = client.post(path + '/login', headers={'Origin': origin}, json={'username': user, 'password': password.read_text()})
            assert ok.status_code == 200 and ok.json()['principal_id'] == 'synthetic-principal-' + user, (path, ok.text)
