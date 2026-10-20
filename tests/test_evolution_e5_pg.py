"""NX-038 E5 (design §12.3, §12.4, §12.6; D4, D7, D16, D18, D19): the candidate overlay, L0 deterministic replay, the frozen
evaluation bundle and report handed to NX-039, and the review link table. AT-023 (isolation), AT-046 server-side negatives
(evaluation only in world test with a test-only credential, no effect entry), AT-048 (the bundle carries case groups; a leak is
refused before evaluation).

The replay is pure and runs on synthetic access records shaped like E3 output (D16: synthetic or structure-generated cases only).
The PostgreSQL part reuses the E4 fixture (bootstrapped catalog, login roles nexloop_evo_writer / nexloop_api); admin only seeds
synthetic rows (a review decision with foreign-key triggers off, the `evaluating` status NX-039 would set) and probes. Nothing
here holds a model, channel or effect credential.
"""
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from authority_fixture import seed_authority
from nexloop_eios.context_engine.semantic import EvaluationOverlay, OverlayRefused, SemanticAdapter, load_overlay, term_hash
from nexloop_eios.evolution.replay import BundleLeak, check_bundle, replay_l0
from nexloop_eios.evolution_store import diagnosis_id
from support.tenants import TENANT_A
from test_evolution_e4_pg import PROTECTED, REF, TERM, evo, hexid, opened, table_digest  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = json.loads((ROOT / 'packages/contracts/evolution-evaluation-bundle.schema.json').read_text())
REPORT = json.loads((ROOT / 'packages/contracts/evolution-evaluation-report.schema.json').read_text())
SEED = '0' * 32
CREATED = '2026-10-20T04:00:00Z'
GATES = ['references_valid', 'structure_valid', 'zero_overreach', 'zero_cross_tenant', 'zero_fact_pollution', 'no_forbidden_regression',
         'index_migration_impact']  # nx038-hard-gates-1, answered by replay_l0
V2_GATES = ['references_valid', 'structure_valid', 'zero_overreach', 'zero_cross_tenant', 'zero_fact_pollution', 'no_forbidden_regression',
            'semantic_regression', 'cost_performance', 'semantic_conflict', 'index_migration_impact']  # NX-039 V2: what a bundle freezes now


def accesses_for(cases, *, cover_other=True):
    """Per case Run: one browse that missed on TERM (what the alias patch targets) and one that found a definition."""
    out = []
    for run_id in cases:
        out.append({'access_id': str(uuid.uuid4()), 'kind': 'access', 'run_id': run_id, 'term_hashes': [TERM], 'result_refs': [], 'ambiguity': False})
        if cover_other:
            out.append({'access_id': str(uuid.uuid4()), 'kind': 'access', 'run_id': run_id, 'term_hashes': [hexid('other' + run_id)],
                        'result_refs': [REF], 'ambiguity': False})
    return out


def pure_bundle(design=('a',), validation=('b',)):
    ids = {k: str(uuid.uuid5(uuid.NAMESPACE_URL, k)) for k in design + validation}
    return {'candidate_id': str(uuid.uuid4()), 'design_case_ids': [ids[k] for k in design], 'validation_case_ids': [ids[k] for k in validation],
            'case_groups': {ids[k]: 'consumer:' + k for k in design + validation}, 'levels': ['L0'], 'evaluator_version': 'l0-1',
            'non_inferiority': {'primary_metric': 'coverage', 'margin': 0.01, 'ties_accept': False}, 'budget_remaining': {'unit': 'CNY'}}


ALIAS = {'change_kind': 'alias', 'target_refs': [REF['ref']], 'term_hashes': [TERM], 'run_ids': []}


# --- pure L0 -----------------------------------------------------------------------------------------------------------------

def test_l0_replay_is_deterministic_and_never_accepts_without_improvement():
    b = pure_bundle()
    cases = b['design_case_ids'] + b['validation_case_ids']
    report = replay_l0(b, 'd' * 64, ALIAS, accesses_for(cases), created_at=CREATED)
    assert report['conclusion'] == 'accept' and report['reason'] is None
    assert report['scores']['design'] == {'cases': 1, 'parent': 0.5, 'candidate': 1.0} and report['scores']['metrics']['regressions'] == 0
    assert [g['gate'] for g in report['hard_gates']] == GATES and all(g['result'] == 'pass' for g in report['hard_gates'])
    again = replay_l0(b, 'd' * 64, ALIAS, list(reversed(accesses_for(cases))), created_at=CREATED)
    assert {k: v for k, v in again.items() if k != 'hard_gates'} == {k: v for k, v in report.items() if k != 'hard_gates'}  # M22
    assert [(g['gate'], g['result']) for g in again['hard_gates']] == [(g['gate'], g['result']) for g in report['hard_gates']]
    # A description patch changes no lookup: equal scores are never an acceptance (D7).
    same = replay_l0(b, 'd' * 64, {**ALIAS, 'change_kind': 'description'}, accesses_for(cases), created_at=CREATED)
    assert (same['conclusion'], same['reason']) == ('reject', 'not_better')
    # A patch reaching outside definition refs fails its hard gates and is rejected whatever it scores (AT-024, M22).
    bad = replay_l0(b, 'd' * 64, {**ALIAS, 'target_refs': ['eios:action:Order.cancel:1']}, accesses_for(cases), created_at=CREATED)
    assert (bad['conclusion'], bad['reason']) == ('reject', 'hard_gate_failed')
    assert {g['gate'] for g in bad['hard_gates'] if g['result'] == 'fail'} == {'references_valid', 'zero_overreach'}
    # Missing inputs are incomplete, never a pass.
    gone = replay_l0(b, 'd' * 64, ALIAS, accesses_for(cases[:1]), created_at=CREATED)
    assert (gone['conclusion'], gone['reason'], gone['scores']['metrics']['missing_cases']) == ('incomplete', 'missing_data', 1)
    # An access of a Run outside the bundle is a cross-tenant / overreach signal: the gate fails.
    stray = replay_l0(b, 'd' * 64, ALIAS, accesses_for(cases) + accesses_for([str(uuid.uuid4())]), created_at=CREATED)
    assert {g['gate'] for g in stray['hard_gates'] if g['result'] == 'fail'} == {'zero_cross_tenant'}


def test_a_cluster_on_both_sides_stops_the_evaluation_before_it_starts():
    """AT-048: NX-039 re-checks the frozen case groups; a leak is refused, not scored."""
    b = pure_bundle(design=('a', 'c'), validation=('b',))
    check_bundle(b)
    b['case_groups'][b['design_case_ids'][1]] = b['case_groups'][b['validation_case_ids'][0]]
    with pytest.raises(BundleLeak):
        replay_l0(b, 'd' * 64, ALIAS, accesses_for(b['design_case_ids'] + b['validation_case_ids']), created_at=CREATED)
    del b['case_groups'][b['design_case_ids'][0]]
    with pytest.raises(BundleLeak):
        check_bundle(b)


class _Recall:
    def recall(self, query, **_):
        return SimpleNamespace(definitions=[], config_version='c1', profile_id=None)


def test_the_semantic_adapter_takes_an_overlay_only_outside_world_real():
    overlay = EvaluationOverlay(candidate_id=str(uuid.uuid4()), patch_digest='a' * 64, change_kind='alias', target_refs=(REF['ref'],),
                                term_hashes=frozenset({term_hash('synthetic alias')}))
    real = SimpleNamespace(world='real')
    with pytest.raises(OverlayRefused):
        SemanticAdapter(None, real, None, _Recall(), overlay=overlay)
    with pytest.raises(OverlayRefused):
        load_overlay(None, real, overlay.candidate_id, overlay.patch_digest)
    adapter = SemanticAdapter(None, SimpleNamespace(world='test'), None, _Recall(), overlay=overlay)
    adapter._definitions = lambda refs: ({r: {'ref': r, 'schema_version': 3, 'aliases': []} for r in refs}, [])  # READ gate stubbed: refs only
    hit = adapter.browse('synthetic alias')
    assert [i['ref'] for i in hit['items']] == [REF['ref']] and hit['items'][0]['method'] == 'evaluation_overlay' and hit['overlay'] == overlay.candidate_id
    assert adapter.browse('another phrase')['items'] == []
    assert adapter.resolve(['synthetic alias'])['resolutions'][0]['status'] == 'resolved'
    # Without an overlay the same adapter shape reads the active definitions only.
    plain = SemanticAdapter(None, real, None, _Recall())
    plain._definitions = adapter._definitions
    assert plain.browse('synthetic alias')['items'] == [] and 'overlay' not in plain.browse('synthetic alias')


# --- PostgreSQL ------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def proposed(evo):  # noqa: F811
    f = evo
    opened(f)
    out = f['store'].propose(diagnosis_id(f['diagnosis'].snapshot_digest), f['drafts'][0])
    f['candidate'] = out['candidate']['candidate_id']
    f['patch'] = f['drafts'][0].patch
    return f


def review_decision(admin, tenant=TENANT_A):
    decision = str(uuid.uuid4())
    with admin.transaction():
        admin.execute('set local session_replication_role = replica')  # the S3 candidate definition it decides is not what this test is about
        admin.execute("""insert into ontology.nexloop_review_decisions(tenant_id,world,decision_id,candidate_id,decision,outcome,reviewer_ref,reviewer_principal,
            expected_revision,rationale,record,authority,reflow_status,decided_at) values(%s,'real',%s,'synthetic-definition','reject','rejected','r','p',1,'synthetic','{}','{}','none',now())""",
                      (tenant, decision))
    return decision


def test_bundle_report_and_review_link_hand_over_without_touching_active_state(proposed):
    f = proposed
    admin, store = f['admin'], f['store']
    before = table_digest(admin)
    frozen = store.freeze_bundle(f['candidate'], seed=SEED)
    bundle, digest = frozen['bundle'], frozen['bundle_digest']
    jsonschema.validate(bundle, BUNDLE)
    assert frozen['replayed'] is False and bundle['levels'] == ['L0'] and bundle['hard_gates'] == V2_GATES and bundle['hard_gates_version'] == 'nx039-gates-1'
    assert bundle['budget_remaining'] == {'unit': 'CNY', 'rounds': 3, 'diagnosis': '20', 'candidate': '10', 'tenant_month': '100'}
    stored = admin.execute('select body_digest from runtime.nexloop_evo_patches').fetchone()[0]
    assert bundle['patch_digest'] == stored and bundle['design_case_ids'] == f['drafts'][0].candidate['design_case_ids']
    assert store.freeze_bundle(f['candidate'], seed=SEED) == {**frozen, 'replayed': True}
    for levels, seed in ((('L0', 'L1'), SEED), (('L0',), '1' * 32)):  # NX-039 may not change a frozen condition
        with pytest.raises(psycopg.Error, match='frozen'):
            store.freeze_bundle(f['candidate'], levels=levels, seed=seed)
    with pytest.raises(psycopg.Error, match='invalid'):
        store.freeze_bundle(f['candidate'], levels=('L1',), seed=SEED)
    # NX-039 V2 (D39-1): the writer no longer records reports; the evaluator does (tests/test_evolution_v2_pg.py, also for the D4 review
    # link that needs a recorded report).
    cases = bundle['design_case_ids'] + bundle['validation_case_ids']
    report = replay_l0(bundle, digest, f['patch'], accesses_for(cases), created_at=CREATED)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        store.record_report(report)
    assert store.read(f['candidate'])['candidate']['status'] == 'proposed'
    events = [r[0] for r in admin.execute('select event_kind from runtime.nexloop_evo_candidate_events order by event_id')]
    assert events[-1] == 'bundle_frozen'
    for table in ('runtime.nexloop_evo_bundles',):  # reports and review links: tests/test_evolution_v2_pg.py
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='frozen'), admin.transaction():
            admin.execute('set local role nexloop_owner')
            admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
            admin.execute(f'delete from {table}')
    assert table_digest(admin) == before  # AT-023: no definition, object, Claim, budget limit, authority fact or grant changed
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_evo_reader'), autocommit=True) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select authz.nexloop_evo_bundle_freeze(%s,%s,%s,%s)', (f['digest'], f['candidate'], ['L0'], SEED)).fetchone()


def test_the_overlay_is_read_only_in_world_test_with_a_registered_evaluation_credential(proposed):
    f = proposed
    admin = f['admin']
    patch_digest = admin.execute('select body_digest from runtime.nexloop_evo_patches').fetchone()[0]
    test_token, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32, world='test', identity_suffix='-evaluation')
    test_digest = hexid(test_token)
    before = table_digest(admin)

    def overlay(digest, world, candidate=f['candidate'], patch=patch_digest, role='nexloop_api'):
        with psycopg.connect(make_conninfo(f['pg'], user=role), autocommit=True) as db:
            return db.execute('select authz.nexloop_evo_overlay(%s,%s,%s,%s)', (digest, world, candidate, patch)).fetchone()[0]

    # AT-023 second line: world real is refused at the entry, whatever credential is passed.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='outside world test'):
        overlay(f['digest'], 'real')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        overlay(test_digest, 'test')  # not registered as the evaluation credential yet
    # D18: only a credential issued for world test alone can be registered.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='world test only'):
        admin.execute("insert into authz.nexloop_evo_evaluation_credentials(token_digest,tenant_id,registered_by) values(%s,%s,'synthetic-config')", (f['digest'], TENANT_A))
    admin.execute("insert into authz.nexloop_evo_evaluation_credentials(token_digest,tenant_id,registered_by) values(%s,%s,'synthetic-config')", (test_digest, TENANT_A))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        overlay(test_digest, 'test')  # the candidate is proposed, not evaluating
    admin.execute("update runtime.nexloop_evo_candidates set status='evaluating' where candidate_id=%s", (f['candidate'],))  # as NX-039 would
    got = overlay(test_digest, 'test')
    assert got == {'candidate_id': f['candidate'], 'patch_digest': patch_digest, 'change_kind': 'alias', 'target_refs': f['patch']['target_refs'],
                   'term_hashes': f['patch']['term_hashes']}
    assert 'run_ids' not in got
    for bad in (dict(patch='f' * 64), dict(candidate=str(uuid.uuid4())), dict(role='nexloop_evo_writer')):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            overlay(test_digest, 'test', **bad)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='outside world test'):
        overlay(test_digest, 'real')
    with pytest.raises(psycopg.Error):
        overlay(test_digest, 'simulation')
    assert table_digest(admin) == before
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='never changed'):
        admin.execute('update authz.nexloop_evo_evaluation_credentials set registered_by=%s', ('x',))
