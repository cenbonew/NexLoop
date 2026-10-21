"""NX-043 X4 wiring (dispatcher 2026-10-21): runtime.nexloop_experiment_strategy_for reaches the decision paths.

A started online experiment (world real) is constructed here: start still refuses online (online_not_available) until the owner
opens it, so the started event is seeded as start will write it then. A Consumer allocated to the candidate group gets the candidate
strategy - its persona version in the Context section (source unchanged: context-pack v6 is published), its outreach_purposes lever in the dispatch gates, its follow-up delay; every
other Consumer, and an erasing one, one excluded by in_same_comparison, a retired strategy or a stopped experiment, gets the current
policy. A real inbound message allocates on first sight; an accepted outreach send records the strategy version in audit. Nothing
opens real dispatch. Real PostgreSQL on a clean catalog; synthetic tenant and synthetic consumer ids only.
"""
import hashlib
import uuid

import pytest
from psycopg.types.json import Jsonb

from support.tenants import TENANT_A
from test_nx043_allocation_pg import assign, consumer, strategy_for
from test_nx043_experiments_pg import (call, db, publish, ready, register, registration, retire, stop, strategy,  # noqa: F401
                                       persona_document)

EXPERIMENT = 'wiring.online'
LEVERS = {'follow_up_delay_hours': 48, 'persona': {'persona_id': 'xiaohe', 'version': 1}, 'outreach_purposes': {'service_followup': False}}


def persona(admin, version):
    call(admin, 'nexloop_governed_publish_operator_persona', {'operation': 'publish_operator_persona', 'request_id': f'pp{version}',
                                                              'persona_id': 'xiaohe', 'version': version, 'status': 'published',
                                                              'document': persona_document(), 'reason': '发布'})


def seed_start(admin, experiment):
    admin.execute("""insert into control.nexloop_experiment_events(tenant_id,experiment_id,status,recorded_by,request_id)
        values(%s,%s,'started','owner-principal',%s)""", (TENANT_A, experiment, 'seeded-' + experiment))


def levers(admin, c, world='real'):
    return admin.execute('select runtime.nexloop_experiment_levers(%s,%s,%s)', (TENANT_A, world, c)).fetchone()[0]


def section(admin, c):
    return admin.execute("select runtime.nexloop_operator_persona_section(%s,'real',%s)", (TENANT_A, c)).fetchone()[0]


def gates(admin, c, purpose):
    return admin.execute("select runtime.nexloop_outreach_gates(%s,'real',%s,%s,clock_timestamp())", (TENANT_A, c, purpose)).fetchone()[0]


def gate(items, name):
    return next((g for g in items if g['gate'] == name), None)


def switches(admin):
    return admin.execute("""select md5(coalesce(string_agg(t::text,'|' order by t::text),'')) from (
        select * from control.nexloop_real_dispatch_switches) t""").fetchone()[0]


@pytest.fixture
def online(ready, db):
    persona(db, 1)
    persona(db, 2)
    publish(db, strategy('wiring.later', **LEVERS), request='s-wiring')
    register(db, registration('real', None, experiment_id=EXPERIMENT, stage='online', candidate={'strategy_id': 'wiring.later', 'version': 1}))
    seed_start(db, EXPERIMENT)
    decided = {n: strategy_for(db, 'real', consumer(f'w{n}')) for n in range(15)}
    arms = {'candidate': [consumer(f'w{n}') for n, d in decided.items() if d and d['arm'] == 'candidate'],
            'control': [consumer(f'w{n}') for n, d in decided.items() if d and d['arm'] == 'control']}
    assert arms['candidate'] and arms['control']
    return arms


def test_only_a_candidate_group_consumer_gets_the_candidate_levers(online, db):
    for c in online['candidate']:
        assert levers(db, c) == {'experiment_id': EXPERIMENT, 'strategy_id': 'wiring.later', 'version': 1, 'levers': LEVERS}
        assert levers(db, c, world='sim') is None  # another world: the current policy
    assert all(levers(db, c) is None for c in online['control'])
    assert levers(db, consumer('never-seen')) is None
    assert db.execute('select count(*) from control.nexloop_experiment_allocations where consumer_id=%s', (consumer('never-seen'),)).fetchone()[0] == 0


def default_persona(admin):
    admin.execute("""insert into control.nexloop_operator_persona_rules(tenant_id,version,default_persona_id,rules,published_by,request_intent,reason)
        values(%s,1,'xiaohe','[]','owner-principal','intent-rules','默认')""", (TENANT_A,))


def test_without_a_current_persona_the_lever_adds_none(online, db):
    assert section(db, consumer('never-seen')) is None
    assert all(section(db, c) is None for c in online['candidate'])  # no persona section appears only because of an experiment


def test_the_context_persona_is_the_strategy_version_for_the_candidate_group_only(online, db):
    default_persona(db)
    current = section(db, consumer('never-seen'))
    assert (current['persona_ref'], current['source']) == ('operator-persona:xiaohe@2', 'default')
    for c in online['candidate']:
        s = section(db, c)
        # context-pack v6 is published (decision B): the version is the strategy's, the source value stays the current one.
        assert s == {**current, 'persona_ref': 'operator-persona:xiaohe@1', 'version': 1, 'document': s['document']}
    for c in online['control']:
        assert section(db, c) == current
    # A retired persona version is no longer spoken: back to the current persona.
    db.execute("""insert into control.nexloop_operator_persona_status_events(tenant_id,persona_id,version,status,set_by,request_intent,reason)
        values(%s,'xiaohe',1,'retired','owner-principal','intent-retire','作废')""", (TENANT_A,))
    assert all(section(db, c) == current for c in online['candidate'])


def test_the_purpose_lever_switches_a_service_purpose_for_the_candidate_group_only(online, db):
    for c in online['candidate']:
        g = gates(db, c, 'service_followup')
        assert gate(g, 'purpose') == {'gate': 'purpose', 'result': 'refuse', 'detail': 'purpose_disabled'}
        assert gate(g, 'experiment_strategy') == {'gate': 'experiment_strategy', 'result': 'pass', 'detail': 'strategy:wiring.later@1'}
        assert gate(gates(db, c, 'commitment_followup'), 'experiment_strategy') is None  # a purpose the lever does not name
        assert gate(gates(db, c, 'marketing_general'), 'experiment_strategy') is None  # never a marketing purpose
    for c in online['control']:
        g = gates(db, c, 'service_followup')
        assert gate(g, 'purpose')['result'] == 'pass' and gate(g, 'experiment_strategy') is None


def test_the_purpose_lever_never_switches_on_what_the_tenant_switched_off(ready, db):
    """L6 review 3.1 (dispatcher 2026-10-21): an experiment only switches a purpose off; the tenant's off stays off."""
    publish(db, strategy('wiring.on', outreach_purposes={'service_followup': True}), request='s-on')
    register(db, registration('real', None, experiment_id='wiring.on', stage='online', candidate={'strategy_id': 'wiring.on', 'version': 1}))
    seed_start(db, 'wiring.on')
    picked = [consumer(f'on{n}') for n in range(15) if (strategy_for(db, 'real', consumer(f'on{n}')) or {}).get('arm') == 'candidate']
    assert picked
    db.execute("""insert into control.nexloop_outreach_purpose_states(tenant_id,purpose_id,enabled,revision,updated_by,request_intent)
        values(%s,'service_followup',false,1,'owner-principal','intent-off')""", (TENANT_A,))
    for c in picked:
        g = gates(db, c, 'service_followup')
        assert gate(g, 'purpose') == {'gate': 'purpose', 'result': 'refuse', 'detail': 'purpose_disabled'}


def test_erasing_excluded_retired_and_stopped_fall_back_to_the_current_policy(online, db):
    default_persona(db)
    current = section(db, consumer('never-seen'))
    erasing = online['candidate'][0]
    db.execute("""insert into control.nexloop_consumer_erasures(tenant_id,world,request_id,consumer_id,source,requested_by,reason,state,watermark)
        values(%s,'real','erase-wiring',%s,'owner','owner-principal','擦除','blocking',0)""", (TENANT_A, erasing))
    assert levers(db, erasing) is None and section(db, erasing) == current
    assert gate(gates(db, erasing, 'service_followup'), 'experiment_strategy') is None
    # A retired candidate strategy: everyone gets the current policy.
    retire(db, 'wiring.later', 1)
    assert all(levers(db, c) is None and section(db, c) == current for c in online['candidate'])


def test_a_stopped_experiment_and_an_in_same_comparison_exclusion_fall_back(online, db):
    stop(db, EXPERIMENT)
    assert all(levers(db, c) is None for c in online['candidate'])
    register(db, registration('real', None, experiment_id='wiring.again', stage='online', candidate={'strategy_id': 'wiring.later', 'version': 1}))
    seed_start(db, 'wiring.again')
    again = assign(db, online['candidate'][0], experiment='wiring.again')
    assert again['arm'] is None and again['excluded_reason'] is not None  # in_same_comparison: no arm
    assert levers(db, online['candidate'][0]) is None


def conversation(admin, c):
    cv = hashlib.sha256(c.encode()).hexdigest()
    admin.execute("insert into runtime.nexloop_conversations values(%s,'real',%s,%s,'consumer-principal',%s,0)", (TENANT_A, cv, c, 'k-' + cv[:12]))
    return cv


def message(admin, cv, seq, record):
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    admin.execute('insert into runtime.nexloop_conversation_messages values(%s,%s,%s,%s,%s,%s,%s,%s)',
                  (TENANT_A, 'real', cv, seq, hashlib.sha256(uuid.uuid4().bytes).hexdigest(), f'm-{seq}', 'd' * 64, Jsonb(record)))


def allocated(admin, c):
    return admin.execute('select count(*) from control.nexloop_experiment_allocations where consumer_id=%s', (c,)).fetchone()[0]


def test_a_real_inbound_message_allocates_on_first_sight(ready, db):
    quiet = consumer('before-any-experiment')
    message(db, conversation(db, quiet), 1, {'body': '你好', 'status': 'accepted'})
    assert allocated(db, quiet) == 0  # no started online experiment: nothing runs
    persona(db, 1)
    persona(db, 2)
    publish(db, strategy('wiring.later', **LEVERS), request='s-wiring')
    register(db, registration('real', None, experiment_id=EXPERIMENT, stage='online', candidate={'strategy_id': 'wiring.later', 'version': 1}))
    seed_start(db, EXPERIMENT)
    c = consumer('inbound')
    cv = conversation(db, c)
    message(db, cv, 1, {'body': '在吗', 'status': 'accepted'})
    assert allocated(db, c) == 1
    message(db, cv, 2, {'body': '回复', 'status': 'accepted', 'direction': 'outbound'})  # our own words allocate nobody new
    assert allocated(db, c) == 1
    other = consumer('outbound-only')
    message(db, conversation(db, other), 1, {'body': '回复', 'status': 'accepted', 'direction': 'outbound'})
    assert allocated(db, other) == 0
    # A failing allocation never fails the real message: it is only counted.
    db.execute('alter table control.nexloop_experiment_allocations add constraint wiring_fail check(false) not valid')
    broken = consumer('broken')
    message(db, conversation(db, broken), 1, {'body': '在吗', 'status': 'accepted'})
    assert allocated(db, broken) == 0
    assert db.execute("""select count(*) from runtime.nexloop_experiment_accounting_failures
        where consumption_id='allocation:inbound'""").fetchone()[0] == 1


def send(admin, c, decided=None):
    intent = 'outreach:' + hashlib.sha256(c.encode()).hexdigest()
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    admin.execute("""insert into runtime.nexloop_outreach_sends(tenant_id,world,intent_id,consumer_id,purpose_id,purpose_version,topic,frequency_class,
        channel,origin_ref,body,body_digest,initiator,principal_id,request_intent,control_snapshot,experiment_strategy)
        values(%s,'real',%s,%s,'commitment_followup',2,'service.commitment','service','native.webchat','owner:wiring','跟进一下',%s,'human',
        'owner-principal','intent-wiring','{}',%s)""", (TENANT_A, intent, c, 'e' * 64, Jsonb(decided) if decided else None))
    return intent


def test_an_outreach_send_records_the_strategy_version_and_dispatch_switches_are_untouched(online, db):
    before = switches(db)
    # The decision's strategy (as runtime.nexloop_outreach_accept freezes it); a retirement before the insert changes nothing.
    decided = {c: levers(db, c) for c in online['candidate'][:1] + online['control'][:1]}
    retire(db, 'wiring.later', 1)
    for c, d in decided.items():
        send(db, c, d and {k: v for k, v in d.items() if k != 'levers'})
    rows = db.execute("""select payload from runtime.audit_events where action='nexloop.experiment.strategy_applied'""").fetchall()
    assert [r[0] for r in rows] == [{'experiment_id': EXPERIMENT, 'strategy_id': 'wiring.later', 'version': 1, 'decision': 'outreach_send',
                                     'intent_id': 'outreach:' + hashlib.sha256(online['candidate'][0].encode()).hexdigest(),
                                     'purpose': 'commitment_followup'}]
    assert switches(db) == before
    # A failed append never fails the send; it is counted.
    db.execute('alter table runtime.audit_events add constraint wiring_fail check(false) not valid')
    send(db, online['candidate'][1] if len(online['candidate']) > 1 else consumer('late'), {'experiment_id': EXPERIMENT, 'strategy_id': 'wiring.later', 'version': 1})
    assert db.execute("""select count(*) from runtime.nexloop_experiment_accounting_failures
        where consumption_id='evidence:outreach_send'""").fetchone()[0] == 1
    # The new functions are the owner's; no application role may call them directly.
    for fn in ('nexloop_experiment_levers(text,text,text)', 'nexloop_experiment_online_running(text)'):
        assert db.execute("""select coalesce(array_agg(grantee::regrole::text order by 1) filter (where grantee<>'nexloop_owner'::regrole),'{}')
            from aclexplode((select proacl from pg_proc where oid=%s::regprocedure))""", ('runtime.' + fn,)).fetchone()[0] == []
