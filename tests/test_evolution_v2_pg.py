"""NX-039 V2 on PostgreSQL (design §3.4, §4.1, §5.1, §7.2; D39-1, D39-2, D39-7): the evaluator records the L0 report against the frozen
bundle and concludes evaluating -> accepted_for_release | rejected | incomplete in one transaction; SQL refuses a report that accepts
with a failed gate or a breaking change, hides a failed gate under another reason, or answers another gate version; the writer no
longer records reports; reports and review links stay frozen; the D4 review link takes the evaluator's report digest.

Reuses the V1 fixture (registered evaluator, frozen bundle). Records are synthetic E3-shaped accesses (D16)."""
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from nexloop_eios.evolution.evaluation import evaluate_l0
from support.tenants import TENANT_A
from test_evolution_e4_pg import OTHER_REF, REF, TERM, evo, hexid, table_digest  # noqa: F401
from test_evolution_e5_pg import CREATED, proposed, review_decision  # noqa: F401
from test_evolution_v1_pg import evaluation, status  # noqa: F401

FAST = {'latency_ms': {'parent': [10.0] * 50, 'candidate': [10.5] * 50}}


def records(cases):
    """E3-shaped records as the E4 fixture's miss cases: a browse that found nothing on TERM, another that returned both definitions."""
    out = []
    for run in cases:
        out.append({'access_id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'miss' + run)), 'kind': 'access', 'run_id': run, 'term_hashes': [TERM],
                    'result_refs': [], 'ambiguity': False})
        out.append({'access_id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'hit' + run)), 'kind': 'access', 'run_id': run,
                    'term_hashes': [hexid('other-' + run)], 'result_refs': [REF, OTHER_REF], 'ambiguity': False})
    return out


def report_for(f, *, inputs=FAST, patch=None):
    bundle = f['admin'].execute('select bundle from runtime.nexloop_evo_bundles where candidate_id=%s', (f['candidate'],)).fetchone()[0]
    cases = bundle['design_case_ids'] + bundle['validation_case_ids']
    return evaluate_l0(bundle, f['bundle_digest'], patch or f['patch'], records(cases), inputs=inputs, created_at=CREATED)


@pytest.fixture
def opened_eval(evaluation):  # noqa: F811
    assert evaluation['evaluator'].open(evaluation['candidate'], evaluation['bundle_digest'])['opened'] is True
    return evaluation


def test_the_evaluator_concludes_an_acceptance_and_the_review_link_uses_its_report(opened_eval):
    f = opened_eval
    before = table_digest(f['admin'])
    report = report_for(f)
    assert (report['conclusion'], report['change_class'], report['hard_gates_version']) == ('accept', 'minor', 'nx039-gates-1'), report['failed_gates']
    out = f['evaluator'].conclude(report)
    assert out['concluded'] is True and out['replayed'] is False and out['status'] == 'accepted_for_release' and status(f) == 'accepted_for_release'
    assert f['evaluator'].conclude(report) == {**out, 'replayed': True}
    with pytest.raises(psycopg.Error, match='reused'):
        f['evaluator'].conclude({**report, 'elapsed_ms': 9})
    event = f['admin'].execute("select event_kind,from_status,to_status,detail->>'change_class',recorded_by from runtime.nexloop_evo_candidate_events order by event_id desc limit 1").fetchone()
    assert event == ('evaluation_concluded', 'evaluating', 'accepted_for_release', 'minor', 'service:evaluator')
    # D4: the writer links the review decision to the evaluator's report digest.
    decision = review_decision(f['admin'])
    assert f['store'].link_review(decision, f['candidate'], out['report_digest'])['gate_result'] == 'pass'
    for table in ('runtime.nexloop_evo_reports', 'runtime.nexloop_evo_review_links'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='frozen'), f['admin'].transaction():
            f['admin'].execute('set local role nexloop_owner')
            f['admin'].execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
            f['admin'].execute(f'delete from {table}')
    assert table_digest(f['admin']) == before  # AT-023


def test_sql_refuses_a_report_that_hides_or_overrides_a_gate(opened_eval):
    f = opened_eval
    good = report_for(f)
    failing = report_for(f, inputs={})  # G7 fails: latency not measured
    assert failing['failed_gates'] == ['cost_performance'] and failing['reason'] == 'hard_gate_failed'
    fresh = lambda r: {**r, 'report_id': str(uuid.uuid4())}  # noqa: E731
    for broken, match in (
            ({**failing, 'conclusion': 'accept', 'reason': None}, 'accepts what it may not'),
            ({**failing, 'reason': 'not_better'}, 'failed gate under another reason'),
            ({**failing, 'failed_gates': []}, 'other failed gates'),
            ({**good, 'change_class': 'major'}, 'accepts what it may not'),
            ({**good, 'conclusion': 'reject', 'reason': 'not_better', 'change_class': 'major'}, 'breaking change under another reason'),
            ({**good, 'reproduced': False}, 'accepts what it may not'),
            ({**good, 'hard_gates_version': 'nx038-hard-gates-1'}, 'differs from its frozen bundle'),
            ({**good, 'hard_gates': good['hard_gates'][1:]}, 'every frozen hard gate'),
            ({**good, 'bundle_digest': 'e' * 64}, 'no frozen bundle'),
            ({**good, 'change_class': 'trivial'}, 'invalid')):
        with pytest.raises(psycopg.Error, match=match):
            f['evaluator'].conclude(fresh(broken))
    assert status(f) == 'evaluating'
    # A failed gate concludes rejected.
    assert f['evaluator'].conclude(failing)['status'] == 'rejected' and status(f) == 'rejected'
    with pytest.raises(psycopg.Error, match='not evaluating'):
        f['evaluator'].conclude(fresh(good))


def test_an_irreproducible_evaluation_ends_incomplete_with_evaluator_error(opened_eval):
    f = opened_eval
    report = {**report_for(f), 'reproduced': False, 'conclusion': 'incomplete', 'reason': 'not_reproducible'}
    assert f['evaluator'].conclude(report)['status'] == 'incomplete'
    assert f['admin'].execute('select status,end_reason from runtime.nexloop_evo_candidates where candidate_id=%s', (f['candidate'],)).fetchone() == ('incomplete', 'evaluator_error')


def test_the_writer_neither_records_reports_nor_concludes(opened_eval):
    f = opened_eval
    report = report_for(f)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        f['store'].record_report(report)
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_evo_writer'), autocommit=True) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select authz.nexloop_evo_evaluation_conclude(%s,%s)', (f['digest'], psycopg.types.json.Jsonb(report)))
    assert f['admin'].execute("select has_function_privilege('nexloop_evo_writer','authz.nexloop_evo_report_record(text,jsonb)','execute')").fetchone() == (False,)
    assert status(f) == 'evaluating'
