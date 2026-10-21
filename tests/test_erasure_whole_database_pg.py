"""NX-029 / AT-026 erasure, the last line (dispatcher 2026-10-21, L6 review of erasure batch 3): a Consumer walks the real paths -
inbound messages and replies, Claims, a plan, a commitment, a payment event, a cost, an effect that ends unknown and is reconciled,
a Run and its job - the owner erases them, the retention keeper completes it; then EVERY text, varchar, json and jsonb column of every
table of the database is searched for the original consumer id and for the Consumer's principal ids. Nothing may hold them except the
rows listed below that keep the id on purpose, each with its reason. A table added later that keeps either id fails here.
Real PostgreSQL on a clean catalog; synthetic tenant and Consumer.
"""
import hashlib
import json

import pytest

from commercial_fixture import TENANT
from test_erasure_pg import act, active_plan, erasing, fault, fresh, keeper, request  # noqa: F401
from test_erasure_pg import pytestmark  # noqa: F401  (execution_plan parametrization)
from commitment_fixture import commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

# (table, column) -> (why the original id stays after a completed erasure, the rows allowed: a SQL condition on alias x, or None for
# every row). Nothing else may hold the Consumer's id or principal.
_SUPPRESSION = 'the erasure itself: the permanent suppression and its proof keep the original id so the erased Consumer is never contacted again (0132)'
_RESTORE = 'NX-035: after a restore the deletion ledger replays this erasure, so it names the subject it must delete again'
KEPT_ON_PURPOSE = {
    ('control.nexloop_consumer_erasures', 'consumer_id'): ('the erasure request: runtime.nexloop_consumer_erasing reads it to refuse any contact and any release for good', None),
    ('control.nexloop_contact_restrictions', 'consumer_id'): (_SUPPRESSION, "x.rule_id='consumer_erasure'"),
    ('control.nexloop_control_events', 'scope_ref'): (_SUPPRESSION + ' (the restriction\'s revision event)', "x.event_kind='contact_restricted' and x.detail->>'rule_id'='consumer_erasure'"),
    ('control.nexloop_deletion_ledger', 'subject_ref'): (_RESTORE, "x.kind='consumer'"),
    ('control.nexloop_restore_journal', 'entry'): (_RESTORE + ' (the journal of the ledger and of the suppression)', "x.kind in ('deletion','contact_restriction')"),
    # Owner 2026-10-21: deterministic ids made before the keyed algorithm (an unkeyed hash of the Consumer id) are an accepted residue.
    ('runtime.nexloop_outreach_sends', 'intent_id'): ('负责人 2026-10-21 决定: a legacy (unkeyed) outreach intent id from before 0308 is an accepted residue',
                                                      "x.origin_ref='owner:legacy-residue'"),
    ('runtime.nexloop_work_feed', 'item_key'): ('transient: the recall index deletion of the erased Consumer; the recall worker deletes the item once done',
                                                "x.feed='recall-instance' and x.payload->>'op'='delete' and x.payload->>'object_id'='{k}'"),
    ('runtime.nexloop_work_feed', 'payload'): ('transient: the recall index deletion of the erased Consumer; the recall worker deletes the item once done',
                                               "x.feed='recall-instance' and x.payload->>'op'='delete' and x.payload->>'object_id'='{k}'"),
}

# Every column of a type that can hold an id as text is searched; every other column must be of a type that cannot (asserted).
SEARCHED = r'^(tsvector|text|character varying|character|json|jsonb|uuid|name|citext|xml|hstore|bytea|text\[\]|character varying\[\]|jsonb\[\]|uuid\[\])'
CANNOT_HOLD = r'^(integer|bigint|smallint|numeric|boolean|real|double precision|date|interval|inet|oid|regclass|regproc|money|pg_lsn|"char"|bit|timestamp|time|extensions\.vector|vector)'
COLUMNS = """select n.nspname, c.relname, a.attname, format_type(a.atttypid,a.atttypmod) from pg_class c join pg_namespace n on n.oid=c.relnamespace
    join pg_attribute a on a.attrelid=c.oid and a.attnum>0 and not a.attisdropped
    where c.relkind in ('r','p','m') and c.relpersistence<>'t' and n.nspname not in ('pg_catalog','information_schema','pg_toast')
    order by 1,2,3"""


def search(admin, needles, allowed=None, consumer=''):
    """(schema.table, column) -> rows holding any needle, over the whole database (superuser read); rows a KEPT_ON_PURPOSE condition
    allows are not counted. Every column is either searched or of a type that cannot hold an id (asserted)."""
    hits = {}
    pattern = ['%' + n + '%' for n in needles]
    import re
    for schema, table, column, kind in admin.execute(COLUMNS).fetchall():
        if not re.match(SEARCHED, kind):
            assert re.match(CANNOT_HOLD, kind), (schema, table, column, kind)
            continue
        key = (f'{schema}.{table}', column)
        keep = (allowed or {}).get(key)
        if keep and keep[1] is None:
            continue
        extra = f' and not ({keep[1].format(k=consumer)})' if keep else ''
        text = f"encode(x.\"{column}\",'escape')" if kind == 'bytea' else f'x."{column}"::text'
        n = admin.execute(f'select count(*) from "{schema}"."{table}" x where {text} like any(%s){extra}', (pattern,)).fetchone()[0]
        if n:
            hits[key] = n
    return hits


def walk(c, admin):
    """The Consumer's real paths, as their writers leave them; returns (needles, a bystander whose ids contain the Consumer's)."""
    import uuid
    from psycopg.types.json import Jsonb
    from test_erasure_coverage_pg import seed as seed_words
    env, consumer = c['env'], c['consumer']
    active_plan(c)
    fresh(c)  # a commitment
    c.inbound('我的手机号是 13800000000，请删除我的资料')
    reply = c.outbound('好的，已为您登记')
    c.claim(reply, '已为您登记')
    env.post(env.event('payment.succeeded', 'pay-1'))
    env.tick()
    conversation, message = admin.execute("""select m.conversation_id,m.message_id from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations v
        using(tenant_id,world,conversation_id) where v.consumer_id=%s order by m.sequence desc limit 1""", (consumer,)).fetchone()
    seed_words(admin, consumer, conversation, message)  # a takeover, outreach sends (one still dispatching), opt-out, consent, refusal, escalation
    principal0 = admin.execute('select principal_id from runtime.nexloop_conversations where consumer_id=%s limit 1', (consumer,)).fetchone()[0]
    # A Run's job and invocation as the router leaves them (run_command names the Consumer; the actor is their principal) - L6 review 2.
    # And a bystander whose consumer ref and principal CONTAIN the Consumer's (L6 review 2.1): never touched.
    for who, ref, actor in (('own', 'consumer:' + consumer, principal0), ('bystander', 'consumer:' + consumer + 'ab', principal0 + '-x')):
        invocation = str(uuid.uuid4())
        fault(admin, """insert into runtime.invocations(invocation_id,tenant_id,capability_name,capability_version,actor_id,trace_id,request_id,correlation_id,
            idempotency_key,input_digest,status,created_at,updated_at) values(%s,%s,'NexLoop.event','1.0.0',%s,'t','r','c',%s,%s,'succeeded',now(),now())""",
              (invocation, TENANT, actor, f'inv-{who}-' + consumer[:8], 'e' * 64))
        fault(admin, """insert into runtime.jobs(job_id,invocation_id,capability_name,capability_version,capability_type,execution_mode,tenant_id,actor_id,trace_id,request_id,
            idempotency_key,normalized_input,plan_snapshot,status,created_at,updated_at,world,queue,available_at,attempts,max_attempts,fencing_token)
            values(%s,%s,'NexLoop.event','1.0.0','event','async',%s,%s,'t','r',%s,%s,'{}','succeeded',now(),now(),'real','runs',now(),1,3,1)""",
              (str(uuid.uuid4()), invocation, TENANT, actor, f'job-{who}-' + consumer[:8],
               Jsonb({'input': {'erased': True}, 'run_command': {'consumer_ref': ref, 'run_id': str(uuid.uuid4())}})))
    # Consumer pauses in both forms (L6 review 2.2) with their control events, and a cohort metric observation.
    for revision, ref in ((9998, consumer), (9999, 'consumer:' + consumer)):
        fault(admin, """insert into control.nexloop_control_scopes(tenant_id,world,scope_kind,scope_ref,paused,revision,reason) values(%s,'real','consumer',%s,true,%s,'暂停此顾客')""",
              (TENANT, ref, revision))
        fault(admin, """insert into control.nexloop_control_events(tenant_id,world,revision,event_kind,scope_kind,scope_ref,detail,principal_id,intent_id)
            values(%s,'real',%s,'pause','consumer',%s,'{}','synthetic-owner',%s)""", (TENANT, revision, ref, f'pause-{revision}'))
    fault(admin, """insert into control.nexloop_metric_observations(tenant_id,world,metric_id,metric_version,observation_id,numerator,occurred_at,matures_at,data_mode,
        source_ref,evidence_refs,recorded_by,subject_ref) values(%s,'real','repeat-rate',1,'obs-1',1,now(),now(),'real','record:x','[]','synthetic',%s)""", (TENANT, consumer))
    # Owner 2026-10-21: a send accepted now has a keyed intent id; one from before the change keeps its legacy id (accepted residue).
    from test_erasure_keyed_ids_pg import ORIGIN, accept, legacy_id, today
    accept(admin, consumer)
    day = today(admin)
    legacy_residue = 'outreach:' + hashlib.sha256(json.dumps([TENANT, consumer, 'service_followup', 'owner:legacy-residue', day], ensure_ascii=False).encode()).hexdigest()
    admin.execute("""select runtime.nexloop_outreach_accept(%s,'real',%s,'service_followup','owner:legacy-residue','旧算法的一条',null,'human','synthetic-owner','intent-legacy',null)""",
                  (TENANT, consumer))
    fault(admin, "update runtime.nexloop_outreach_sends set intent_id=%s where origin_ref='owner:legacy-residue'", (legacy_residue,))
    # The Consumer's principals from every place that maps them (L6 review 3.2): conversations, ownership, web events.
    principals = sorted({p for (p,) in admin.execute("""select principal_id from runtime.nexloop_conversations where consumer_id=%s
        union select principal_id from control.nexloop_consumer_owners where consumer_id=%s
        union select principal_id from runtime.nexloop_native_web_events where consumer_id=%s""", (consumer, consumer, consumer)).fetchall()})
    assert principals, 'the Consumer has a principal'
    # The unkeyed hash of the keyed send's inputs: whoever holds the id could compute it; it must not be found after the erasure.
    return [consumer, *principals, legacy_id(consumer, day), legacy_residue], principal0


def complete(c, admin, consumer, intent=None):
    host = lambda run_id: 'purged'  # noqa: E731
    for _ in range(12):
        keeper(c, host=host).run_once()
        if intent and request(admin, consumer)[1] == 'waiting_unknown_effects':
            fault(admin, "update runtime.nexloop_effect_intents set state='failed' where intent_id=%s", (intent,))  # reconciled
        if request(admin, consumer)[1] in ('completed', 'completed_with_holds'):
            return request(admin, consumer)[1]
    raise AssertionError(request(admin, consumer))


def assert_nothing_left(admin, consumer, needles, principal0):
    # The outreach send that was in flight settles now; it is erased when it does (batch 2).
    admin.execute("update runtime.nexloop_outreach_sends set state='fulfilled',finished_at=clock_timestamp(),updated_at=clock_timestamp() where state='dispatching'")
    # The bystander (a test construct: another person whose ids contain the Consumer's) holds the substring by design.
    bystander = 'a different person whose ids contain the Consumer\'s as a substring - checked untouched below'
    allowed = {**KEPT_ON_PURPOSE,
               ('runtime.jobs', 'normalized_input'): (bystander, "x.idempotency_key like 'job-bystander-%%'"),
               ('runtime.jobs', 'actor_id'): (bystander, "x.idempotency_key like 'job-bystander-%%'"),
               ('runtime.invocations', 'actor_id'): (bystander, "x.idempotency_key like 'inv-bystander-%%'")}
    after = search(admin, needles, allowed, consumer)
    assert after == {}, '\n'.join(f'{t}.{col}: {n}' for (t, col), n in sorted(after.items()))
    assert all(reason for reason, _ in KEPT_ON_PURPOSE.values())
    own = admin.execute("select normalized_input->'run_command'->>'consumer_ref',actor_id from runtime.jobs where idempotency_key=%s", ('job-own-' + consumer[:8],)).fetchone()
    assert own[0].startswith('consumer:pseudonym:') and own[1].startswith('pseudonym:')
    # The bystander whose ids contain the Consumer's is untouched (L6 review 2.1).
    other = admin.execute("""select j.normalized_input->'run_command'->>'consumer_ref',j.actor_id,i.actor_id from runtime.jobs j join runtime.invocations i
        on i.tenant_id=j.tenant_id and i.invocation_id=j.invocation_id where j.idempotency_key=%s""", ('job-bystander-' + consumer[:8],)).fetchone()
    assert other == ('consumer:' + consumer + 'ab', principal0 + '-x', principal0 + '-x')
    assert admin.execute("select count(*) from ontology.objects where type_name='CommercialRecord' and properties->>'consumer_ref' is not null").fetchone() == (0,)


def test_after_a_completed_erasure_no_column_of_the_database_holds_the_consumer(erasing, admin):
    c = erasing
    consumer = c['consumer']
    needles, principal0 = walk(c, admin)
    intent = c.submit({'service': '后台退款'})['intent_id']
    fault(admin, "update runtime.nexloop_effect_intents set state='unknown' where intent_id=%s", (intent,))
    before = search(admin, needles, None, consumer)
    assert len(before) >= 15, before  # positive control: the id is in many tables before
    for key in (('runtime.nexloop_conversation_messages', 'record'), ('runtime.jobs', 'normalized_input'), ('runtime.invocations', 'actor_id'),
                ('ontology.objects', 'properties'), ('control.nexloop_control_scopes', 'scope_ref'), ('control.nexloop_metric_observations', 'subject_ref'),
                ('control.nexloop_takeovers', 'consumer_id'), ('runtime.nexloop_outreach_sends', 'consumer_id'), ('runtime.nexloop_commitments', 'consumer_id')):
        assert key in before, key
    act(c['owner_actions'], 'erase_consumer', 'erase-all', consumer_id=consumer, reason='顾客要求删除')
    assert complete(c, admin, consumer, intent) == 'completed'
    assert_nothing_left(admin, consumer, needles, principal0)


def test_an_erasure_held_back_by_a_hold_completes_when_the_hold_is_released(erasing, admin):
    """L6 review 2.3: a Consumer hold keeps everything (completed_with_holds); releasing the last hold resumes the erasure, which then
    completes as any erasure does (every phase and batches 1-4)."""
    c = erasing
    consumer = c['consumer']
    needles, principal0 = walk(c, admin)
    hold = act(c['owner_actions'], 'hold_retention', 'hold-all', scope_kind='consumer', scope_ref=consumer, basis='争议处理中')
    act(c['owner_actions'], 'erase_consumer', 'erase-held', consumer_id=consumer, reason='顾客要求删除')
    assert complete(c, admin, consumer) == 'completed_with_holds'
    assert len(search(admin, needles, KEPT_ON_PURPOSE, consumer)) >= 15  # the hold kept everything
    act(c['owner_actions'], 'release_retention_hold', 'hold-all-release', hold_id=hold['hold_id'], reason='争议结束')
    assert request(admin, consumer)[1] == 'purging'
    assert complete(c, admin, consumer) == 'completed'
    assert_nothing_left(admin, consumer, needles, principal0)
