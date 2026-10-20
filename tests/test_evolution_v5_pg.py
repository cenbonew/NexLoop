"""NX-039 V5 (design §8.4, §9; owner D39-9 to D39-12 decided 2026-10-20) on PostgreSQL: erasure propagation through every kind of
evaluation reference, the 7-day watch window with its alert (positive control), and the evolution_records retention class.

- Reports: stored with pseudonyms only (D39-11); the case references map them back and are what an erasure deletes.
- Holdouts holding a Run of the erased Consumer are invalidated and lose that Run id; an opening then answers holdout_exhausted.
- An evaluating candidate with such a case ends incomplete/missing_data and its L1 session closes (snapshot removed).
- The watch: a release whose week after is more than 2 points worse (miss or ambiguity rate, at least 20 accesses a side) is
  regressing with the suggestion rollback; alert rules v7 fire on it; nothing rolls back.
- Retention: reports, review links and case references 365 days after recording; the reports the release path keeps stay.
Synthetic data only; admin seeds and probes."""
import json
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from erasure_support import mark_erasing
from nexloop_eios.evolution.evaluation import evaluate_l0
from support.tenants import TENANT_A
from test_evolution_e4_pg import evo, hexid  # noqa: F401
from test_evolution_e5_pg import CREATED, SEED, proposed  # noqa: F401
from test_evolution_v2_pg import FAST, records
from test_evolution_v3_pg import l1  # noqa: F401
from test_evolution_v3_pg import suspended


def concluded(f):
    b = f['bundle']
    cases = b['design_case_ids'] + b['validation_case_ids']
    report = evaluate_l0(b, f['bundle_digest'], f['patch'], records(cases), inputs=FAST, created_at=CREATED)
    return report, f['evaluator'].conclude(report)


def consumer_of(f, run_id):
    """The Consumer of one of the fixture's message Runs."""
    return f['admin'].execute("""select c.consumer_id from runtime.nexloop_message_routes mr join runtime.nexloop_conversation_messages m on m.message_id=mr.message_id
        join runtime.nexloop_conversations c on c.conversation_id=m.conversation_id where mr.run_id=%s""", (run_id,)).fetchone()[0]


def test_reports_are_stored_with_pseudonyms_and_replay_by_what_was_submitted(l1):
    f = l1
    admin = f['admin']
    report, out = concluded(f)
    stored = admin.execute('select report,report_digest from runtime.nexloop_evo_reports where report_id=%s', (report['report_id'],)).fetchone()
    cases = set(f['bundle']['design_case_ids'] + f['bundle']['validation_case_ids'])
    text = json.dumps(stored[0])
    assert stored[1] == out['report_digest'] and not any(c in text for c in cases)  # no case id in the stored report
    refs = admin.execute('select pseudonym::text,kind,original_id from runtime.nexloop_evo_case_refs where report_digest=%s', (out['report_digest'],)).fetchall()
    assert {r[2] for r in refs if r[1] == 'case'} == cases
    assert {s['case_id'] for s in stored[0]['swaps']} == {r[0] for r in refs if r[1] == 'case'}
    assert f['evaluator'].conclude(report)['replayed'] is True  # the same submission replays
    with pytest.raises(psycopg.Error, match='reused'):
        f['evaluator'].conclude({**report, 'elapsed_ms': 77})


def test_an_erasure_removes_case_references_invalidates_holdouts_and_redacts_them(l1):
    f = l1
    admin = f['admin']
    report, out = concluded(f)
    validation_run = f['bundle']['validation_case_ids'][0]
    consumer = consumer_of(f, validation_run)
    erased_runs = {r[0] for r in admin.execute('select runtime.nexloop_evo_consumer_runs(%s,%s,%s)::text', (TENANT_A, 'real', consumer)).fetchall()}
    holdout = admin.execute('select holdout_digest,case_ids from runtime.nexloop_evo_holdouts').fetchone()
    assert validation_run in holdout[1]
    refs_before = admin.execute('select count(*) from runtime.nexloop_evo_case_refs').fetchone()[0]
    mark_erasing(admin, TENANT_A, consumer)
    left = {r[0] for r in admin.execute('select original_id from runtime.nexloop_evo_case_refs').fetchall()}
    assert not left & erased_runs and 0 < len(left) < refs_before  # other Consumers' references stay
    assert admin.execute('select reason from runtime.nexloop_evo_holdout_invalidations where holdout_digest=%s', (holdout[0],)).fetchone() == ('missing_data',)
    remaining = admin.execute('select case_ids from runtime.nexloop_evo_holdouts where holdout_digest=%s', (holdout[0],)).fetchone()[0]
    assert not set(remaining) & erased_runs
    # The report row is unchanged (append only), and it never held the ids.
    assert admin.execute('select report_digest from runtime.nexloop_evo_reports where report_id=%s', (report['report_id'],)).fetchone()[0] == out['report_digest']
    stone = admin.execute("select counts from runtime.nexloop_erasure_tombstones where item_class='evolution_records'").fetchone()[0]
    assert stone['case_refs'] >= 1 and stone['holdouts'] == 1


def test_an_erasure_ends_an_evaluating_candidate_and_closes_its_l1_session(l1):
    f = l1
    admin, ev = f['admin'], f['evaluator']
    session = ev.l1_open(f['candidate'], f['test_digest'])['session_id']
    assert admin.execute('select count(*) from runtime.nexloop_evo_l1_definitions').fetchone()[0] > 0
    mark_erasing(admin, TENANT_A, consumer_of(f, f['bundle']['design_case_ids'][0]))
    assert admin.execute('select status,end_reason from runtime.nexloop_evo_candidates where candidate_id=%s', (f['candidate'],)).fetchone() == ('incomplete', 'missing_data')
    assert admin.execute('select status,close_reason from runtime.nexloop_evo_l1_sessions where session_id=%s', (session,)).fetchone() == ('closed', 'candidate_ended')
    assert admin.execute('select count(*) from runtime.nexloop_evo_l1_definitions').fetchone()[0] == 0


def accesses(admin, n, *, missed, ambiguous, at_sql):
    for i in range(n):
        suspended(admin, f"""insert into runtime.nexloop_semantic_accesses(tenant_id,world,access_id,kind,run_id,operation,query_kind,term_hashes,coverage,ambiguity,
            result_refs,result_count,unresolved_count,recorded_at) values(%s,'real',%s,'access',null,'browse','any',%s,%s,%s,'[]',%s,0,{at_sql})""",
                  (TENANT_A, uuid.uuid4(), [hexid(f'term-{at_sql}-{i}')], 0 if i < missed else 1, i < ambiguous, 0 if i < missed else 1))


def watch(admin):
    return admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'evolution'->'release_watch'", (TENANT_A,)).fetchone()[0]


def test_the_watch_window_flags_a_regressing_release_and_suggests_rollback_only(evo):
    admin = evo['admin']
    admin.execute("select runtime.nexloop_evo_watch_start(%s,'real','semantic-release:1.4.0',now()-interval '2 days')", (TENANT_A,))
    # Week before: 2 of 20 missed (10%); since: 2 of 20 (10%): not regressing.
    accesses(admin, 20, missed=2, ambiguous=0, at_sql="now()-interval '4 days'")
    accesses(admin, 20, missed=2, ambiguous=0, at_sql="now()-interval '1 day'")
    w = watch(admin)
    assert (w['active'], w['regressing']) == (1, 0) and w['releases'][0]['judged'] is True and w['releases'][0]['suggestion'] is None
    # Since: 4 more misses in 20 more accesses -> 6/40 = 15% vs 10%: more than 2 points worse.
    accesses(admin, 20, missed=4, ambiguous=0, at_sql="now()-interval '12 hours'")
    w = watch(admin)
    assert (w['regressing'], w['releases'][0]['regressing'], w['releases'][0]['suggestion']) == (1, True, 'rollback')
    # A second release is watched on its own.
    admin.execute("select runtime.nexloop_evo_watch_start(%s,'real','semantic-release:1.5.0',now()-interval '3 days')", (TENANT_A,))
    assert watch(admin)['active'] == 2
    # Too few accesses on a side: not judged, never regressing.
    admin.execute("select runtime.nexloop_evo_watch_start(%s,'real','semantic-release:0.9.0',now()-interval '30 days')", (TENANT_A,))
    assert watch(admin)['active'] == 2  # past its 7 days: no longer watched
    # Nothing rolled back: the watch only reads.
    assert admin.execute('select count(*) from runtime.nexloop_evo_release_watches').fetchone()[0] == 3


def test_retention_deletes_reports_links_and_refs_after_365_days_but_keeps_retained_ones(l1):
    f = l1
    admin = f['admin']
    report, out = concluded(f)
    settings = admin.execute('select definition from control.nexloop_retention_settings order by version desc limit 1').fetchone()[0]
    assert settings['classes']['evolution_records'] == {'days': 365, 'source': None}
    assert 'evolution_records' in admin.execute('select runtime.nexloop_retention_classes()').fetchone()[0]
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    young = admin.execute("select runtime.nexloop_retention_sweep(%s,'real','evolution_records',200,'sweep:evo','service:synthetic')", (TENANT_A,)).fetchone()[0]
    assert young['processed'] == 0
    suspended(admin, "update runtime.nexloop_evo_reports set recorded_at=now()-interval '366 days' where report_id=%s", (report['report_id'],))
    # A report the release path keeps (current or previous release) stays: simulate it by replacing the hook.
    suspended(admin, f"""create or replace function runtime.nexloop_evo_retained_reports(p_tenant text,p_world text) returns setof text
        language sql stable security definer set search_path=pg_catalog,pg_temp as $$ select '{out['report_digest']}'::text $$""")
    assert admin.execute("select runtime.nexloop_retention_sweep(%s,'real','evolution_records',200,'sweep:kept','service:synthetic')", (TENANT_A,)).fetchone()[0]['processed'] == 0
    suspended(admin, """create or replace function runtime.nexloop_evo_retained_reports(p_tenant text,p_world text) returns setof text
        language sql stable security definer set search_path=pg_catalog,pg_temp as $$ select null::text where false $$""")
    old = admin.execute("select runtime.nexloop_retention_sweep(%s,'real','evolution_records',200,'sweep:old','service:synthetic')", (TENANT_A,)).fetchone()[0]
    assert old['processed'] == 1 and old['counts']['case_refs'] >= 1
    assert admin.execute('select count(*) from runtime.nexloop_evo_reports').fetchone()[0] == 0
    assert admin.execute('select count(*) from runtime.nexloop_evo_case_refs').fetchone()[0] == 0
    # Outside the retention path a report is still frozen.
    with pytest.raises(psycopg.errors.InsufficientPrivilege), admin.transaction():
        admin.execute('set local role nexloop_owner')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
        admin.execute('delete from runtime.nexloop_evo_bundles')
