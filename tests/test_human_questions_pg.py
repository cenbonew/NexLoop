"""K4b (migration human_questions; dsh intake v2 §3.3, Q-2, N3; K4 D1-D6): a plan reevaluation Run asks one person one question
(run-outcome 1.1), a workbench member answers it through the governed handler. Real PostgreSQL on the NX-024 outcome fixture: the
outcome-capable runtime worker records through the signed port; admin seeds the workbench members (trusted configuration) and probes.
The handler is called as the governed entry calls it (tenant, world, principal, subject, intent, body); the entry itself, its grants and
its idempotency are the governed entry's own tests. Synthetic data."""
import json
from pathlib import Path
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from test_plan_outcome_pg import outcome, outcomes, plan_events, reevaluation_run  # noqa: F401  (fixtures)
from test_plan_reevaluation_pg import planning, services, spec  # noqa: F401  (fixtures)
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
KINDS = json.loads((ROOT / 'deploy/configuration/human-question-kinds.v1.json').read_text())
QUESTION = {'audience': 'operator', 'question_kind': 'schedule_change', 'text': '客户希望改到周五下午，是否可以？', 'options': ['可以', '不可以'],
            'subject_ref': 'consumer:synthetic-k4b', 'withdrawable': True}
OWNER, OPERATOR, REVIEWER, STRANGER = 'principal-k4b-owner', 'principal-k4b-operator', 'principal-k4b-reviewer', 'principal-k4b-none'


def asking(question=None, kind='escalate'):
    return outcome(kind, schema_version='1.1', question=QUESTION if question is None else question)


def members(admin, tenant):
    """Trusted configuration: one owner, one operator and one reviewer of the tenant's workbench (seeded as admin)."""
    admin.execute("""insert into control.nexloop_workbench_configurations(tenant_id,manifest_version,roles_version,application_id,roles,members,
        roles_digest,recorded_by) values(%s,(select coalesce(max(manifest_version),0)+1 from control.nexloop_workbench_configurations where tenant_id=%s),
        1,'workbench-k4b','{}',%s,%s,'test:k4b')""",
        (tenant, tenant, Jsonb({OWNER: 'owner', OPERATOR: 'operator', REVIEWER: 'reviewer'}), 'a' * 64))


def answer(admin, tenant, principal, question_id, answer_kind='answer', world='real', **body):
    return admin.execute('select runtime.nexloop_governed_answer_human_question(%s,%s,%s,%s,%s,%s)',
                         (tenant, world, principal, 'subject-' + principal, 'intent:' + str(uuid.uuid4()),
                          Jsonb({'operation': 'answer_human_question', 'question_id': str(question_id), 'answer_kind': answer_kind,
                                 'option_index': None, 'text': None} | body))).fetchone()[0]


def question(admin, run_id):
    return admin.execute("""select question_id,state,revision,audience,question_kind,text,options,subject_ref,withdrawable,plan_version,
        due_by-created_at from runtime.nexloop_human_questions where run_id=%s""", (run_id,)).fetchone()


def wakes(admin, plan_id):
    row = admin.execute("select payload from runtime.nexloop_work_feed where feed='plan-reevaluate' and item_key=%s", ('plan:' + plan_id,)).fetchone()
    return [t['kind'] for t in row[0]['triggers']] if row else []


def bind_pack(admin, tenant, run_id, protocol='nexloop.context-pack.v7'):
    """The context pack bound to the Run (K4h K1: a question needs a v7 pack), seeded as admin: its artifact is not seeded."""
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into runtime.nexloop_context_packs(tenant_id,world,context_id,run_id,protocol,strategy_ref,pack_digest,artifact_id,budget_report,insufficient)
            values(%s,'real',%s,%s,%s,'context-strategy:plan@1',repeat('e',64),repeat('f',32),'{}','[]')""", (tenant, uuid.uuid4(), run_id, protocol))


@pytest.fixture
def asked(outcomes):  # noqa: F811
    f = outcomes
    members(f['admin'], f['plan']['tenant'])
    bind_pack(f['admin'], f['plan']['tenant'], f['run'].run_id)
    yield f


def test_the_trusted_kinds_are_the_file_and_1_0_still_records_without_a_question(asked):
    f = asked; admin = f['admin']
    assert admin.execute('select definition from control.nexloop_human_question_kinds where version=1').fetchone()[0] == KINDS
    with pytest.raises(psycopg.Error):
        admin.execute("update control.nexloop_human_question_kinds set published_by='x'")
    f['worker'].record_plan_outcome(activation_ref=f['ref'], command=f['command'], outcome=outcome('escalate'))
    assert admin.execute('select count(*) from runtime.nexloop_human_questions').fetchone()[0] == 0


def test_a_1_1_escalation_opens_one_question_for_seven_days(asked):
    f = asked; admin = f['admin']
    result = f['worker'].record_plan_outcome(activation_ref=f['ref'], command=f['command'], outcome=asking())
    assert result['kind'] == 'escalate'
    qid, state, revision, audience, kind, text, options, subject, withdrawable, version, lifetime = question(admin, f['run'].run_id)
    assert (state, revision, audience, kind, text, options, subject, withdrawable, version) == (
        'open', 1, 'operator', 'schedule_change', QUESTION['text'], QUESTION['options'], 'consumer:synthetic-k4b', True, 1)
    assert lifetime.total_seconds() == pytest.approx(KINDS['expire_after_seconds'], abs=5)
    # The same body replays (no second question); one Run asks at most one (D5): the outcome itself is one per Run.
    assert f['worker'].record_plan_outcome(activation_ref=f['ref'], command=f['command'], outcome=asking())['replay'] is True
    assert admin.execute('select count(*) from runtime.nexloop_human_questions where run_id=%s', (f['run'].run_id,)).fetchone()[0] == 1


@pytest.mark.parametrize('change', [
    {'schema_version': '1.0'}, {'kind': 'no_action'}, {'question': None},
    {'question': QUESTION | {'question_kind': 'refund'}}, {'question': QUESTION | {'audience': 'reviewer'}},
    {'question': QUESTION | {'text': ''}}, {'question': QUESTION | {'text': '问' * 301}},
    {'question': QUESTION | {'options': ['a', 'b', 'c', 'd', 'e']}}, {'question': QUESTION | {'options': ['a', 'a']}},
    {'question': QUESTION | {'options': ['x' * 61]}}, {'question': QUESTION | {'subject_ref': 'no reference'}},
    {'question': QUESTION | {'withdrawable': 'yes'}}, {'question': QUESTION | {'answer_roles': ['operator']}},
    {'question': {k: v for k, v in QUESTION.items() if k != 'withdrawable'}}])
def test_sql_refuses_what_the_contract_refuses(asked, change):
    """The SQL check is the contract's (exact keys and bounds); the Python guard refuses first, so the SQL is probed directly."""
    f = asked; admin = f['admin']
    body = asking() | change
    assert admin.execute('select runtime.nexloop_run_question_valid(%s)', (Jsonb(body),)).fetchone()[0] in (False, None)
    assert admin.execute('select runtime.nexloop_run_question_valid(%s)', (Jsonb(asking()),)).fetchone()[0] is True
    assert admin.execute('select runtime.nexloop_run_question_valid(%s)', (Jsonb(asking(kind='needs_information')),)).fetchone()[0] is True
    with pytest.raises((ValueError, psycopg.Error)):
        f['worker'].record_plan_outcome(activation_ref=f['ref'], command=f['command'], outcome=body)
    assert admin.execute('select count(*) from runtime.nexloop_plan_outcomes').fetchone()[0] == 0


def test_an_operator_answers_an_operational_question_and_the_plan_wakes(asked):
    f = asked; admin = f['admin']; tenant = f['plan']['tenant']; plan_id = f['spec']['plan_id']
    f['worker'].record_plan_outcome(activation_ref=f['ref'], command=f['command'], outcome=asking())
    qid = question(admin, f['run'].run_id)[0]
    admin.execute("delete from runtime.nexloop_work_feed where feed='plan-reevaluate'")
    assert answer(admin, tenant, OPERATOR, qid, option_index=0) == {'question_id': str(qid), 'state': 'answered', 'revision': 2}
    assert wakes(admin, plan_id) == ['question_answered']
    # A correction is a new revision; the latest one counts.
    assert answer(admin, tenant, OWNER, qid, option_index=1)['revision'] == 3
    rows = admin.execute('select revision,answer_kind,option_index,text,principal_id,role from runtime.nexloop_human_answers where question_id=%s order by revision',
                         (qid,)).fetchall()
    assert rows == [(2, 'option', 0, None, OPERATOR, 'operator'), (3, 'option', 1, None, OWNER, 'owner')]
    with pytest.raises(psycopg.Error):
        admin.execute('update runtime.nexloop_human_answers set option_index=0 where question_id=%s', (qid,))
    with pytest.raises(psycopg.Error):
        admin.execute('delete from runtime.nexloop_human_answers where question_id=%s', (qid,))


@pytest.mark.parametrize('kind', ['budget', 'contact_release', 'erasure', 'other'])
def test_owner_only_kinds_refuse_the_operator_whatever_the_run_declared(asked, kind):
    """Q-2: the Run's audience is never read; an owner-only kind is always withdrawable (D3)."""
    f = asked; admin = f['admin']; tenant = f['plan']['tenant']
    f['worker'].record_plan_outcome(activation_ref=f['ref'], command=f['command'],
                                    outcome=asking(QUESTION | {'question_kind': kind, 'audience': 'operator', 'withdrawable': False, 'options': []}))
    qid, state, *_, withdrawable, _version, _ = question(admin, f['run'].run_id)
    assert withdrawable is True
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='human question: question_role_forbidden'):
        answer(admin, tenant, OPERATOR, qid, text='可以')
    for principal in (REVIEWER, STRANGER):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='question_role_forbidden'):
            answer(admin, tenant, principal, qid, text='可以')
    assert question(admin, f['run'].run_id)[1:3] == ('open', 1)
    assert answer(admin, tenant, OWNER, qid, text='同意追加')['state'] == 'answered'


def test_answers_match_the_question_shape(asked):
    f = asked; admin = f['admin']; tenant = f['plan']['tenant']
    f['worker'].record_plan_outcome(activation_ref=f['ref'], command=f['command'], outcome=asking())
    qid = question(admin, f['run'].run_id)[0]
    for body in ({'option_index': 2}, {'text': '可以'}, {'option_index': 0, 'text': 'x'}, {}, {'option_index': '0'}):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='human question: (invalid_answer|invalid)'):
            answer(admin, tenant, OWNER, qid, **body)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='not_found'):
        answer(admin, tenant, OWNER, uuid.uuid4(), option_index=0)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='not_answered'):
        answer(admin, tenant, OWNER, qid, 'withdraw')


def test_withdraw_only_an_answered_withdrawable_question(asked):
    f = asked; admin = f['admin']; tenant = f['plan']['tenant']; plan_id = f['spec']['plan_id']
    f['worker'].record_plan_outcome(activation_ref=f['ref'], command=f['command'], outcome=asking(QUESTION | {'withdrawable': False}))
    qid = question(admin, f['run'].run_id)[0]
    answer(admin, tenant, OPERATOR, qid, option_index=0)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='not_withdrawable'):
        answer(admin, tenant, OPERATOR, qid, 'withdraw')
    # A withdrawable question: withdrawn, the plan wakes, and it can be answered again.
    run2, command2, ref2, _ = reevaluation_run(f['plan'], f['worker'], 'k4b-w')
    bind_pack(admin, tenant, run2.run_id)
    f['port']().link_run(plan_id=plan_id, version=1, run_id=run2.run_id, triggers=[{'kind': 'reassess_due', 'cause': 'system'}])
    f['worker'].record_plan_outcome(activation_ref=ref2, command=command2, outcome=asking())
    q2 = question(admin, run2.run_id)[0]
    answer(admin, tenant, OPERATOR, q2, option_index=0)
    admin.execute("delete from runtime.nexloop_work_feed where feed='plan-reevaluate'")
    assert answer(admin, tenant, OPERATOR, q2, 'withdraw')['state'] == 'withdrawn'
    assert wakes(admin, plan_id) == ['question_withdrawn']
    assert admin.execute('select answer_kind,option_index,text from runtime.nexloop_human_answers where question_id=%s order by revision desc limit 1',
                         (q2,)).fetchone() == ('withdraw', None, None)
    assert answer(admin, tenant, OPERATOR, q2, option_index=1)['state'] == 'answered'


def test_an_open_question_expires_once_through_the_retention_keeper_verb(asked):
    f = asked; admin = f['admin']; tenant = f['plan']['tenant']; plan_id = f['spec']['plan_id']
    f['worker'].record_plan_outcome(activation_ref=f['ref'], command=f['command'], outcome=asking())
    qid = question(admin, f['run'].run_id)[0]
    assert admin.execute("select runtime.nexloop_human_questions_expire(%s,'real',100)", (tenant,)).fetchone()[0] == {'expired': 0}
    admin.execute("update runtime.nexloop_human_questions set created_at=created_at-interval '8 days',due_by=due_by-interval '8 days' where question_id=%s", (qid,))
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='human question: expired'):
        answer(admin, tenant, OWNER, qid, option_index=0)
    admin.execute("delete from runtime.nexloop_work_feed where feed='plan-reevaluate'")
    assert admin.execute("select runtime.nexloop_human_questions_expire(%s,'real',100)", (tenant,)).fetchone()[0] == {'expired': 1}
    assert question(admin, f['run'].run_id)[1:3] == ('expired', 2) and wakes(admin, plan_id) == ['question_expired']
    assert admin.execute("select runtime.nexloop_human_questions_expire(%s,'real',100)", (tenant,)).fetchone()[0] == {'expired': 0}  # never again
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='not_open'):
        answer(admin, tenant, OWNER, qid, option_index=0)
    # The keeper reaches it through its signed port verb (the 0353 retention command, one more verb).
    body = admin.execute("select prosrc from pg_proc where proname='nexloop_retention_command'").fetchone()[0]
    assert "c->>'verb'='questions_expire'" in body and 'runtime.nexloop_human_questions_expire(t,p_world' in body


def test_a_new_plan_version_supersedes_an_open_question(asked):
    f = asked; admin = f['admin']; tenant = f['plan']['tenant']; plan_id = f['spec']['plan_id']
    f['worker'].record_plan_outcome(activation_ref=f['ref'], command=f['command'], outcome=asking())
    qid = question(admin, f['run'].run_id)[0]
    admin.execute("insert into runtime.nexloop_plan_events(tenant_id,world,plan_id,version,status,reason) values(%s,'real',%s,1,'closed','synthetic_close')",
                  (tenant, plan_id))
    assert question(admin, f['run'].run_id)[1:3] == ('superseded', 2)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='not_open'):
        answer(admin, tenant, OWNER, qid, option_index=0)


def test_the_consumer_erasure_of_the_outcome_blanks_the_question_and_answers(asked):
    f = asked; admin = f['admin']; tenant = f['plan']['tenant']
    f['worker'].record_plan_outcome(activation_ref=f['ref'], command=f['command'], outcome=asking(QUESTION | {'options': []}))
    qid = question(admin, f['run'].run_id)[0]
    answer(admin, tenant, OWNER, qid, text='客户说周五下午在家')
    with admin.transaction():
        admin.execute("select runtime.nexloop_erasure_open('runtime.nexloop_plan_outcomes',array['outcome'],false,'erasure:test-k4b')")
        admin.execute("""set local role nexloop_owner""")
        admin.execute("""select set_config('eios.tenant_id',%s,true)""", (tenant,))
        admin.execute("""update runtime.nexloop_plan_outcomes set outcome='{"erased":true}'::jsonb where run_id=%s""", (f['run'].run_id,))
        admin.execute('select runtime.nexloop_erasure_close()')
    assert question(admin, f['run'].run_id)[5:8] == ('', [], None)
    assert admin.execute('select text,principal_id from runtime.nexloop_human_answers where question_id=%s', (qid,)).fetchall() == [('', OWNER)]


def test_the_plan_closed_retention_deletes_questions_with_their_outcome(asked):
    f = asked; admin = f['admin']; tenant = f['plan']['tenant']
    f['worker'].record_plan_outcome(activation_ref=f['ref'], command=f['command'], outcome=asking())
    qid = question(admin, f['run'].run_id)[0]
    answer(admin, tenant, OWNER, qid, option_index=0)
    with pytest.raises(psycopg.Error):  # outside a registered retention or erasure transaction nothing is deleted
        admin.execute('delete from runtime.nexloop_human_answers where question_id=%s', (qid,))
    with admin.transaction():
        admin.execute("select runtime.nexloop_erasure_open('runtime.nexloop_plan_outcomes',null,true,'retention:test-k4b')")
        admin.execute('set local role nexloop_owner')
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        admin.execute('delete from runtime.nexloop_plan_outcomes where run_id=%s', (f['run'].run_id,))
        admin.execute('select runtime.nexloop_erasure_close()')
    assert admin.execute('select count(*) from runtime.nexloop_human_questions where question_id=%s', (qid,)).fetchone()[0] == 0
    assert admin.execute('select count(*) from runtime.nexloop_human_answers where question_id=%s', (qid,)).fetchone()[0] == 0


def test_the_governed_capability_and_no_application_role_reads_the_tables(asked):
    admin = asked['admin']
    assert admin.execute("select operation,subject_rule,handler::text from control.nexloop_governed_capabilities where capability='human_question.answer'").fetchone() == (
        'answer_human_question', 'human', 'runtime.nexloop_governed_answer_human_question(text,text,text,text,text,jsonb)')
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_runtime', 'nexloop_scheduler', 'nexloop_action_worker'):
        for table in ('runtime.nexloop_human_questions', 'runtime.nexloop_human_answers', 'control.nexloop_human_question_kinds'):
            assert admin.execute('select has_table_privilege(%s,%s,%s)', (role, table, 'select')).fetchone()[0] is False, (role, table)


def test_with_no_question_an_answer_is_not_found_and_writes_and_wakes_nothing(asked):
    """L6 (f656e6ce, condition 1): the answer Action is published inertly before v7; with no question it refuses not_found, writes
    neither table and wakes no plan - for the owner and the operator, an answer and a withdrawal alike."""
    f = asked; admin = f['admin']; tenant = f['plan']['tenant']
    admin.execute("delete from runtime.nexloop_work_feed where feed='plan-reevaluate'")
    for principal, kind, body in ((OWNER, 'answer', {'option_index': 0}), (OPERATOR, 'answer', {'text': '可以'}), (OWNER, 'withdraw', {})):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match='human question: not_found'):
            answer(admin, tenant, principal, uuid.uuid4(), kind, **body)
    assert admin.execute('select (select count(*) from runtime.nexloop_human_questions),(select count(*) from runtime.nexloop_human_answers)').fetchone() == (0, 0)
    assert admin.execute("select count(*) from runtime.nexloop_work_feed where feed='plan-reevaluate'").fetchone()[0] == 0


@pytest.mark.parametrize('packs', [['nexloop.context-pack.v6'], [], ['nexloop.context-pack.v7', 'nexloop.context-pack.v6']])
def test_a_question_needs_a_run_bound_to_context_pack_v7_only(outcomes, packs):  # noqa: F811
    """K4h K1: the outcome port refuses a 1.1 question from a Run not bound to v7 (v6, none, or mixed) and opens no question; the same
    Run records 1.1 without a question and 1.0 as before."""
    f = outcomes; admin = f['admin']; tenant = f['plan']['tenant']
    members(admin, tenant)
    for protocol in packs:
        bind_pack(admin, tenant, f['run'].run_id, protocol)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='run outcome invalid'):
        f['worker'].record_plan_outcome(activation_ref=f['ref'], command=f['command'], outcome=asking())
    assert admin.execute('select (select count(*) from runtime.nexloop_human_questions),(select count(*) from runtime.nexloop_plan_outcomes)').fetchone() == (0, 0)
    without = {k: v for k, v in asking().items() if k != 'question'}
    assert f['worker'].record_plan_outcome(activation_ref=f['ref'], command=f['command'], outcome=without)['kind'] == 'escalate'
    assert admin.execute('select count(*) from runtime.nexloop_human_questions').fetchone()[0] == 0
