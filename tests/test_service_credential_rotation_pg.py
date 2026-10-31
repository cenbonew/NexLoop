"""I31 service credential rotation (migration service_credential_rotation, alert rules v25) on real PostgreSQL, through
`stage_secrets.py rotate-credential` / `revoke-credential` on a registered stage tree: one credential_id holds two active tokens for
the overlap and both authenticate as the same principal; a third active token is refused; revoking the previous token refuses it
(42501) while the new one keeps working; a previous token past its own row's expiry is refused although the fact was
extended (S65); a second token only from inside the rotation entry on the call stack, never a GUC (S68); the revocation rules (a later-expiring token must remain; --retired only without grants or rule facts, S66);
the authentication fact follows the active tokens; register stays in sync; the restore journal names the revoked token; the metrics
snapshot and the two v25 rules; no token in any output, file modes 0700/0600."""
import json
import stat
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios import service_grants as G
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.observability import load_rules
from test_stage_register_pg import ROOT, TENANT, allowed, registered  # noqa: F401
from test_stage_secrets import S, tree  # noqa: F401

ROLE = 'outreach_sender'
RULES = ROOT / 'deploy/configuration'


def tokens(r):
    return json.loads((r['tree']['out'] / 'management' / 'service-secrets.json').read_text())


def principal(r, role=ROLE):
    return next(p for p in r['manifest']['principals'] if p['role'] == role)


def token_of(r, role=ROLE):
    stage = r['stage']
    return next(t for k, t in tokens(r).items() if stage['credential_files'].get(k.split(':', 1)[1]) == role)


def authenticates(r, token, role=ROLE):
    try:
        s = authenticate_service(r['pools'][principal(r, role)['database_role']], token, world='real')
    except AuthorizationUnavailable:
        return False
    return s.authentication.subject_principal_id == principal(r, role)['principal_id']


def decides(r, token, role=ROLE):
    """The principal's own grant is allowed through the full decision (binding and authentication fact read)."""
    from eios.authz.operations import Operation
    from eios.authz.resources import ResourceType
    grant = next(g for g in r['manifest']['grants'] if g['principal'] == role)
    pool = r['pools'][principal(r, role)['database_role']]
    s = authenticate_service(pool, token, world='real')
    return allowed(pool, s, grant['resource_id'], ResourceType(grant['resource_type']), Operation(grant['operations'][0]))


def dsn(r):
    return r['tree']['tmp'] / 'configurator-dsn'


def run(r, capsys, *argv):
    """stage_secrets.main with the tree, as an operator types it; returns the printed JSON."""
    code = S.main([argv[0], '--out', str(r['tree']['out']), *argv[1:]])
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert not any(t in printed for t in tokens(r).values())
    return json.loads(printed)


def database(r):
    return ('--tenant', TENANT, '--configurator-dsn-file', str(dsn(r)), '--signing-key-id', 'stage-1')


def expiry(days):
    return (datetime.now(UTC) + timedelta(days=days)).replace(microsecond=0).isoformat()


def health(r):
    return r['admin'].execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'service_credentials'", (TENANT,)).fetchone()[0]


def fired(snapshot):
    return {x['rule_id'] for x in load_rules(RULES / 'alert-rules.v25.json')['rules']
            if x['metric'].partition('.')[0] == 'service_credentials' and snapshot[x['metric'].partition('.')[2]] > x['threshold']}


def fact(r, credential_id):
    return r['admin'].execute("select payload from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='authentication' and entity_key=%s",
                              (TENANT, [credential_id])).fetchone()[0]


def test_v25_adds_exactly_the_two_credential_rules():
    v24, v25 = json.loads((RULES / 'alert-rules.v24.json').read_text()), load_rules(RULES / 'alert-rules.v25.json')
    assert v25['version'] == 25 and len(v25['rules']) == 89 and v25['rules'][:len(v24['rules'])] == v24['rules']
    added = {x['rule_id']: x for x in v25['rules'][len(v24['rules']):]}
    assert added['service_credential_expiring']['metric'] == 'service_credentials.expiring_7d' and added['service_credential_expiring']['severity'] == 'warning'
    assert added['service_credential_expired']['metric'] == 'service_credentials.expired_active' and added['service_credential_expired']['severity'] == 'critical'
    assert all(x['threshold'] == 0 and x['condition'] == 'gt' for x in added.values())


def test_rotate_overlap_revoke_previous_and_register_in_sync(registered, capsys):  # noqa: F811
    r = registered
    credential = principal(r)['credential_id']
    old = token_of(r)
    before = health(r)
    assert before['active'] >= 26 and before['expired_active'] == 0 and before['expiring_7d'] == 0 and fired(before) == set()

    prepared = run(r, capsys, 'rotate-credential', '--role', ROLE, '--prepare')
    assert prepared == {'role': ROLE, 'credential_id': credential, 'prepared': True, 'files': ['app:outreach-sender/service-credential']}
    d = r['tree']['out'] / 'management' / 'rotation' / ROLE
    assert stat.S_IMODE(d.stat().st_mode) == 0o700 and stat.S_IMODE(d.parent.stat().st_mode) == 0o700
    assert {f.name: stat.S_IMODE(f.stat().st_mode) for f in d.iterdir()} == {'current.json': 0o600, 'pending.json': 0o600}
    new = json.loads((d / 'pending.json').read_text())['token']
    assert token_of(r) == old  # prepare changes no credential file

    until = expiry(60)
    applied = run(r, capsys, 'rotate-credential', '--role', ROLE, '--apply', *database(r), '--credential-expires-at', until)
    assert applied['rotated'] is True and applied['replayed'] is False and applied['active_tokens'] == 2 and applied['credential_id'] == credential
    again = run(r, capsys, 'rotate-credential', '--role', ROLE, '--apply', *database(r), '--credential-expires-at', until)
    assert again['replayed'] is True and again['authority_revision'] == applied['authority_revision']
    # The overlap: both tokens authenticate as the same principal, one binding, the fact follows the later expiry.
    assert authenticates(r, old) and authenticates(r, new) and decides(r, old) and decides(r, new)
    assert datetime.fromisoformat(fact(r, credential)['expires_at']) == datetime.fromisoformat(until)
    rows = r['admin'].execute("select count(*),count(distinct binding) from authz.nexloop_service_credentials where credential_id=%s and status='active'",
                              (credential,)).fetchone()
    assert rows == (2, 1)

    assert S.main(['rotate-credential', '--out', str(r['tree']['out']), '--role', ROLE, '--finish']) == 2  # not switched yet
    capsys.readouterr()
    switched = run(r, capsys, 'rotate-credential', '--role', ROLE, '--switch')
    assert run(r, capsys, 'rotate-credential', '--role', ROLE, '--switch-back')['switched_to'] == 'current' and token_of(r) == old
    switched = run(r, capsys, 'rotate-credential', '--role', ROLE, '--switch')
    assert switched == {'role': ROLE, 'switched_to': 'new', 'files_rewritten': ['app:outreach-sender/service-credential']}
    assert token_of(r) == new and (r['tree']['out'] / 'app' / 'secrets' / 'outreach-sender' / 'service-credential').read_text() == new
    assert S.check(r['tree']['out'])['ok'] is True  # the manifest's digests follow the rewritten file
    # register reads the active token that expires last: nothing to write.
    assert r['register']()['changed'] is False

    revoked = run(r, capsys, 'revoke-credential', '--role', ROLE, '--previous', *database(r))
    assert revoked['revoked'] == 1 and revoked['active_tokens'] == 1 and revoked['retired'] is False
    assert run(r, capsys, 'rotate-credential', '--role', ROLE, '--finish') == {'role': ROLE, 'finished': True} and not d.exists()
    assert not authenticates(r, old) and authenticates(r, new) and decides(r, new)
    with r['pools']['nexloop_domain_worker'].connection() as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("select authz.nexloop_service_identity(encode(sha256(convert_to(%s,'UTF8')),'hex'),'real')", (old,))
    entry = r['admin'].execute("select entry from control.nexloop_restore_journal where kind='revocation' order by seq desc limit 1").fetchone()[0]
    assert entry['credential_id'] == credential and entry['token_digest'] == G.hashlib.sha256(old.encode()).hexdigest()
    assert r['register']()['changed'] is False


def test_a_third_token_and_the_revocation_rules_are_refused(registered, capsys):  # noqa: F811
    r = registered
    credential = principal(r)['credential_id']
    signing = r['tree']['out'] / 'management' / 'signing.key.hex'
    common = dict(database_url_file=dsn(r), signing_key_file=signing, signing_key_id='stage-1')
    old, second, third = token_of(r), 'second-' + uuid.uuid4().hex * 2, 'third-' + uuid.uuid4().hex * 2
    until = datetime.now(UTC) + timedelta(days=60)
    row = r['admin'].execute('select tenant_id,binding,worlds,audience from authz.nexloop_service_credentials where token_digest=%s',
                             (G.hashlib.sha256(old.encode()).hexdigest(),)).fetchone()

    def insert(secret):
        r['admin'].execute("insert into authz.nexloop_service_credentials(token_digest,tenant_id,credential_id,binding,worlds,audience,status,expires_at)"
                           " values(%s,%s,%s,%s,%s,%s,'active',%s)",
                           (G.hashlib.sha256(secret.encode()).hexdigest(), row[0], credential, psycopg.types.json.Jsonb(row[1]), row[2], row[3], until))
    # S68: the second-token permission is the call stack, not a GUC - setting the old rotation GUC and inserting as 0050 would is refused.
    with pytest.raises(psycopg.errors.UniqueViolation), r['admin'].transaction():
        r['admin'].execute("select set_config('nexloop.service_credential_rotation',%s,true)",
                           (credential + ':' + G.hashlib.sha256(second.encode()).hexdigest(),))
        insert(second)
    G.rotate_credential(TENANT, credential_id=credential, current_secret=old, new_secret=second, expires_at=until, request_id=uuid.uuid4(), **common)
    # A third active token: refused by the function (the current token is not the only active one) and by the table guard.
    with pytest.raises(G.ServiceGrantsRejected, match='credential_rotate_refused:42501'):
        G.rotate_credential(TENANT, credential_id=credential, current_secret=second, new_secret=third, expires_at=until + timedelta(days=1),
                            request_id=uuid.uuid4(), **common)
    with pytest.raises(psycopg.errors.UniqueViolation), r['admin'].transaction():  # an insert outside the rotation, as 0050 would do it
        insert(third)
    # Revoking the token that expires last is refused (the remaining one would expire earlier); --retired with grants is refused.
    with pytest.raises(G.ServiceGrantsRejected, match='credential_revoke_refused:42501'):
        G.revoke_credential(TENANT, credential_id=credential, secret=second, request_id=uuid.uuid4(), **common)
    with pytest.raises(G.ServiceGrantsRejected, match='credential_revoke_refused:42501'):
        G.revoke_credential(TENANT, credential_id=credential, retired=True, request_id=uuid.uuid4(), **common)
    with pytest.raises(S.Refused, match='not a retired principal'):
        S.revoke_retired(r['tree']['out'], ROLE, credential_id=credential, tenant=TENANT, database_url_file=dsn(r), signing_key_id='stage-1',
                         request_id=str(uuid.uuid4()))
    # A rotation needs the active signing key and the configurator; a wrong key is refused before anything is read.
    bad = r['tree']['tmp'] / 'bad.key'
    bad.write_text('00' * 32)
    bad.chmod(0o600)
    with pytest.raises(G.ServiceGrantsRejected, match='42501'):
        G.revoke_credential(TENANT, credential_id=credential, secret=old, request_id=uuid.uuid4(), database_url_file=dsn(r), signing_key_file=bad,
                            signing_key_id='stage-1')
    with r['pools']['nexloop_domain_worker'].connection() as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("select control.nexloop_revoke_service_credential(%s,'{}'::jsonb,'stage-1',%s)", (TENANT, b'\0' * 32))
    # The previous token is revocable: the second expires later.
    G.revoke_credential(TENANT, credential_id=credential, secret=old, request_id=uuid.uuid4(), **common)
    assert not authenticates(r, old) and authenticates(r, second)


def test_the_previous_token_expires_on_its_own_row_although_the_fact_is_extended(registered):  # noqa: F811
    r = registered  # S65
    credential = principal(r)['credential_id']
    common = dict(database_url_file=dsn(r), signing_key_file=r['tree']['out'] / 'management' / 'signing.key.hex', signing_key_id='stage-1')
    old, new = token_of(r), 'renewed-' + uuid.uuid4().hex * 2
    until = datetime.now(UTC) + timedelta(days=90)
    G.rotate_credential(TENANT, credential_id=credential, current_secret=old, new_secret=new, expires_at=until, request_id=uuid.uuid4(), **common)
    fact = r['admin'].execute("select payload->>'expires_at' from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='authentication'"
                              " and entity_key=array[%s]", (TENANT, credential)).fetchone()[0]
    assert abs(datetime.fromisoformat(fact) - until) < timedelta(seconds=1)
    with r['admin'].transaction():  # the old row passes its own expires_at; nothing revoked it
        r['admin'].execute("update authz.nexloop_service_credentials set expires_at=clock_timestamp()-interval '1 second' where token_digest=%s",
                           (G.hashlib.sha256(old.encode()).hexdigest(),))
    assert not authenticates(r, old) and authenticates(r, new) and decides(r, new)


def test_retired_principal_without_grants_loses_its_last_token(registered):  # noqa: F811
    r = registered
    role = 'relation_backfiller'
    assert role in {x['role'] for x in json.loads(S.GRANTS.read_text())[G.RETIRED]}
    # The stage registered relation_backfiller before v21 retired it: model its credential as a copy of a principal emptied of grants.
    victim = principal(r, ROLE)
    r['admin'].execute("update authz.nexloop_authority_facts set payload=payload||'{\"grants\":[]}'::jsonb where tenant_id=%s and fact_kind='grants' "
                       "and entity_key[1]=%s", (TENANT, victim['principal_id']))
    signing = r['tree']['out'] / 'management' / 'signing.key.hex'
    rules = r['admin'].execute("select fact_kind from authz.nexloop_authority_facts where tenant_id=%s and entity_key[1]=%s and fact_kind in"
                               " ('message_read_rule','message_purpose_rule','property_access_rule','relay_source_read_rule','role_source_read_rule',"
                               "'definition_read_rule','property_group_restriction')", (TENANT, victim['principal_id'])).fetchall()
    if not rules:  # S66: give it one rule fact so the refusal is exercised
        r['admin'].execute("insert into authz.nexloop_authority_facts(tenant_id,fact_kind,entity_key,payload) select tenant_id,'definition_read_rule',"
                           "array[%s],'{}'::jsonb from authz.nexloop_authority_facts where tenant_id=%s and fact_kind='grants' and entity_key[1]=%s",
                           (victim['principal_id'], TENANT, victim['principal_id']))
    # S66: a principal that still holds a rule fact is not retired.
    with pytest.raises(G.ServiceGrantsRejected, match='credential_revoke_refused:42501'):
        G.revoke_credential(TENANT, credential_id=victim['credential_id'], retired=True, request_id=uuid.uuid4(), database_url_file=dsn(r),
                            signing_key_file=signing, signing_key_id='stage-1')
    r['admin'].execute("delete from authz.nexloop_authority_facts where tenant_id=%s and entity_key[1]=%s and fact_kind in ('message_read_rule',"
                       "'message_purpose_rule','property_access_rule','relay_source_read_rule','role_source_read_rule','definition_read_rule',"
                       "'property_group_restriction')", (TENANT, victim['principal_id']))
    request = str(uuid.uuid4())
    result = S.revoke_retired(r['tree']['out'], role, credential_id=victim['credential_id'], tenant=TENANT, database_url_file=dsn(r),
                              signing_key_id='stage-1', request_id=request)
    assert result['revoked'] == 1 and result['active_tokens'] == 0 and result['retired'] is True
    assert not authenticates(r, token_of(r))
    replay = G.revoke_credential(TENANT, credential_id=victim['credential_id'], retired=True, request_id=request, database_url_file=dsn(r),
                                 signing_key_file=signing, signing_key_id='stage-1')
    assert replay['replayed'] is True


def test_the_snapshot_counts_expiring_and_expired_tokens_and_fires_v25(registered):  # noqa: F811
    r = registered
    credential = principal(r)['credential_id']
    digest = G.hashlib.sha256(token_of(r).encode()).hexdigest()
    with r['admin'].transaction():
        r['admin'].execute("update authz.nexloop_service_credentials set expires_at=clock_timestamp()+interval '3 days' where token_digest=%s", (digest,))
    soon = health(r)
    assert soon['expiring_7d'] == 1 and soon['expired_active'] == 0 and fired(soon) == {'service_credential_expiring'}
    later = r['admin'].execute("select runtime.nexloop_service_credential_health(%s,'real',clock_timestamp()+interval '4 days')", (TENANT,)).fetchone()[0]
    assert later['expired_active'] == 1 and fired(later) == {'service_credential_expiring', 'service_credential_expired'} - (
        set() if later['expiring_7d'] else {'service_credential_expiring'})
    assert datetime.fromisoformat(soon['earliest_expires_at']) < datetime.now(UTC) + timedelta(days=4)
    assert r['admin'].execute("select runtime.nexloop_metrics_snapshot(%s,'test') ? 'service_credentials'", (TENANT,)).fetchone()[0] is False
    assert credential  # the block names no credential id or token
    assert set(soon) == {'active', 'expiring_7d', 'expired_active', 'earliest_expires_at'}
