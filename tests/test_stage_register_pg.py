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
