"""K4d (migration workbench_pending; contract workbench-pending v1): GET /api/v1/workbench/pending through the real workbench (owner,
operator and reviewer log in). Real PostgreSQL; admin seeds synthetic waiting items (replica mode where their sources - plan outcomes,
effect intents, approval policies - are not seeded) and probes. The read writes nothing and filters by the member's own role, never by a
question's audience; the reviewer sees its candidate category, not a 403."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import secrets
import uuid

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from psycopg.types.json import Jsonb

from test_browser_http import ORIGIN
from test_workbench_read_pg import (ROLES, TENANT, add_human, browser_business, configure, conversations, identity, members_file,  # noqa: F401
                                    published_action, uow, workbench, workbench_login, write_facts)
from nexloop_eios.workbench_roles import compile_members

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = Draft202012Validator(json.loads((ROOT / 'packages/contracts/workbench-pending.schema.json').read_text()), format_checker=FormatChecker())
CATEGORIES = ['question', 'approval', 'remediation', 'reply_escalation', 'takeover_expiry', 'candidate_review']


@pytest.fixture
def members(workbench):  # noqa: F811
    w = workbench
    w['reviewer'] = add_human(w['admin'], 'reviewer')
    roster = ((w['owner'], 'owner'), (w['operator'], 'operator'), (w['reviewer'], 'reviewer'))
    configure(w, *roster)
    write_facts(w['admin'], compile_members(ROLES, members_file(*roster)))
    yield w


def pending(w, who, query=''):
    with w['client']() as client:
        assert workbench_login(client, w[who]).status_code == 200
        response = client.get('/api/v1/workbench/pending' + query, headers={'Origin': ORIGIN})
    if response.status_code == 200:
        SCHEMA.validate(response.json())
    return response


def counts(body):
    return {c['category']: c['count'] for c in body['categories']}


def replica(admin, sql, args):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute(sql, args)


def question(admin, kind='schedule_change', audience='operator', ago=0, state='open', text='客户希望改到周五下午，是否可以？'):
    qid = uuid.uuid4()
    replica(admin, """insert into runtime.nexloop_human_questions(tenant_id,world,question_id,run_id,plan_id,plan_version,audience,question_kind,text,
        options,subject_ref,withdrawable,kinds_version,state,created_at,due_by) values(%s,'real',%s,%s,%s,1,%s,%s,%s,'["可以","不可以"]','consumer:synthetic-k4d',
        true,1,%s,clock_timestamp()-make_interval(secs=>%s),clock_timestamp()+interval '7 days')""",
        (TENANT, qid, uuid.uuid4(), uuid.uuid4(), audience, kind, text, state, ago))
    return qid


def seed_others(admin, w):
    replica(admin, """insert into control.nexloop_approval_requests(tenant_id,world,capability,action_resource,policy_version,request_digest,request_id,body,
        requester_subject,requester_principal,requested_at,due_by) values(%s,'real','goals.budget.loosen','eios:action:nexloop.budget.loosen:1',1,%s,'k4d-approval','{}',
        'subject-other','principal-other',clock_timestamp()-interval '2 hours',clock_timestamp()+interval '1 day')""", (TENANT, secrets.token_hex(32)))
    replica(admin, """insert into control.nexloop_approval_requests(tenant_id,world,capability,action_resource,policy_version,request_digest,request_id,body,
        requester_subject,requester_principal,due_by) values(%s,'real','goals.budget.loosen','eios:action:nexloop.budget.loosen:1',1,%s,'k4d-own','{}',%s,%s,
        clock_timestamp()+interval '1 day')""", (TENANT, secrets.token_hex(32), w['owner']['subject_id'], w['owner']['principal_id']))  # the owner's own: not theirs to decide
    replica(admin, """insert into runtime.nexloop_remediation_intents(tenant_id,world,source_intent_id,source_action,source_action_version,failure_kind,failure_id,
        rule_id,rule_version,rule_digest,kind,mode,state,due_by) values(%s,'real',%s,'synthetic.send',1,'origin_expired',%s,'synthetic_rule',1,%s,'escalate','manual',
        'escalated',clock_timestamp()+interval '7 days')""", (TENANT, uuid.uuid4(), uuid.uuid4(), 'c' * 64))
    replica(admin, """insert into control.nexloop_reply_escalations(tenant_id,world,message_id,conversation_id,consumer_id,reason,detail,escalated_at)
        values(%s,'real',%s,%s,%s,'fallback_failed','{}',clock_timestamp()-interval '3 hours')""", (TENANT, 'a' * 64, 'b' * 64, 'd' * 64))
    for who, ends in (('owner', '30 minutes'), ('operator', '20 minutes'), ('operator', '5 hours')):
        replica(admin, """insert into control.nexloop_takeovers(tenant_id,world,takeover_id,scope_kind,scope_ref,consumer_id,taken_by,start_intent,reason,started_at,
            expires_at,start_revision) values(%s,'real',%s,'conversation',%s,%s,%s,%s,'人工接管',clock_timestamp()-interval '1 hour',clock_timestamp()+%s::interval,1)""",
            (TENANT, uuid.uuid4(), secrets.token_hex(32), 'e' * 64, w[who]['principal_id'], 'k4d-' + secrets.token_hex(4), ends))
    replica(admin, """insert into ontology.nexloop_candidate_definitions(tenant_id,world,candidate_id,kind,dedupe_key,status,candidate,dependent_claims,
        merge_scores,config_version) values(%s,'real',%s,'alias',%s,'pending_review','{}',array['claim-k4d'],'{}','synthetic-1')""",
        (TENANT, str(uuid.uuid4()), secrets.token_hex(32)))


def test_empty_is_all_clear_for_every_role(members):
    w = members
    for who in ('owner', 'operator', 'reviewer'):
        r = pending(w, who)
        assert r.status_code == 200, (who, r.text)
        body = r.json()
        assert body['all_clear'] is True and body['questions'] == [] and [c['category'] for c in body['categories']] == CATEGORIES
        assert all(c == {'category': c['category'], 'count': 0, 'oldest_waiting_seconds': None, 'oldest_ref': None} for c in body['categories'])


def test_each_role_sees_what_it_may_act_on(members):
    w = members; admin = w['admin']
    seed_others(admin, w)
    q_op = question(admin, 'schedule_change', ago=600)
    q_owner = question(admin, 'budget', audience='operator', ago=300)  # the Run said operator; the kind is the owner's
    question(admin, 'customer_fact', state='answered')
    owner, operator, reviewer = (pending(w, who).json() for who in ('owner', 'operator', 'reviewer'))
    assert counts(owner) == {'question': 2, 'approval': 1, 'remediation': 1, 'reply_escalation': 1, 'takeover_expiry': 2, 'candidate_review': 1}
    assert counts(operator) == {'question': 1, 'approval': 0, 'remediation': 0, 'reply_escalation': 1, 'takeover_expiry': 1, 'candidate_review': 0}
    assert counts(reviewer) == {'question': 0, 'approval': 0, 'remediation': 0, 'reply_escalation': 0, 'takeover_expiry': 0, 'candidate_review': 1}
    assert owner['all_clear'] is False and reviewer['all_clear'] is False
    assert [q['question_id'] for q in operator['questions']] == [str(q_op)]
    assert {q['question_id'] for q in owner['questions']} == {str(q_op), str(q_owner)}
    first = {c['category']: c for c in owner['categories']}['question']
    assert first['oldest_ref'] == f'question:{q_op}' and 590 <= first['oldest_waiting_seconds'] <= 700
    assert owner['questions'][0]['answer'] == {'operation': 'nexloop.human_question.answer:1'}


def test_audience_orders_and_text_is_cut_to_120(members):
    w = members; admin = w['admin']
    older = question(admin, 'schedule_change', audience='owner', ago=900, text='问' * 300)
    newer = question(admin, 'schedule_change', audience='operator', ago=100)
    listed = pending(w, 'operator').json()['questions']
    assert [q['question_id'] for q in listed] == [str(newer), str(older)]  # the operator's own audience first, then by age
    assert len(listed[1]['text']) == 120
    assert [q['question_id'] for q in pending(w, 'owner').json()['questions']] == [str(older), str(newer)]


def test_questions_page_by_after_and_limit(members):
    w = members; admin = w['admin']
    ids = [question(admin, ago=1000 - i) for i in range(25)]
    first = pending(w, 'operator').json()
    assert counts(first)['question'] == 25 and [q['question_id'] for q in first['questions']] == [str(x) for x in ids[:20]]
    second = pending(w, 'operator', f'?after={ids[19]}').json()
    assert [q['question_id'] for q in second['questions']] == [str(x) for x in ids[20:]]
    assert [q['question_id'] for q in pending(w, 'operator', f'?after={ids[2]}&limit=2').json()['questions']] == [str(ids[3]), str(ids[4])]
    for bad in ('?limit=21', '?limit=0', '?after=nope', f'?after={uuid.uuid4()}', '?other=1'):
        assert pending(w, 'operator', bad).status_code == 422, bad


def test_the_read_writes_nothing_and_outsiders_are_refused(members):
    w = members; admin = w['admin']
    seed_others(admin, w)
    question(admin)
    tables = [t for (t,) in admin.execute("""select c.oid::regclass::text from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where c.relkind='r' and n.nspname in ('runtime','control','ontology','authz')""").fetchall()]
    def snapshot():
        return {t: admin.execute(f'select count(*) from {t}').fetchone()[0] for t in tables}
    from contextlib import ExitStack
    with ExitStack() as stack:
        clients = []
        for who in ('owner', 'operator', 'reviewer'):  # log in first: logging in writes sessions and evidence, the read must not
            client = stack.enter_context(w['client']())
            assert workbench_login(client, w[who]).status_code == 200
            clients.append(client)
        before = snapshot()
        for client in clients:
            assert client.get('/api/v1/workbench/pending', headers={'Origin': ORIGIN}).status_code == 200
        after = snapshot()
    assert {t for t in tables if before[t] != after[t]} == set()
    assert admin.execute("select provolatile from pg_proc where proname='nexloop_workbench_pending'").fetchone()[0] == 's'
    with w['client']() as client:
        assert workbench_login(client, w['customer']).status_code == 200
        assert client.get('/api/v1/workbench/pending', headers={'Origin': ORIGIN}).status_code == 403
    with w['client']() as client:
        assert client.get('/api/v1/workbench/pending', headers={'Origin': ORIGIN}).status_code in (401, 403)
