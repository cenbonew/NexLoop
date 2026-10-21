"""NX-029 / AT-026 erasure coverage, batch 1 (0305; docs/implementation/erasure-coverage.md): the real tables that held the Consumer's
own words. End to end on real PostgreSQL: a Consumer with rows in each table (seeded as their writers leave them), the owner's governed
erasure Action and the retention keeper until completed; then no row of those tables holds the original text or the original id, the
facts (kind, state, codes, times) stay, and outside an erasure the guards still refuse every change.
"""
import secrets
import uuid

import pytest
from psycopg.types.json import Jsonb

from commercial_fixture import TENANT
from test_erasure_pg import act, erasing, keeper, request  # noqa: F401
from test_erasure_pg import pytestmark  # noqa: F401  (execution_plan parametrization)
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

WORDS = ('call-13800000000', '别再给我发消息了', '退订营销短信', '顾客情绪激动要求人工', '人工接管：顾客投诉', '您好，新品红色款到货了', '顾客电话同意接收')
TABLES = ('control.nexloop_contact_refusal_hits', 'control.nexloop_reply_escalations', 'control.nexloop_takeovers', 'runtime.nexloop_outreach_sends',
          'runtime.nexloop_marketing_optouts', 'runtime.nexloop_marketing_consents')


def seed(admin, consumer, conversation=None, message=None):
    """One row per table naming the Consumer, with their words (guards bypassed for the seed only; the erasure runs with every trigger on).
    An outreach send still dispatching is seeded too: it is left for its state machine (0305 §2)."""
    conversation, message = conversation or secrets.token_hex(32), message or secrets.token_hex(16)
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into control.nexloop_contact_refusal_hits(tenant_id,world,message_id,consumer_id,conversation_id,rule_version,rule_id,certainty,matched_text,control_revision)
            values(%s,'real',%s,%s,%s,1,'refuse-1','refuse',%s,1)""", (TENANT, message, consumer, conversation, WORDS[0]))
        admin.execute("""insert into control.nexloop_reply_escalations(tenant_id,world,message_id,conversation_id,consumer_id,reason,detail)
            values(%s,'real',%s,%s,%s,'needs_human',%s)""", (TENANT, message, conversation, consumer, Jsonb({'note': WORDS[2]})))
        admin.execute("""insert into control.nexloop_takeovers(tenant_id,world,takeover_id,scope_kind,scope_ref,consumer_id,taken_by,start_intent,reason,started_at,expires_at,start_revision)
            values(%s,'real',%s,'consumer',%s,%s,'synthetic-staff','takeover-1',%s,clock_timestamp(),clock_timestamp()+interval '1 hour',1)""",
                      (TENANT, uuid.uuid4(), consumer, consumer, WORDS[3]))
        for state, intent in (('fulfilled', 'outreach:' + 'a' * 64), ('dispatching', 'outreach:' + 'b' * 64)):
            admin.execute("""insert into runtime.nexloop_outreach_sends(tenant_id,world,intent_id,consumer_id,purpose_id,purpose_version,topic,frequency_class,channel,origin_ref,
                conversation_id,body,body_digest,initiator,principal_id,request_intent,control_snapshot,state,admitted_at,finished_at)
                values(%s,'real',%s,%s,'restock',1,'restock','marketing','native.webchat','owner:synthetic',%s,%s,%s,'human','synthetic-owner','req-'||%s,'{}',%s,
                clock_timestamp(),case when %s='fulfilled' then clock_timestamp() end)""", (TENANT, intent, consumer, conversation, WORDS[4], 'c' * 64, state, state, state))
        admin.execute("""insert into runtime.nexloop_marketing_optouts(tenant_id,world,consumer_id,kind,source,message_id,rule_version,rule_id,matched_text,outreach_intent,recorded_by)
            values(%s,'real',%s,'unsubscribed','unsubscribe_rule',%s,1,'unsub-1',%s,'outreach:x','synthetic-rule')""", (TENANT, consumer, message, WORDS[1]))
        admin.execute("""insert into runtime.nexloop_marketing_consents(tenant_id,world,consumer_id,kind,source,evidence_ref,recorded_by,request_intent,reason)
            values(%s,'real',%s,'granted','offline','call-13800000000','synthetic-owner','consent-1',%s)""", (TENANT, consumer, WORDS[5]))
    return conversation, message


def holding(admin, needles):
    """Per table, the rows whose JSON form contains one of the needles."""
    out = {}
    for table in TABLES:
        n = admin.execute(f"select count(*) from {table} x where to_jsonb(x)::text like any(%s)", (['%' + w + '%' for w in needles],)).fetchone()[0]
        if n:
            out[table] = n
    return out


def run_erasure(c, admin, consumer):
    for _ in range(20):
        keeper(c, host=lambda run_id: 'purged').run_once()
        if request(admin, consumer)[1] == 'completed':
            return
    raise AssertionError(request(admin, consumer))


def linkable(admin, consumer):
    """L6 review 1.1/1.4: every id these tables keep (message, conversation, scope) joined to every table that maps such an id to a
    Consumer - the rows that still lead back to the original consumer id."""
    ids = set()
    for table, cols in (('control.nexloop_contact_refusal_hits', ('message_id', 'conversation_id')), ('control.nexloop_reply_escalations', ('message_id', 'conversation_id')),
                        ('control.nexloop_takeovers', ('scope_ref',)), ('runtime.nexloop_outreach_sends', ('conversation_id', 'intent_id')),
                        ('runtime.nexloop_marketing_optouts', ('message_id', 'outreach_intent')), ('runtime.nexloop_marketing_consents', ('message_id', 'evidence_ref'))):
        for col in cols:
            ids |= {v for (v,) in admin.execute(f"select {col} from {table} where tenant_id=%s and consumer_id like 'pseudonym:%%' and {col} is not null", (TENANT,)).fetchall()}
    mapping = admin.execute("""select c.oid::regclass::text, array_agg(a.attname::text) from pg_class c join pg_namespace n on n.oid=c.relnamespace
        join pg_attribute a on a.attrelid=c.oid and not a.attisdropped and a.attname in ('message_id','conversation_id','intent_id','scope_ref')
        where c.relkind='r' and n.nspname in ('runtime','control','authz','ontology')
          and exists(select 1 from pg_attribute b where b.attrelid=c.oid and b.attname='consumer_id' and not b.attisdropped) group by 1""").fetchall()
    hits = {}
    for table, cols in mapping:
        for col in cols:
            n = admin.execute(f'select count(*) from {table} where consumer_id=%s and {col}::text=any(%s)', (consumer, list(ids))).fetchone()[0]
            if n:
                hits[f'{table}.{col}'] = n
    # The messages carry no consumer id: through their conversation.
    n = admin.execute("""select count(*) from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations v using(tenant_id,world,conversation_id)
        where v.consumer_id=%s and (m.message_id=any(%s) or m.conversation_id=any(%s))""", (consumer, list(ids), list(ids))).fetchone()[0]
    if n:
        hits['runtime.nexloop_conversation_messages via conversations'] = n
    return hits


def test_the_consumer_erasure_reaches_every_table_that_held_their_words(erasing, admin):
    c = erasing
    consumer = c['consumer']
    inbound = c.inbound('你好，想问问新品')
    conversation, message = admin.execute('select conversation_id,message_id from runtime.nexloop_conversation_messages order by sequence desc limit 1').fetchone()
    seed(admin, consumer, conversation, message)
    assert linkable(admin, consumer) == {}  # nothing is pseudonymous yet: the join finds no pseudonymous row
    assert inbound
    assert set(holding(admin, WORDS)) == set(TABLES) and set(holding(admin, [consumer])) == set(TABLES)
    act(c['owner_actions'], 'erase_consumer', 'erase-cov-1', consumer_id=consumer, reason='顾客要求删除')
    run_erasure(c, admin, consumer)
    # Neither the words nor the id are left, except the outreach send still dispatching (its state machine owns it, 0305 §2).
    assert holding(admin, WORDS) == {'runtime.nexloop_outreach_sends': 1}
    assert holding(admin, [consumer]) == {'runtime.nexloop_outreach_sends': 1}
    assert admin.execute("select state from runtime.nexloop_outreach_sends where consumer_id=%s", (consumer,)).fetchall() == [('dispatching',)]
    # No kept id joins back to the original consumer id (L6 review 1.1): the conversation and the plans are pseudonymous too. The only
    # row left is the send still in flight (it keeps the original id until it settles, below).
    assert linkable(admin, consumer) == {'runtime.nexloop_outreach_sends.conversation_id': 1}
    assert admin.execute('select count(*) from runtime.nexloop_conversations where consumer_id=%s', (consumer,)).fetchone() == (0,)
    assert admin.execute('select count(*) from runtime.nexloop_plans where consumer_id=%s', (consumer,)).fetchone() == (0,)
    assert admin.execute("select count(*) from runtime.nexloop_conversations where conversation_id=%s and consumer_id like 'pseudonym:%%'", (conversation,)).fetchone() == (1,)
    # The customer's principal goes with it (the same pseudonym), and the plans' content (L5, merged into the WIP).
    assert admin.execute('select consumer_id=principal_id from runtime.nexloop_conversations where conversation_id=%s', (conversation,)).fetchone() == (True,)
    assert admin.execute("select count(*) from runtime.nexloop_plans where consumer_id like 'pseudonym:%%' and recipe<>'{\"erased\":true}'::jsonb").fetchone() == (0,)
    # The facts stay under one pseudonym: a refusal, an escalation code, a takeover, a sent outreach, an opt-out, a consent.
    pseudonyms = set()
    for table, facts in (('control.nexloop_contact_refusal_hits', "rule_id||'/'||certainty||'/'||matched_text"), ('control.nexloop_reply_escalations', "reason||'/'||detail::text"),
                         ('control.nexloop_takeovers', "scope_kind||'/'||(scope_ref=consumer_id)::text||'/'||reason"),
                         ('runtime.nexloop_marketing_optouts', "kind||'/'||source||'/'||coalesce(matched_text,'null')"),
                         ('runtime.nexloop_marketing_consents', "kind||'/'||(evidence_ref=consumer_id)::text||'/'||reason")):
        rows = admin.execute(f"select consumer_id,{facts} from {table} where tenant_id=%s and consumer_id like 'pseudonym:%%'", (TENANT,)).fetchall()
        assert len(rows) == 1, (table, rows)
        pseudonyms.add(rows[0][0])
        assert rows[0][1] in ('refuse-1/refuse/', 'needs_human/{"erased": true}', 'consumer/true/', 'unsubscribed/unsubscribe_rule/null', 'granted/true/'), (table, rows)
    sent = admin.execute("select consumer_id,state,body,body_digest from runtime.nexloop_outreach_sends where intent_id=%s", ('outreach:' + 'a' * 64,)).fetchone()
    pseudonyms.add(sent[0])
    pseudonyms.add(admin.execute('select consumer_id from runtime.nexloop_conversations where conversation_id=%s', (conversation,)).fetchone()[0])
    assert sent[1:] == ('fulfilled', '', None) and len(pseudonyms) == 1  # review 1.3: no digest left to confirm a guessed body
    tomb = admin.execute("select subject_key,action,counts from runtime.nexloop_erasure_tombstones where item_class='consumer_text'").fetchall()
    key = admin.execute('select runtime.nexloop_erasure_subject_key(%s,%s)', (TENANT, consumer)).fetchone()[0]  # 0308: keyed
    assert len(tomb) == 1 and tomb[0][:2] == (key, 'pseudonymized') and {k: v for k, v in tomb[0][2].items() if k not in ('plans',)} == {
        'refusal_hits': 1, 'reply_escalations': 1, 'takeovers': 1, 'outreach_sends': 1, 'marketing_optouts': 1, 'marketing_consents': 1, 'conversations': 1}
    # The send that was in flight is erased when it settles (its own pseudonym), and then nothing of the Consumer is left here.
    admin.execute("update runtime.nexloop_outreach_sends set state='fulfilled',finished_at=clock_timestamp(),updated_at=clock_timestamp() where intent_id=%s", ('outreach:' + 'b' * 64,))
    late = admin.execute("select consumer_id,body,body_digest from runtime.nexloop_outreach_sends where intent_id=%s", ('outreach:' + 'b' * 64,)).fetchone()
    assert late[0].startswith('pseudonym:') and late[0] not in pseudonyms and late[1:] == ('', None)
    assert holding(admin, WORDS) == {} and holding(admin, [consumer]) == {} and linkable(admin, consumer) == {}
    # The Consumer stays suppressed: the erasure's own contact restriction keeps the original id (0132).
    assert admin.execute("select active,rule_id from control.nexloop_contact_restrictions where consumer_id=%s", (consumer,)).fetchone() == (True, 'consumer_erasure')


@pytest.mark.parametrize('table,change', [
    ('control.nexloop_contact_refusal_hits', "matched_text=''"), ('control.nexloop_reply_escalations', "detail='{\"erased\":true}'"),
    ('control.nexloop_takeovers', "reason=''"), ('runtime.nexloop_outreach_sends', "body=''"),
    ('runtime.nexloop_marketing_optouts', 'matched_text=null'), ('runtime.nexloop_marketing_consents', "reason=''")])
def test_outside_an_erasure_the_guards_still_refuse(erasing, admin, table, change):
    """Only the erasure scope opens these tables: the same erasure values, written without it, are refused; so is a delete."""
    consumer = erasing['consumer']
    seed(admin, consumer)
    for sql in (f'update {table} set {change} where consumer_id=%s', f'delete from {table} where consumer_id=%s'):
        with pytest.raises(Exception, match='append-only|immutable|never deleted'), admin.transaction():
            admin.execute(sql, (consumer,))


def test_a_retention_hold_keeps_the_words_and_the_id(erasing, admin):
    """A Consumer retention hold keeps everything (0144): the erasure completes with the hold, these tables are untouched."""
    c = erasing
    consumer = c['consumer']
    seed(admin, consumer)
    act(c['owner_actions'], 'hold_retention', 'hold-cov', scope_kind='consumer', scope_ref=consumer, basis='争议处理中')
    act(c['owner_actions'], 'erase_consumer', 'erase-cov-2', consumer_id=consumer, reason='顾客要求删除')
    for _ in range(5):
        keeper(c, host=lambda run_id: 'purged').run_once()
    assert request(admin, consumer)[1] == 'completed_with_holds'
    assert set(holding(admin, WORDS)) == set(TABLES) and set(holding(admin, [consumer])) == set(TABLES)
    assert admin.execute("select count(*) from runtime.nexloop_erasure_tombstones where item_class='consumer_text'").fetchone() == (0,)
