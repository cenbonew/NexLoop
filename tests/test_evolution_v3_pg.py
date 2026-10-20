"""NX-039 V3 on PostgreSQL (design §3.4, §5.2, §5.3, §6.1; D39-4, D39-6, NX-038 D15, D16, D18, D20): the L1 test-world session.

Credential isolation (a registered evaluation credential issued for world test alone and holding no Action grant; it cannot
authenticate in world real and real dispatch is closed for world test - AT-046 negatives), the definition snapshot and its cleanup
(only the session's close or the sweep removes it), the evolution budget (each L1 run counted as evolution of the candidate; a spent
cap ends incomplete/budget_exhausted), the 30-minute deadline (incomplete/evaluator_timeout), and an L1 report concluded through the
evaluator. Runs are synthetic (D16): the runner is a stand-in returning counts and flags only; no model, Run or effect is called."""
import uuid
from datetime import timedelta

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from authority_fixture import seed_authority
from eios.authz.resources import ResourceType
from nexloop_eios.evolution.evaluation import evaluate_l0
from nexloop_eios.evolution.l1 import run_l1
from nexloop_eios.evolution_store import EvaluatorStore
from support.tenants import TENANT_A
from test_evolution_e4_pg import NOW, evo, hexid, table_digest  # noqa: F401
from test_evolution_e5_pg import CREATED, SEED, proposed  # noqa: F401
from test_evolution_traces_pg import private
from test_evolution_v1_pg import register, status
from test_evolution_v2_pg import FAST, records

FAR = NOW + timedelta(days=1)


def suspended(admin, sql, params=()):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica');admin.execute(sql, params)


def eval_credential(admin, suffix, resource='eios:artifact:' + 'e' * 32, kind=ResourceType.ARTIFACT, registered=True):
    token, _ = seed_authority(admin, TENANT_A, resource, world='test', resource_type=kind, identity_suffix=suffix)
    if registered:
        admin.execute("insert into authz.nexloop_evo_evaluation_credentials(token_digest,tenant_id,registered_by) values(%s,%s,'synthetic-config')",
                      (hexid(token), TENANT_A))
    return token, hexid(token)


@pytest.fixture
def l1(proposed, tmp_path):  # noqa: F811
    f = proposed
    admin = f['admin']
    token, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32, identity_suffix='-evaluator')
    register(admin, hexid(token), 'evaluator')
    connect = lambda: psycopg.connect(make_conninfo(f['pg'], user='nexloop_evo_evaluator'), autocommit=True)  # noqa: E731
    f['evaluator'] = EvaluatorStore(connect, private(tmp_path / 'evo-evaluator', token))
    frozen = f['store'].freeze_bundle(f['candidate'], levels=('L0', 'L1'), seed=SEED)
    f['bundle'], f['bundle_digest'] = frozen['bundle'], frozen['bundle_digest']
    assert f['evaluator'].open(f['candidate'], f['bundle_digest'])['opened'] is True
    f['test_token'], f['test_digest'] = eval_credential(admin, '-l1')
    return f


def definitions(admin, session=None):
    sql = 'select count(*) from runtime.nexloop_evo_l1_definitions'
    return admin.execute(sql + (' where session_id=%s' if session else ''), ((session,) if session else ())).fetchone()[0]


def test_only_a_registered_test_only_credential_without_action_grants_opens_l1(l1):
    f = l1
    admin, ev = f['admin'], f['evaluator']
    # Not registered as an evaluation credential (and a real one could never be: E5's registry takes world test alone).
    _, unregistered = eval_credential(admin, '-l1-unregistered', registered=False)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='test-only evaluation credential'):
        ev.l1_open(f['candidate'], unregistered)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='test-only evaluation credential'):
        ev.l1_open(f['candidate'], f['digest'])  # the writer's real credential
    # D18: a test credential that holds an Action grant (an effect entry) is refused.
    _, acting = eval_credential(admin, '-l1-acting', resource='eios:action:Contact.release:1', kind=ResourceType.ACTION)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='Action grant'):
        ev.l1_open(f['candidate'], acting)
    opened = ev.l1_open(f['candidate'], f['test_digest'])
    assert opened['opened'] is True and opened['replayed'] is False and opened['copied'] == definitions(admin) > 0
    assert ev.l1_open(f['candidate'], f['test_digest'])['replayed'] is True
    # AT-046 negatives: the evaluation credential never authenticates in world real; real dispatch is closed for world test.
    with pytest.raises(psycopg.errors.InsufficientPrivilege), admin.transaction():
        admin.execute("select authz.nexloop_service_identity_snapshot(%s,'real')", (f['test_digest'],))
    for setting in ('', 'true'):
        with admin.transaction():
            admin.execute("select set_config('nexloop.real_dispatch_enabled',%s,true)", (setting,))
            layer = admin.execute("select control.nexloop_real_dispatch_closed_layer(%s,'test','eios:action:nexloop.message.staff_send:1')", (TENANT_A,)).fetchone()[0]
        assert layer in ('deployment', 'world') and (setting != 'true' or layer == 'world')
    # The overlay is served in world test only, for this credential (E5), and refused in world real.
    patch_digest = admin.execute('select body_digest from runtime.nexloop_evo_patches where candidate_id=%s', (f['candidate'],)).fetchone()[0]
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_api'), autocommit=True) as db:
        assert db.execute('select authz.nexloop_evo_overlay(%s,%s,%s,%s)', (f['test_digest'], 'test', f['candidate'], patch_digest)).fetchone()[0]['candidate_id'] == f['candidate']
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute('select authz.nexloop_evo_overlay(%s,%s,%s,%s)', (f['test_digest'], 'real', f['candidate'], patch_digest))


def test_the_snapshot_holds_refs_versions_and_hashes_only_and_leaves_with_the_session(l1):
    f = l1
    admin, ev = f['admin'], f['evaluator']
    before = table_digest(admin)
    session = ev.l1_open(f['candidate'], f['test_digest'])['session_id']
    snap = ev.l1_definitions(session)
    assert {s['source'] for s in snap} == {'parent', 'overlay'} and all(set(s) == {'ref', 'source', 'schema_version', 'alias_hashes'} for s in snap)
    assert {s['ref'] for s in snap} == set(f['patch']['target_refs'])
    assert [s['alias_hashes'] for s in snap if s['source'] == 'overlay'][0] == sorted(f['patch']['term_hashes'])
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='only when their session closes'), admin.transaction():
        admin.execute('set local role nexloop_owner')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute('delete from runtime.nexloop_evo_l1_definitions')
    closed = ev.l1_close(session)
    assert (closed['status'], closed['close_reason'], closed['remaining']) == ('closed', 'done', 0) and closed['removed'] == len(snap)
    assert definitions(admin) == 0 and ev.l1_definitions(session) is None
    assert ev.l1_close(session)['status'] == 'closed'  # idempotent
    assert table_digest(admin) == before  # AT-023: no definition, object, Claim, budget limit, authority fact or grant written
    assert f['admin'].execute('select count(*) from runtime.nexloop_evo_l1_sessions').fetchone()[0] == 1


def test_l1_runs_are_counted_as_evolution_of_the_candidate_and_a_spent_cap_ends_incomplete(l1):
    f = l1
    admin, ev = f['admin'], f['evaluator']
    session = ev.l1_open(f['candidate'], f['test_digest'])['session_id']
    got = ev.l1_reserve(session, 'l1:case:candidate:0', '4', 'CNY')
    assert got['reserved'] is True and got['candidate_remaining'] == '6'
    assert ev.l1_reserve(session, 'l1:case:candidate:0', '4', 'CNY')['replayed'] is True
    row = admin.execute("select budget_kind,source_ref,principal_id from control.nexloop_budget_consumption where consumption_id='l1:case:candidate:0'").fetchone()
    assert row == ('evolution', 'evolution-candidate:' + f['candidate'], 'service:evaluator')  # D15: never the model budget
    assert admin.execute("select count(*) from control.nexloop_budget_consumption where budget_kind='model'").fetchone()[0] == 0
    spent = ev.l1_reserve(session, 'l1:case:candidate:1', '7', 'CNY')  # 4 + 7 > the candidate's 10 (D20)
    assert (spent['reserved'], spent['reason'], spent['tier']) == (False, 'budget_exhausted', 'candidate')
    assert admin.execute('select status,end_reason from runtime.nexloop_evo_candidates where candidate_id=%s', (f['candidate'],)).fetchone() == ('incomplete', 'budget_exhausted')
    assert definitions(admin) == 0  # the session closed with the candidate
    assert ev.l1_reserve(session, 'l1:case:candidate:2', '1', 'CNY')['reason'] == 'session_closed'


def test_the_deadline_ends_incomplete_evaluator_timeout_and_the_sweep_cleans_up(l1):
    f = l1
    admin, ev = f['admin'], f['evaluator']
    session = ev.l1_open(f['candidate'], f['test_digest'])['session_id']
    with pytest.raises(psycopg.Error, match='deadline has not passed'):
        ev.l1_close(session, 'timeout')  # the evaluator cannot declare a timeout early
    suspended(admin, "update runtime.nexloop_evo_l1_sessions set opened_at=now()-interval '31 minutes',deadline=now()-interval '1 minute' where session_id=%s", (session,))
    assert ev.l1_reserve(session, 'l1:late:0', '1', 'CNY') == {'reserved': False, 'reason': 'evaluator_timeout'}
    assert admin.execute('select status,end_reason from runtime.nexloop_evo_candidates where candidate_id=%s', (f['candidate'],)).fetchone() == ('incomplete', 'evaluator_timeout')
    assert definitions(admin) == 0 and admin.execute("select close_reason from runtime.nexloop_evo_l1_sessions").fetchone()[0] == 'timeout'


def test_a_crashed_evaluator_is_swept(l1):
    f = l1
    admin, ev = f['admin'], f['evaluator']
    session = ev.l1_open(f['candidate'], f['test_digest'])['session_id']
    suspended(admin, "update runtime.nexloop_evo_l1_sessions set opened_at=now()-interval '31 minutes',deadline=now()-interval '1 minute' where session_id=%s", (session,))
    assert ev.l1_sweep() == {'swept': 1} and definitions(admin) == 0
    assert status(f) == 'incomplete' and ev.l1_sweep() == {'swept': 0}


def test_an_l1_report_concludes_through_the_evaluator(l1):
    f = l1
    admin, ev = f['admin'], f['evaluator']
    session = ev.l1_open(f['candidate'], f['test_digest'])['session_id']
    b = f['bundle']
    cases = b['design_case_ids'] + b['validation_case_ids']
    l0 = evaluate_l0(b, f['bundle_digest'], f['patch'], records(cases), inputs=FAST, created_at=CREATED)
    reserve = lambda cid_, amount: ev.l1_reserve(session, cid_, amount, 'CNY')['reserved']  # noqa: E731
    runner = lambda case, variant, i: {'success': variant == 'candidate' or i == 1, 'tool_param_errors': 0, 'model_calls': 2, 'cost': '0.01', 'violations': {}}  # noqa: E731
    report = run_l1(b, f['bundle_digest'], l0, [{'case_id': c, 'origin': 'synthetic'} for c in cases], runner, reserve=reserve,
                    now=lambda: 0, deadline=1, created_at=CREATED)
    assert (report['level'], report['conclusion']) == ('L1', 'accept'), report
    used = admin.execute("select count(*),sum(amount) from control.nexloop_budget_consumption where source_ref=%s", ('evolution-candidate:' + f['candidate'],)).fetchone()
    assert used[0] == len(cases) * 6
    assert ev.conclude(report)['status'] == 'accepted_for_release'
    assert ev.l1_close(session)['remaining'] == 0
    # A timed-out report is recordable as incomplete/evaluator_timeout (0248 conclude).
    assert 'evaluator_timeout' in admin.execute("select pg_get_functiondef('authz.nexloop_evo_evaluation_conclude(text,jsonb)'::regprocedure)").fetchone()[0]
    with pytest.raises(psycopg.Error, match='not evaluating'):
        ev.conclude({**report, 'report_id': str(uuid.uuid4())})
