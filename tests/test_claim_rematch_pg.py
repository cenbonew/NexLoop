"""Claim → Consumer property phase one (migration claim_rematch; plan §1.3 a, §9.3): the operator release of Claims for rematching by
the reason of their latest match, as the claim matcher's own service. Claims matched while no edit Action was configured end
needs_resolution / type_not_writable; once edit_actions is configured, the release puts the eligible ones back to unresolved under a
new rematch generation and touches their Conversation, so the matcher matches them again instead of replaying the old outcome.
Never released: a Claim of an erasing or erased Consumer, of a Consumer whose persona is not real, a corrected or superseded Claim,
a Claim whose latest reason is another. Counts only; tenant and world from the credential; the payload names neither.
Actual PostgreSQL authority with the matcher of tests/test_claim_matching_pg.py."""
import hashlib
import json
from pathlib import Path

import psycopg
import pytest

from nexloop_eios.claim_matching import MATCHER_VERSION, ClaimMatcher, MatchConfiguration, ScriptedMatchProvider, rematch
from nexloop_eios.recall import EiosRecallAuthorizer, OntologyRecall
from multi_authority_fixture import seed_multi_authority
from test_claim_matching_pg import decision, env, matcher, matcher_targets, resolution  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
BUDGET = 'eios:property:Consumer/budget_level'


def unconfigured(f, decisions):
    """The matcher as on stage before phase one: no edit Action configured, so a Consumer Claim ends type_not_writable."""
    session, _ = seed_multi_authority(f['admin'], f['worker'], matcher_targets(f), identity_suffix='-rematch-before', tenant=f['tenant'])
    recall = OntologyRecall(f['worker'], session, authorizer=EiosRecallAuthorizer(f['worker'], session), provider=f['provider'], expected_dimension=64)
    return ClaimMatcher(f['worker'], session, f['signer'], recall=recall, provider=ScriptedMatchProvider(decisions), configuration=MatchConfiguration(edit_actions={}))


def latest_reason(admin, claim):
    return admin.execute('select reason from ontology.nexloop_claim_matches where claim_id=%s order by recorded_at desc limit 1', (claim,)).fetchone()[0]


def call(m, verb, **extra):
    return rematch(m.pool, m.session, m.signer, verb=verb, reason='type_not_writable', **extra)


@pytest.fixture
def stranded(env):
    """Six Consumer Claims left needs_resolution: four type_not_writable that must stay (erasing, persona, corrected, superseded),
    one eligible and one with another reason (tentative)."""
    f = env;admin = f['admin']
    labels = ('eligible', 'erasing', 'simulated', 'corrected', 'superseded')
    claims = {label: f['claims'].add('rematch-' + label, '预算', '两千元以内', kind='constraint') for label in labels}
    claims['tentative'] = f['claims'].add('rematch-tentative', '预算', '两千元以内', kind='constraint', modality='tentative')
    before = unconfigured(f, {c: decision(BUDGET, '两千元以内') for c in claims.values()})
    before.match_conversation(f['conversation'])
    assert {k: (resolution(admin, c), latest_reason(admin, c)) for k, c in claims.items()} == {
        **{k: ('needs_resolution', 'type_not_writable') for k in labels}, 'tentative': ('needs_resolution', 'modality_not_asserted:tentative')}
    # The states that exclude a Claim, as erasure, persona, correction and expiry leave them.
    admin.execute("update ontology.nexloop_claims set consumer_id='pseudonym:'||md5(claim_id) where claim_id=%s", (claims['erasing'],))
    other = hashlib.sha256(b'rematch-simulated-consumer').hexdigest()
    admin.execute('update ontology.nexloop_claims set consumer_id=%s where claim_id=%s', (other, claims['simulated']))
    admin.execute("""insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by)
        values(%s,'real',%s,'simulated_agent',1,'synthetic')""", (f['tenant'], other))
    correction = f['claims'].add('rematch-correction', '预算', '五千元以上', kind='correction')
    admin.execute('update ontology.nexloop_claims set corrects_claim_id=%s where claim_id=%s', (claims['corrected'], correction))
    admin.execute('update ontology.nexloop_claims set superseded_at=clock_timestamp() where claim_id=%s', (claims['superseded'],))
    return f, claims, before


def test_dry_run_counts_the_eligible_claim_only_and_writes_nothing(stranded):
    f, claims, before = stranded;admin = f['admin']
    state = admin.execute('select count(*),(select count(*) from ontology.nexloop_claim_rematch) from ontology.nexloop_claims where resolution_state=%s', ('needs_resolution',)).fetchone()
    assert call(before, 'dry_run') == {'eligible': 1, 'released': 0, 'conversations': 1}
    assert admin.execute('select count(*),(select count(*) from ontology.nexloop_claim_rematch) from ontology.nexloop_claims where resolution_state=%s', ('needs_resolution',)).fetchone() == state


def test_release_rematches_only_the_eligible_claim_under_a_new_generation(stranded):
    f, claims, before = stranded;admin = f['admin']
    out = call(before, 'release')
    assert out == {'eligible': 1, 'released': 1, 'conversations': 1}
    assert json.dumps(out).count(claims['eligible'][:12]) == 0  # counts only, never a Claim id
    assert resolution(admin, claims['eligible']) == 'unresolved'
    assert {k: resolution(admin, c) for k, c in claims.items() if k != 'eligible'} == {k: 'needs_resolution' for k in claims if k != 'eligible'}
    assert admin.execute('select generation,cause from ontology.nexloop_claim_rematch where claim_id=%s', (claims['eligible'],)).fetchone() == (1, 'rematch:type_not_writable')
    assert admin.execute("select count(*) from runtime.nexloop_work_feed where feed='claim-match' and item_key=%s", (f['conversation'],)).fetchone()[0] >= 1
    # Nothing left to release: a second run is a no-op.
    assert call(before, 'dry_run') == {'eligible': 0, 'released': 0, 'conversations': 0}
    # With edit_actions configured (phase one), the matcher matches it again under the rematch generation, never replaying. (The
    # Consumer ids changed above only stand for erasure and persona; put back so the Conversation's Claims read as one Consumer's.)
    admin.execute('update ontology.nexloop_claims set consumer_id=%s where claim_id=any(%s)', (f['consumer'], [claims['erasing'], claims['simulated']]))
    every = [r[0] for r in admin.execute('select claim_id from ontology.nexloop_claims where conversation_id=%s', (f['conversation'],)).fetchall()]
    m, _ = matcher(f, {c: decision(BUDGET, '两千元以内') for c in every}, suffix='-rematch-after')
    result = m.match_conversation(f['conversation'])[claims['eligible']]
    assert result['replay'] is False and result['outcome'] == 'full_match'
    assert admin.execute('select matcher_version from ontology.nexloop_claim_matches where claim_id=%s order by recorded_at desc limit 1',
                         (claims['eligible'],)).fetchone() == (MATCHER_VERSION + ';rematch:1',)


@pytest.mark.parametrize('payload', [{'verb': 'release', 'reason': 'type_not_writable', 'matcher_version': MATCHER_VERSION, 'tenant_id': 'x'},
                                     {'verb': 'release', 'reason': 'type_not_writable', 'matcher_version': MATCHER_VERSION, 'world': 'real'},
                                     {'verb': 'release', 'reason': 'no_match', 'matcher_version': MATCHER_VERSION},
                                     {'verb': 'delete', 'reason': 'type_not_writable', 'matcher_version': MATCHER_VERSION},
                                     {'verb': 'release', 'reason': 'type_not_writable', 'matcher_version': 'other/1'}])
def test_the_payload_names_no_tenant_or_world_and_only_known_verbs_and_reasons(stranded, payload):
    from nexloop_eios.claim_matching import _Port
    f, claims, before = stranded
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='claim rematch invalid'):
        _Port(before.pool, before.session, before.signer).call('nexloop_claim_rematch', payload)
    assert resolution(f['admin'], claims['eligible']) == 'needs_resolution'


def test_only_the_claim_match_service_may_call(stranded):
    f, claims, before = stranded
    session, _ = seed_multi_authority(f['admin'], f['worker'], matcher_targets(f, match=False), identity_suffix='-rematch-nogrant', tenant=f['tenant'])
    from eios.authz.errors import AuthorizationUnavailable
    with pytest.raises((PermissionError, AuthorizationUnavailable)):  # no nexloop.claim.match EXECUTE: refused before any SQL write
        rematch(f['worker'], session, f['signer'], verb='release', reason='type_not_writable')
    with pytest.raises(ValueError):
        rematch(f['worker'], before.session, f['signer'], verb='release', reason='no_match')
    assert resolution(f['admin'], claims['eligible']) == 'needs_resolution'


def test_the_command_reports_counts_or_the_error_type_only(tmp_path, capsys):
    import sys
    sys.path.insert(0, str(ROOT / 'scripts/deploy'))
    import claim_rematch as R
    secret = tmp_path / 'dsn';secret.write_text('postgresql://user:synthetic-secret@localhost/db');secret.chmod(0o600)
    args = ['--database-url-file', str(secret), '--service-credential-file', str(secret), '--signing-key-file', str(secret),
            '--signing-key-id', 'k', '--artifact-root', str(tmp_path), '--reason', 'type_not_writable']
    assert R.main(args) == 2
    out = capsys.readouterr().out
    assert json.loads(out)['verb'] == 'dry_run' and json.loads(out)['done'] is False and 'synthetic-secret' not in out
    with pytest.raises(SystemExit):
        R.main([*args[:-1], 'no_match'])
