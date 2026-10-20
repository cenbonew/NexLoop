"""NX-039 V1 (design §2.2, §3.4, §4.2, §4.3, §5.1; owner D39-1, D39-2, D39-3 decided 2026-10-20): the evaluator role and the opening of
an evaluation. One purpose per service principal (a writer never evaluates, an evaluator never writes), the seven pre-evaluation
checks each with its own reason code, proposed -> evaluating, holdout registration and the 5-candidate use limit.

Reuses the E4/E5 fixtures (bootstrapped catalog, login role nexloop_evo_writer, a proposed candidate, its frozen bundle). Admin only
registers principals, seeds synthetic rows (erasures, stops, consumption, duplicate candidates for the holdout limit) and probes.
"""
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from authority_fixture import seed_authority
from nexloop_eios.evolution_store import OPEN_REFUSALS, EvaluatorStore, StoreUnavailable
from support.tenants import TENANT_A
from test_evolution_e4_pg import evo, hexid, table_digest  # noqa: F401
from test_evolution_e5_pg import SEED, proposed  # noqa: F401
from test_evolution_traces_pg import private


def principal(admin, digest):
    return admin.execute("select authz.nexloop_service_identity_snapshot(%s,'real')->'binding'->>'subject_principal_id'", (digest,)).fetchone()[0]


def register(admin, digest, purpose, tenant=TENANT_A):
    admin.execute("insert into authz.nexloop_evo_principals(principal_id,tenant_id,purpose,registered_by) values(%s,%s,%s,'synthetic-config')",
                  (principal(admin, digest), tenant, purpose))


@pytest.fixture
def evaluation(proposed, tmp_path):  # noqa: F811
    f = proposed
    admin = f['admin']
    token, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32, identity_suffix='-evaluator')
    f['evaluator_digest'] = hexid(token)
    register(admin, f['evaluator_digest'], 'evaluator')
    connect = lambda: psycopg.connect(make_conninfo(f['pg'], user='nexloop_evo_evaluator'), autocommit=True)  # noqa: E731
    f['evaluator'] = EvaluatorStore(connect, private(tmp_path / 'evo-evaluator', token))
    f['evaluator_token'] = token
    frozen = f['store'].freeze_bundle(f['candidate'], seed=SEED)
    f['bundle_digest'] = frozen['bundle_digest']
    return f


def status(f, candidate=None):
    return f['admin'].execute('select status from runtime.nexloop_evo_candidates where candidate_id=%s', (candidate or f['candidate'],)).fetchone()[0]


def test_one_purpose_per_principal_a_writer_never_evaluates_and_an_evaluator_never_writes(evaluation, tmp_path):
    f = evaluation
    admin = f['admin']
    # The registry holds one row per principal: the same principal cannot also be registered as the other purpose.
    with pytest.raises(psycopg.errors.UniqueViolation):
        register(admin, f['evaluator_digest'], 'writer')
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='registered once'):
        admin.execute("update authz.nexloop_evo_principals set purpose='writer'")
    # The evaluator's credential is refused by every writer entry (it may not propose, freeze, report or link).
    as_writer = type(f['store'])(f['store'].connect, private(tmp_path / 'evaluator-as-writer', f['evaluator_token']))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='evaluator principal'):
        as_writer.read(f['candidate'])
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='evaluator principal'):
        as_writer.freeze_bundle(f['candidate'], seed=SEED)
    # The writer's credential is not an evaluator: unregistered, it cannot open; and the writer role cannot reach the function.
    as_evaluator = EvaluatorStore(f['evaluator'].connect, f['credential'])
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='evaluation unavailable'):
        as_evaluator.open(f['candidate'], f['bundle_digest'])
    register(admin, f['digest'], 'writer')
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='evaluation unavailable'):
        as_evaluator.open(f['candidate'], f['bundle_digest'])
    assert f['store'].read(f['candidate'])['candidate']['status'] == 'proposed'  # a registered writer still writes
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_evo_writer'), autocommit=True) as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select authz.nexloop_evo_evaluation_open(%s,%s,%s)', (f['digest'], f['candidate'], f['bundle_digest']))
    with pytest.raises(StoreUnavailable):
        EvaluatorStore(f['store'].connect, private(tmp_path / 'x', f['evaluator_token'])).open(f['candidate'], f['bundle_digest'])
    # The evaluator role holds nothing but the evaluation entry: no table, no writer function.
    for table in ('runtime.nexloop_evo_candidates', 'runtime.nexloop_evo_bundles', 'runtime.nexloop_evo_reports', 'runtime.nexloop_evo_holdouts'):
        assert admin.execute("select has_table_privilege('nexloop_evo_evaluator',%s,'select')", (table,)).fetchone() == (False,)
    for fn in ('authz.nexloop_evo_candidate_propose(text,jsonb)', 'authz.nexloop_evo_report_record(text,jsonb)', 'authz.nexloop_evo_bundle_freeze(text,uuid,text[],text)'):
        assert admin.execute("select has_function_privilege('nexloop_evo_evaluator',%s,'execute')", (fn,)).fetchone() == (False,)
    # The registered evaluator itself opens it.
    assert f['evaluator'].open(f['candidate'], f['bundle_digest'])['opened'] is True


def test_opening_turns_proposed_into_evaluating_once_and_replays(evaluation):
    f = evaluation
    before = table_digest(f['admin'])
    out = f['evaluator'].open(f['candidate'], f['bundle_digest'])
    assert out['opened'] is True and out['replayed'] is False and out['holdout_use'] == 1 and status(f) == 'evaluating'
    assert f['evaluator'].open(f['candidate'], f['bundle_digest']) == {**out, 'replayed': True}
    kinds = f['admin'].execute("select event_kind,from_status,to_status,recorded_by from runtime.nexloop_evo_candidate_events order by event_id desc limit 1").fetchone()
    assert kinds == ('evaluation_opened', 'proposed', 'evaluating', 'service:evaluator')
    assert table_digest(f['admin']) == before  # AT-023: opening touches no definition, object, Claim, budget limit or authority
    for table in ('runtime.nexloop_evo_holdouts', 'runtime.nexloop_evo_holdout_uses'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='frozen'), f['admin'].transaction():
            f['admin'].execute('set local role nexloop_owner')
            f['admin'].execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
            f['admin'].execute(f'delete from {table}')


def refused(f, reason, bundle=None):
    out = f['evaluator'].open(f['candidate'], bundle or f['bundle_digest'])
    assert out == {'opened': False, 'candidate_id': f['candidate'], 'reason': reason, 'status': status(f)}
    assert reason in OPEN_REFUSALS


def suspended(admin, sql, params=()):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica');admin.execute(sql, params)


def test_each_pre_evaluation_check_refuses_with_its_own_code(evaluation):
    f = evaluation
    admin, c = f['admin'], f['candidate']
    # 1. bundle_mismatch: another digest; or the candidate row no longer says what the frozen bundle says.
    refused(f, 'bundle_mismatch', bundle='e' * 64)
    suspended(admin, "update runtime.nexloop_evo_candidates set model_profile='changed' where candidate_id=%s", (c,))
    refused(f, 'bundle_mismatch')
    suspended(admin, "update runtime.nexloop_evo_candidates set model_profile='none' where candidate_id=%s", (c,))
    # 2. candidate_not_ready: its diagnosis was invalidated (an erasure or a budget end does this).
    suspended(admin, "update runtime.nexloop_evo_diagnoses set status='incomplete',end_reason='missing_data',closed_at=now()")
    refused(f, 'candidate_not_ready')
    suspended(admin, "update runtime.nexloop_evo_diagnoses set status='open',end_reason=null,closed_at=null")
    # 3. leakage_detected: one group on both sides.
    groups = admin.execute('select case_groups,design_case_ids[1],validation_case_ids[1] from runtime.nexloop_evo_candidates where candidate_id=%s', (c,)).fetchone()
    leaked = {**groups[0], groups[1]: groups[0][groups[2]]}
    bundle = admin.execute('select bundle from runtime.nexloop_evo_bundles where candidate_id=%s', (c,)).fetchone()[0]
    suspended(admin, 'update runtime.nexloop_evo_candidates set case_groups=%s where candidate_id=%s', (psycopg.types.json.Jsonb(leaked), c))
    suspended(admin, "update runtime.nexloop_evo_bundles set bundle=jsonb_set(bundle,'{case_groups}',%s),bundle_digest=encode(sha256(convert_to(jsonb_set(bundle,'{case_groups}',%s)::text,'UTF8')),'hex') where candidate_id=%s",
              (psycopg.types.json.Jsonb(leaked), psycopg.types.json.Jsonb(leaked), c))
    leak_digest = admin.execute('select bundle_digest from runtime.nexloop_evo_bundles where candidate_id=%s', (c,)).fetchone()[0]
    refused(f, 'leakage_detected', bundle=leak_digest)
    suspended(admin, 'update runtime.nexloop_evo_candidates set case_groups=%s where candidate_id=%s', (psycopg.types.json.Jsonb(groups[0]), c))
    suspended(admin, 'update runtime.nexloop_evo_bundles set bundle=%s,bundle_digest=%s where candidate_id=%s', (psycopg.types.json.Jsonb(bundle), f['bundle_digest'], c))
    # 5. dimension_closed: the open-dimension row is the audited switch.
    suspended(admin, "delete from control.nexloop_evo_open_dimensions where dimension='semantic_content'")
    refused(f, 'dimension_closed')
    admin.execute("insert into control.nexloop_evo_open_dimensions(dimension,opened_by,decision) values('semantic_content','synthetic','synthetic reopen')")
    # 6. dimension_stopped: the owner's stop for this tenant.
    admin.execute("""insert into control.nexloop_evo_dimension_stops(tenant_id,world,dimension,stopped,revision,changed_by) values(%s,'real','semantic_content',true,1,'owner')""", (TENANT_A,))
    refused(f, 'dimension_stopped')
    admin.execute("update control.nexloop_evo_dimension_stops set stopped=false,revision=2 where tenant_id=%s", (TENANT_A,))
    # 7. budget_exhausted: the candidate's cap is spent.
    admin.execute("""insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,source_ref,amount,unit,principal_id,recorded_at)
        values(%s,'real','evolution','synthetic-spend','evolution-candidate:'||%s,10,'CNY','synthetic',now())""", (TENANT_A, c))
    refused(f, 'budget_exhausted')
    assert status(f) == 'proposed'  # no refusal changed anything


def test_a_near_duplicate_across_the_sides_is_a_leak(evaluation):
    """§4.2: a design case and a validation case that asked exactly the same set of terms count as one cluster."""
    f = evaluation
    admin, c = f['admin'], f['candidate']
    d, v = admin.execute('select design_case_ids[1],validation_case_ids[1] from runtime.nexloop_evo_candidates where candidate_id=%s', (c,)).fetchone()
    for run in (d, v):  # E3 records of both Runs: the same single term (synthetic hash)
        admin.execute("""insert into runtime.nexloop_semantic_accesses(tenant_id,world,access_id,kind,run_id,operation,query_kind,term_hashes,coverage,ambiguity)
            values(%s,'real',%s,'access',%s,'browse','any',%s,0,false)""", (TENANT_A, uuid.uuid4(), run, [hexid('same-question')]))
    refused(f, 'leakage_detected')


def test_a_holdout_decides_at_most_five_candidates(evaluation):
    f = evaluation
    admin = f['admin']
    assert f['evaluator'].open(f['candidate'], f['bundle_digest'])['holdout_use'] == 1
    holdout = admin.execute('select holdout_digest from runtime.nexloop_evo_holdout_uses').fetchone()[0]
    # Four more candidates on the same validation set (synthetic copies of the first, each with its own frozen bundle).
    base = admin.execute('select * from runtime.nexloop_evo_candidates where candidate_id=%s', (f['candidate'],)).fetchone()
    cols = [d.name for d in admin.execute('select * from runtime.nexloop_evo_candidates limit 0').description]
    made = []
    for _ in range(5):
        new = str(uuid.uuid4())
        row = dict(zip(cols, base));row.update(candidate_id=new, status='proposed', end_reason=None)
        patch = str(uuid.uuid4());row['patch_id'] = patch
        suspended(admin, "insert into runtime.nexloop_evo_patches(tenant_id,world,patch_id,candidate_id,body,body_digest) select tenant_id,world,%s,%s,body,body_digest from runtime.nexloop_evo_patches where candidate_id=%s",
                  (patch, new, f['candidate']))
        suspended(admin, f"insert into runtime.nexloop_evo_candidates({','.join(cols)}) values({','.join(['%s'] * len(cols))})",
                  tuple(psycopg.types.json.Jsonb(row[k]) if k == 'case_groups' else row[k] for k in cols))
        frozen = f['store'].freeze_bundle(new, seed=SEED)
        made.append((new, frozen['bundle_digest']))
    for i, (cand, digest) in enumerate(made[:4]):
        assert f['evaluator'].open(cand, digest)['holdout_use'] == i + 2
    last, digest = made[4]
    out = f['evaluator'].open(last, digest)
    assert out == {'opened': False, 'candidate_id': last, 'reason': 'holdout_exhausted', 'status': 'proposed'}
    assert admin.execute('select count(*),max(use_number) from runtime.nexloop_evo_holdout_uses where holdout_digest=%s', (holdout,)).fetchone() == (5, 5)
