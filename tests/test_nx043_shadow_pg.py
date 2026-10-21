"""NX-043 X3 (design §2, §4, §6, §8, §9, D7; NX-041 D7, S2, S4): the shadow world on PostgreSQL.

The owner switches a redacted copy of the tenant's real inbound events into a registered shadow:<uuid> world (default off); each copy
holds a content hash, the hour and the Claim types, never the text or an identifier. The simulation worker records a published
strategy's decision on a copied event; follow-up and outreach go through the S4 stand-in, nothing is sent, and the real effect path
refuses the shadow world. The frozen difference report is aggregate, suppresses cells under 30 distinct Consumers (NX-042 D3) and
leaves with the world (S2 whole deletion). Real PostgreSQL on a clean catalog; synthetic tenants, Consumers and messages.
"""
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from authority_fixture import seed_authority
from nexloop_eios.bootstrap import bootstrap
from support.tenants import TENANT_A, TENANT_B
from test_nx043_experiments_pg import publish, strategy

TABLES = ('control.nexloop_shadow_copy_events', 'runtime.nexloop_shadow_events', 'runtime.nexloop_shadow_event_claims', 'runtime.nexloop_shadow_decisions',
          'runtime.nexloop_shadow_reports')


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def governed(admin, fn, body, tenant=TENANT_A, world='real'):
    return admin.execute(f"select control.{fn}(%s,%s,'owner-principal','owner-subject',%s,%s)",
                         (tenant, world, 'intent-' + uuid.uuid4().hex[:12], Jsonb(body))).fetchone()[0]


def create_world(admin, kind='shadow', tenant=TENANT_A):
    return governed(admin, 'nexloop_governed_create_sim_world', {'operation': 'create_sim_world', 'request_id': uuid.uuid4().hex, 'kind': kind,
                                                                  'purpose': 'X3 shadow', 'expires_in_days': 7}, tenant)['world']


def switch(admin, world, enabled, tenant=TENANT_A, request=None, origin='real'):
    return governed(admin, 'nexloop_governed_set_shadow_copy', {'operation': 'set_shadow_copy', 'request_id': request or uuid.uuid4().hex, 'world': world,
                                                                 'enabled': enabled, 'reason': '影子实验（合成）'}, tenant, origin)


def inbound(admin, consumer, *, reply=False, tenant=TENANT_A, text='合成消息：明天上午还有空位吗？'):
    """A real conversation with one inbound message (and the agent's reply after it when reply)."""
    conv = 'cv-' + uuid.uuid4().hex
    msg = sha('m-' + uuid.uuid4().hex)  # message ids are 64 hex (0114)
    with admin.transaction():  # as the real intake writes it: under its tenant
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        admin.execute("insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key) values(%s,'real',%s,%s,'p',%s)",
                      (tenant, conv, consumer, conv))
        admin.execute("""insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
            values(%s,'real',%s,1,%s,'k1',%s,%s)""", (tenant, conv, msg, sha(text), Jsonb({'direction': 'inbound', 'text': text})))
    if reply:
        with admin.transaction():  # the outbound record as the delivery path leaves it (its own triggers are not under test here)
            admin.execute('set local session_replication_role=replica')
            admin.execute("""insert into runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence,message_id,idempotency_key,payload_digest,record)
                values(%s,'real',%s,2,%s,'k2',%s,%s)""", (tenant, conv, sha('r-' + msg), sha('reply'), Jsonb({'direction': 'outbound', 'text': '合成回复'})))
    return conv, msg


def claim(admin, conv, consumer, msg, kind='intent', tenant=TENANT_A):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        _claim(admin, conv, consumer, msg, kind, tenant)


def _claim(admin, conv, consumer, msg, kind, tenant):
    admin.execute("""insert into ontology.nexloop_claims(tenant_id,world,claim_id,conversation_id,consumer_id,first_input_digest,topic_key,subject_kind,subject_ref,subject_text,
        predicate,value,speaker,polarity,modality,condition_text,time_expression,valid_time,source_message_id,source_sequence,span_start,span_end,source_content_hash,quote,
        extractor_version,confidence,epistemic_kind,resolution_state,derived_from,correlation_key,guard_flags)
        values(%s,'real',%s,%s,%s,%s,'booking.time','consumer',%s,'顾客','wants',%s,'consumer','affirmed','asserted','','明天上午','{}',%s,1,0,4,%s,'合成引文','x-1',0.9,%s,
        'unresolved','[]',%s,'[]')""", (tenant, sha('claim' + msg), conv, consumer, sha('in' + msg), consumer, Jsonb({'type': 'text', 'value': '上午'}), msg, sha('q' + msg), kind,
                                          sha('corr' + msg)))


def key(world, msg):
    return sha(world + '|' + msg)


@pytest.fixture
def x3(admin, pg):
    bootstrap(admin)
    for t in (TENANT_A, TENANT_B):
        admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict do nothing", (t,))
    shadow, other = create_world(admin), create_world(admin)
    worker, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'a' * 32, world=shadow, identity_suffix='-x3-worker', sim_use='worker')
    real_token, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'c' * 32, world='real', identity_suffix='-x3-real')
    publish(admin, strategy('followup.later', follow_up_delay_hours=48), request='x3-candidate')
    role = lambda name: (lambda: psycopg.connect(make_conninfo(pg, user=name), autocommit=True))  # noqa: E731
    return dict(admin=admin, shadow=shadow, other=other, worker=worker, real_token=real_token, role=role)


def rows(admin, world=None):
    return {t: admin.execute(f'select count(*) from {t}' + (' where world=%s' if world else ''), ((world,) if world else ())).fetchone()[0] for t in TABLES}


def decide(f, event_key, decision, *, world=None, token=None, role='nexloop_sim_worker', strategy_version=1):
    with f['role'](role)() as db:
        return db.execute('select authz.nexloop_shadow_decision_record(%s,%s,%s)', (sha(token or f['worker']), world or f['shadow'], Jsonb(
            {'event_key': event_key, 'strategy_id': 'followup.later', 'strategy_version': strategy_version, 'decision': decision}))).fetchone()[0]


def test_the_switch_is_off_by_default_and_owner_only(x3):
    f = x3
    admin = f['admin']
    inbound(admin, sha('c-off'))
    assert rows(admin) == dict.fromkeys(TABLES, 0)  # D7: no switch event, no copy
    assert admin.execute('select runtime.nexloop_shadow_copy_target(%s)', (TENANT_A,)).fetchone()[0] is None
    for bad, code in (({'origin': 'test'}, 'owner_world_only'), ({'world': 'real'}, 'invalid'), ({'tenant': TENANT_B}, 'world_not_active')):
        with pytest.raises(psycopg.errors.InvalidParameterValue, match=code), admin.transaction():
            switch(admin, bad.get('world', f['shadow']), True, tenant=bad.get('tenant', TENANT_A), origin=bad.get('origin', 'real'))
    sim = create_world(admin, kind='simulation')
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='invalid'), admin.transaction():
        switch(admin, sim, True)  # a simulation world is no shadow world
    assert switch(admin, f['shadow'], True, request='on-1')['changed'] is True
    assert switch(admin, f['shadow'], True, request='on-1')['changed'] is False  # replay
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='request_reused'), admin.transaction():
        switch(admin, f['shadow'], False, request='on-1')
    assert admin.execute('select runtime.nexloop_shadow_copy_target(%s)', (TENANT_A,)).fetchone()[0] == f['shadow']
    switch(admin, f['shadow'], False)
    inbound(admin, sha('c-off-again'))
    assert rows(admin)['runtime.nexloop_shadow_events'] == 0  # off again: nothing copied
    assert admin.execute("select subject_rule from control.nexloop_governed_capabilities where capability='shadow_copy.set'").fetchone()[0] == 'human'
    from nexloop_eios.business_actions import PROFILES
    assert PROFILES['shadow_copy.set'] == 'human_owner'
    for r in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_sim_worker', 'nexloop_sim_api'):
        assert not admin.execute("select has_function_privilege(%s,'control.nexloop_governed_set_shadow_copy(text,text,text,text,text,jsonb)','execute')", (r,)).fetchone()[0]


def test_the_copy_holds_hashes_and_claim_types_never_the_text_or_an_identifier(x3):
    f = x3
    admin = f['admin']
    switch(admin, f['shadow'], True)
    consumer = sha('c-copy')
    conv, msg = inbound(admin, consumer, reply=True)
    claim(admin, conv, consumer, msg, 'intent')
    inbound(admin, sha('c-other-tenant'), tenant=TENANT_B)  # another tenant has no switch on
    events = admin.execute('select to_jsonb(e) from runtime.nexloop_shadow_events e').fetchall()
    assert len(events) == 1  # the inbound message only (the outbound reply and the other tenant are not copied)
    e = events[0][0]
    assert e['event_key'] == key(f['shadow'], msg) and e['world'] == f['shadow'] and (e['data_mode'], e['origin']) == ('simulation', 'shadow_copy')
    assert e['content_hash'] == sha(f['shadow'] + '|' + sha('合成消息：明天上午还有空位吗？'))  # not the real payload digest
    assert admin.execute('select claim_type from runtime.nexloop_shadow_event_claims').fetchall() == [('intent',)]
    dump = json.dumps([r[0] for t in TABLES for r in admin.execute(f'select to_jsonb(x) from {t} x')], ensure_ascii=False)
    for private in (consumer, conv, msg, '合成消息', sha('合成消息：明天上午还有空位吗？'), 'booking.time', '合成引文'):
        assert private not in dump, private


def test_shadow_decisions_are_recorded_and_never_sent(x3):
    f = x3
    admin = f['admin']
    switch(admin, f['shadow'], True)
    keys = [key(f['shadow'], inbound(admin, sha(f'c-dec-{i}'))[1]) for i in range(3)]
    before = {t: admin.execute(f'select count(*) from {t}').fetchone()[0] for t in ('runtime.nexloop_effect_intents', 'runtime.nexloop_outbound_messages')}
    reply = decide(f, keys[0], 'reply')
    assert (reply['recorded'], reply['sent'], reply['stand_in_intent']) == (True, False, None)
    out = decide(f, keys[1], 'outreach')
    assert out['recorded'] is True and out['sent'] is False and out['simulated'] is True and out['stand_in_intent']
    intent = admin.execute('select world,origin,data_mode from runtime.nexloop_sim_effect_intents where intent_id=%s', (out['stand_in_intent'],)).fetchone()
    assert intent == (f['shadow'], 'replay', 'simulation')  # the S4 stand-in: a simulated receipt, nothing is sent
    assert decide(f, keys[1], 'outreach')['recorded'] is False  # replay
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='decision_reused'):
        decide(f, keys[1], 'none')
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='event_unknown'):
        decide(f, 'e' * 64, 'none')
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='strategy_not_published'):
        decide(f, keys[2], 'none', strategy_version=9)
    # Real and test, another world, other roles: refused.
    for world, token in (('real', f['real_token']), ('test', f['real_token'])):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            decide(f, keys[2], 'none', world=world, token=token)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        decide(f, keys[2], 'none', world=f['other'])  # the shadow credential in another world
    for role in ('nexloop_sim_api', 'nexloop_domain_worker', 'nexloop_api', 'nexloop_twin_port'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            decide(f, keys[2], 'none', role=role)
    # The real effect path refuses the shadow world: the real outbound entry, and the effect tables' CHECK (NX-041).
    with f['role']('nexloop_sim_worker')() as db, pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('select authz.nexloop_outbound_message_command(%s,%s,%s,%s,%s)', (sha(f['worker']), f['shadow'], '{}', '', '{}'))
    with pytest.raises(psycopg.errors.CheckViolation, match='nx041_real_effect_world'), admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
            values(%s,%s,'tenant','*',true,'real',1,'shadow tries the real path','shadow',%s)""", (TENANT_A, f['shadow'], 'shadow-real-path'))
    after = {t: admin.execute(f'select count(*) from {t}').fetchone()[0] for t in before}
    assert after == before  # nothing reached the real effect tables


def test_the_difference_report_is_aggregate_suppressed_under_30_and_frozen(x3):
    f = x3
    admin = f['admin']
    switch(admin, f['shadow'], True)
    ids = []
    for i in range(40):  # real replied to 32 Consumers' messages and not to 8; the candidate would reply to all
        consumer = sha(f'c-rep-{i}')
        conv, msg = inbound(admin, consumer, reply=i < 32)
        ids += [consumer, conv, msg]
        decide(f, key(f['shadow'], msg), 'reply')
    since, until = datetime.now(UTC) - timedelta(hours=2), datetime.now(UTC) + timedelta(hours=1)
    report = admin.execute('select runtime.nexloop_shadow_report_freeze(%s,%s,%s,%s,%s,%s,%s)',
                           (TENANT_A, f['shadow'], 'followup.later', 1, since, until, 'owner-principal')).fetchone()[0]
    assert report['cells'] == [{'shadow': 'reply', 'real': 'reply', 'events': 32, 'consumers': 32}]
    assert report['suppressed_cells'] == 1  # (reply, none): 8 Consumers < 30, not output
    assert report['total'] == {'events': 40, 'consumers': 40, 'agreement_rate': 0.8} and report['minimum_consumers'] == 30
    assert report['data_mode'] == 'simulation' and 'not a causal estimate' in report['conclusion_scope'] and report['frozen'] is True
    text = json.dumps(report, ensure_ascii=False)
    assert not any(x in text for x in ids)  # no Consumer, conversation or message id
    again = admin.execute('select runtime.nexloop_shadow_report_freeze(%s,%s,%s,%s,%s,%s,%s)',
                          (TENANT_A, f['shadow'], 'followup.later', 1, since, until, 'owner-principal')).fetchone()[0]
    assert again['report_digest'] == report['report_digest']  # the frozen report answers
    with pytest.raises(psycopg.errors.InsufficientPrivilege), admin.transaction():
        admin.execute("update runtime.nexloop_shadow_reports set report='{}'")
    # Under the minimum in total: nothing per cell, no total.
    small = create_world(admin)
    switch(admin, small, True)
    small_worker, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'b' * 32, world=small, identity_suffix='-x3-small', sim_use='worker')
    for i in range(5):
        decide(f, key(small, inbound(admin, sha(f'c-small-{i}'), reply=True)[1]), 'reply', world=small, token=small_worker)
    tiny = admin.execute('select runtime.nexloop_shadow_report_freeze(%s,%s,%s,%s,%s,%s,%s)',
                         (TENANT_A, small, 'followup.later', 1, since, until, 'owner-principal')).fetchone()[0]
    assert (tiny['cells'], tiny['suppressed_cells'], tiny['total']) == ([], 1, None)
    for r in ('nexloop_api', 'nexloop_metrics', 'nexloop_sim_worker'):
        assert not admin.execute("select has_function_privilege(%s,'runtime.nexloop_shadow_report_freeze(text,text,text,integer,timestamptz,timestamptz,text)','execute')", (r,)).fetchone()[0]


def test_deleting_the_shadow_world_leaves_nothing_and_stops_the_copy(x3):
    f = x3
    admin = f['admin']
    switch(admin, f['shadow'], True)
    conv, msg = inbound(admin, sha('c-del'), reply=True)
    claim(admin, conv, sha('c-del'), msg)
    decide(f, key(f['shadow'], msg), 'outreach')
    admin.execute('select runtime.nexloop_shadow_report_freeze(%s,%s,%s,%s,%s,%s,%s)', (TENANT_A, f['shadow'], 'followup.later', 1,
                  datetime.now(UTC) - timedelta(hours=1), datetime.now(UTC) + timedelta(hours=1), 'owner-principal'))
    assert all(rows(admin, f['shadow']).values())
    result = governed(admin, 'nexloop_governed_delete_sim_world', {'operation': 'delete_sim_world', 'request_id': uuid.uuid4().hex, 'world': f['shadow'],
                                                                   'reason': '影子实验结束'})
    for _ in range(10):
        if result['status'] == 'deleted':
            break
        result = admin.execute('select runtime.nexloop_sim_world_purge(%s,%s,1000)', (TENANT_A, f['shadow'])).fetchone()[0]
    assert result['status'] == 'deleted' and result['blocked'] == {}, result
    assert rows(admin, f['shadow']) == dict.fromkeys(TABLES, 0)
    assert admin.execute('select count(*) from runtime.nexloop_sim_effect_intents where world=%s', (f['shadow'],)).fetchone()[0] == 0
    # The switch pointed at the deleted world: no copy any more.
    assert admin.execute('select runtime.nexloop_shadow_copy_target(%s)', (TENANT_A,)).fetchone()[0] is None
    inbound(admin, sha('c-after'))
    assert rows(admin)['runtime.nexloop_shadow_events'] == 0
