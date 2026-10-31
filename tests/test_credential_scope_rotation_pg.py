"""S79 (0391) on real PostgreSQL through `stage_secrets.py rotate-credential` / `revoke-credential` on a registered stage tree: a
rotation moves a service credential onto the scopes its current service grants compile to (R65: service-grants v24 gave
catalog_maintainer object_type READ, its stored binding asked for action.execute only, register stopped on
credential_rotation_required and the 0384 rotation copied the old binding). Narrowing works the same way; equal scopes keep the
binding byte for byte; a replay is the recorded result; the previous token still authenticates until revoked (its decisions are stale
once the scopes changed); two active tokens without rotation files are closed by revoke-credential --superseded; the scope entry
refuses any other binding change and a malformed request; only the configurator calls it."""
import json
import shutil
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios import service_grants as G
from nexloop_eios.authorization import authenticate_service
from test_service_credential_rotation_pg import authenticates, database, dsn, expiry, fact, principal, run, token_of, tokens
from test_stage_register_pg import TENANT, allowed, registered  # noqa: F401
from test_stage_secrets import S, tree  # noqa: F401

ROLE = 'catalog_maintainer'
TYPE_READ = 'eios:object_type:IncentiveOffering'


def narrowed(tmp):
    """service-grants as v23 had it for the maintainer: no object_type READ (and no rule needing it)."""
    m = json.loads(S.GRANTS.read_text())
    m['grants'] = [g for g in m['grants'] if not (g['principal'] == ROLE and g['resource_type'] == 'object_type')]
    for key in [k for k, v in m.items() if isinstance(v, list) and k not in ('grants', 'principals', G.RETIRED)]:
        m[key] = [x for x in m[key] if not (isinstance(x, dict) and x.get('principal') == ROLE)]
    path = tmp / 'service-grants.narrowed.json'
    path.write_text(json.dumps(m))
    return path


def scopes(r, status='active'):
    return sorted(json.dumps(sorted(b['requested_scopes'])) for (b,) in r['admin'].execute(
        "select binding from authz.nexloop_service_credentials where credential_id=%s and status=%s", (principal(r, ROLE)['credential_id'], status)))


def reads_type(r, token):
    pool = r['pools'][principal(r, ROLE)['database_role']]
    try:
        s = authenticate_service(pool, token, world='real')
    except Exception:
        return False
    return allowed(pool, s, TYPE_READ, ResourceType.OBJECT_TYPE, Operation.READ)


def executes(r, token):
    pool = r['pools'][principal(r, ROLE)['database_role']]
    try:
        s = authenticate_service(pool, token, world='real')
    except Exception:
        return False
    return allowed(pool, s, 'eios:action:IncentiveOffering.create:1')


def rotate(r, capsys, grants, until):
    """prepare, apply (twice: the replay), switch, revoke --previous, finish; returns (old, new, applied)."""
    old = token_of(r, ROLE)
    run(r, capsys, 'rotate-credential', '--role', ROLE, '--prepare')
    new = json.loads((r['tree']['out'] / 'management' / 'rotation' / ROLE / 'pending.json').read_text())['token']
    applied = S.rotation_apply(r['tree']['out'], ROLE, tenant=TENANT, database_url_file=dsn(r), signing_key_id='stage-1', credential_expires_at=until,
                               grants_path=grants)
    again = S.rotation_apply(r['tree']['out'], ROLE, tenant=TENANT, database_url_file=dsn(r), signing_key_id='stage-1', credential_expires_at=until,
                             grants_path=grants)
    assert again == {**applied, 'replayed': True}
    return old, new, applied


def finish(r, capsys):
    run(r, capsys, 'rotate-credential', '--role', ROLE, '--switch')
    assert run(r, capsys, 'revoke-credential', '--role', ROLE, '--previous', *database(r))['active_tokens'] == 1
    assert run(r, capsys, 'rotate-credential', '--role', ROLE, '--finish')['finished'] is True


def test_rotation_narrows_then_widens_the_scopes_and_register_follows(registered, capsys):  # noqa: F811
    r = registered
    v23 = narrowed(r['tree']['tmp'])
    assert scopes(r) == [json.dumps(['action.execute', 'object_type.read'])] and reads_type(r, token_of(r, ROLE))
    # A register with the narrower grants alone is refused: the stored binding asks for scopes the grants no longer give.
    with pytest.raises(G.ServiceGrantsRejected, match='credential_rotation_required'):
        r['register'](grants_path=v23)
    # Narrowing: the rotation compiles action.execute only.
    old, new, applied = rotate(r, capsys, v23, expiry(40))
    assert applied['scopes_added'] == 0 and applied['scopes_removed'] == 1 and applied['active_tokens'] == 2
    assert scopes(r) == sorted([json.dumps(['action.execute']), json.dumps(['action.execute', 'object_type.read'])])
    assert fact(r, principal(r, ROLE)['credential_id'])['requested_scopes'] == ['action.execute']
    # The previous token still authenticates until revoked, but its decisions are stale (binding != the shared fact).
    assert authenticates(r, old, ROLE) and not executes(r, old)
    finish(r, capsys)
    assert r['register'](grants_path=v23)['changed'] is True  # the two type READ grants are emptied
    assert r['register'](grants_path=v23)['changed'] is False
    assert executes(r, new) and not reads_type(r, new) and not authenticates(r, old, ROLE)

    # R65: the grants give object_type READ again, the stored binding does not ask for it: register stops, nothing written.
    revision = r['admin'].execute('select authority_revision from control.nexloop_tenants where tenant_id=%s', (TENANT,)).fetchone()[0]
    with pytest.raises(G.ServiceGrantsRejected, match='credential_rotation_required'):
        r['register']()
    assert r['admin'].execute('select authority_revision from control.nexloop_tenants where tenant_id=%s', (TENANT,)).fetchone()[0] == revision
    findings = G.plan(S.merged_manifest()[0], TENANT, G.inventory(dsn(r), TENANT))['findings']
    assert {'credential': ROLE, 'state': 'credential_rotation_required'} in findings
    # Widening: the rotation compiles the scopes back from the current grants.
    old, newer, applied = rotate(r, capsys, S.GRANTS, expiry(50))
    assert applied['scopes_added'] == 1 and applied['scopes_removed'] == 0
    assert authenticates(r, newer, ROLE) and authenticates(r, old, ROLE)
    finish(r, capsys)
    assert not any(f['credential'] == ROLE for f in G.plan(S.merged_manifest()[0], TENANT, G.inventory(dsn(r), TENANT))['findings'])
    assert r['register']()['changed'] is True and r['register']()['changed'] is False
    assert executes(r, newer) and reads_type(r, newer) and not authenticates(r, old, ROLE)
    assert scopes(r) == [json.dumps(['action.execute', 'object_type.read'])]


def test_equal_scopes_keep_the_binding_byte_for_byte(registered, capsys):  # noqa: F811
    r = registered
    credential = principal(r, ROLE)['credential_id']
    before = r['admin'].execute("select binding from authz.nexloop_service_credentials where credential_id=%s", (credential,)).fetchone()[0]
    _, new, applied = rotate(r, capsys, S.GRANTS, expiry(40))
    assert applied['scopes_added'] == applied['scopes_removed'] == 0
    rows = r['admin'].execute("select count(*),count(distinct binding) from authz.nexloop_service_credentials where credential_id=%s and status='active'",
                              (credential,)).fetchone()
    assert rows == (2, 1)
    assert r['admin'].execute("select binding from authz.nexloop_service_credentials where token_digest=%s",
                              (G.hashlib.sha256(new.encode()).hexdigest(),)).fetchone()[0] == before
    assert executes(r, token_of(r, ROLE)) and executes(r, new)  # both decide during an equal-scope overlap
    finish(r, capsys)
    assert r['register']()['changed'] is False


def test_two_active_tokens_without_rotation_files_are_closed_by_superseded(registered, capsys):  # noqa: F811
    r = registered  # R65: rotated, switched, finish ran, but --previous was never run (the revoking tree was not switched)
    credential = principal(r, ROLE)['credential_id']
    old, new, _ = rotate(r, capsys, S.GRANTS, expiry(60))
    run(r, capsys, 'rotate-credential', '--role', ROLE, '--switch')
    shutil.rmtree(r['tree']['out'] / 'management' / 'rotation' / ROLE)  # what finish leaves
    early = r['admin'].execute("select expires_at from authz.nexloop_service_credentials where token_digest=%s",
                               (G.hashlib.sha256(old.encode()).hexdigest(),)).fetchone()[0]
    late = r['admin'].execute("select expires_at from authz.nexloop_service_credentials where token_digest=%s",
                              (G.hashlib.sha256(new.encode()).hexdigest(),)).fetchone()[0]
    request = str(uuid.uuid4())
    sup = ('revoke-credential', '--role', ROLE, '--superseded', *database(r), '--request-id', request)
    # The later token is never the one revoked; a time naming no token is refused.
    for when in (late, early + timedelta(seconds=1)):
        assert S.main([sup[0], '--out', str(r['tree']['out']), *sup[1:], '--expires-at', when.isoformat()]) == 2
        capsys.readouterr()
    # A tree that still holds the earlier token cannot revoke it (the kept token must expire later).
    keys = [k for k in tokens(r) if r['stage']['credential_files'].get(k.split(':', 1)[1]) == ROLE]
    S._set_token(r['tree']['out'], keys, old)
    assert S.main([sup[0], '--out', str(r['tree']['out']), *sup[1:], '--expires-at', early.isoformat()]) == 2
    capsys.readouterr()
    S._set_token(r['tree']['out'], keys, new)
    done = run(r, capsys, *sup, '--expires-at', early.isoformat())
    assert done['revoked'] == 1 and done['active_tokens'] == 1 and done['superseded'] is True
    assert run(r, capsys, *sup, '--expires-at', early.isoformat()) == {**done, 'replayed': True}
    assert not authenticates(r, old, ROLE) and executes(r, new)
    entry = r['admin'].execute("select entry from control.nexloop_restore_journal where kind='revocation' order by seq desc limit 1").fetchone()[0]
    assert entry['credential_id'] == credential and entry['token_digest'] == G.hashlib.sha256(old.encode()).hexdigest()
    # One token left: nothing is superseded any more.
    assert S.main([sup[0], '--out', str(r['tree']['out']), *sup[1:-1], str(uuid.uuid4()), '--expires-at', late.isoformat()]) == 2
    capsys.readouterr()
    assert r['register']()['changed'] is False


def test_superseded_is_refused_while_a_rotation_is_prepared(registered, capsys):  # noqa: F811
    r = registered
    run(r, capsys, 'rotate-credential', '--role', ROLE, '--prepare')
    with pytest.raises(S.Refused, match='--previous'):
        S.revoke_superseded(r['tree']['out'], ROLE, expires_at=expiry(1), tenant=TENANT, database_url_file=dsn(r), signing_key_id='stage-1',
                            request_id=str(uuid.uuid4()))


def call(r, request, new_digest, fact_payload):
    key = bytes.fromhex((r['tree']['out'] / 'management' / 'signing.key.hex').read_text().strip())
    with psycopg.connect((dsn(r)).read_text()) as db:
        return db.execute('select control.nexloop_rotate_service_credential_scopes(%s,%s::jsonb,%s,%s::jsonb,%s,%s)',
                          (TENANT, json.dumps(request), new_digest, json.dumps(fact_payload), 'stage-1', key)).fetchone()[0]


@pytest.mark.parametrize('change', ['other_subject', 'other_application', 'fact_scopes_differ', 'bad_scope', 'duplicate_scope', 'no_scopes',
                                    'extra_key', 'scopes_not_array'])
def test_the_scope_entry_changes_nothing_but_the_scopes(registered, change):  # noqa: F811
    r = registered
    credential = principal(r, ROLE)['credential_id']
    until = (datetime.now(UTC) + timedelta(days=40)).replace(microsecond=0)
    stored = fact(r, credential)
    new_scopes = ['action.execute']
    request = {'request_id': str(uuid.uuid4()), 'credential_id': credential, 'current_digest': G.hashlib.sha256(token_of(r, ROLE).encode()).hexdigest(),
               'expires_at': until.isoformat(), 'requested_scopes': new_scopes}
    changes = {'expires_at': until.isoformat(), 'snapshot_digest': None, 'requested_scopes': new_scopes}
    if change == 'other_subject':
        changes['subject_revision'] = stored['subject_revision'] + 1
    elif change == 'other_application':
        changes['caller_application_version'] = '2'
    elif change == 'fact_scopes_differ':
        changes['requested_scopes'] = ['action.execute', 'object_type.read']
    payload = G.F.CredentialAuthenticationFacts.model_validate_json(json.dumps({**stored, **changes})).model_dump(mode='json')
    if change == 'bad_scope':
        request['requested_scopes'] = ['Action.Execute']
    elif change == 'duplicate_scope':
        request['requested_scopes'] = ['action.execute', 'action.execute']
    elif change == 'no_scopes':
        request['requested_scopes'] = []
    elif change == 'extra_key':
        request['binding'] = {}
    elif change == 'scopes_not_array':
        request['requested_scopes'] = 'action.execute'
    before = r['admin'].execute("select count(*) from authz.nexloop_service_credentials where credential_id=%s", (credential,)).fetchone()[0]
    with pytest.raises(psycopg.Error) as e:
        call(r, request, uuid.uuid4().hex * 2, payload)
    assert e.value.sqlstate in ('42501', '22023')
    assert r['admin'].execute("select count(*) from authz.nexloop_service_credentials where credential_id=%s", (credential,)).fetchone()[0] == before
    assert fact(r, credential) == stored


def test_only_the_configurator_calls_the_new_entries(registered):  # noqa: F811
    r = registered
    for role in ('nexloop_api', 'nexloop_domain_worker'):
        with r['pools'][role].connection() as c:
            for sql in ("select control.nexloop_rotate_service_credential_scopes(%s,'{}'::jsonb,'x','{}'::jsonb,'stage-1',%s)",
                        "select control.nexloop_revoke_superseded_service_credential(%s,'{}'::jsonb,'stage-1',%s)"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
                    c.execute(sql, (TENANT, b'\0' * 32))
