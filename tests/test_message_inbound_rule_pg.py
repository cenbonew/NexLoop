"""One inbound rule (stage X7 S5, release 37: the shadow copy copied 0 of 6 real inbound WebChat messages).

runtime.nexloop_message_is_inbound decides for the shadow copy (0288) and the inbound allocation (0309) alike. The inbound messages
here are written by the production path - the Consumer's own governed channel entry (ConversationMessagePort.accept_message, 0046 /
0138 commit_message) - never a hand-made record; the person's reply in a takeover is written by its governed Action (0127). Synthetic
data only.
"""
from datetime import datetime, timedelta, timezone

import pytest

from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from support.tenants import TENANT_A
from test_browser_business_authorization import browser_business  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_action_definitions import published_action  # noqa: F401
from test_conversation_messages import conversations  # noqa: F401
from test_nx043_experiments_pg import publish, register, registration, strategy
from test_nx043_shadow_pg import create_world, switch


def shadow_events(admin, world):
    return admin.execute('select count(*) from runtime.nexloop_shadow_events where world=%s', (world,)).fetchone()[0]


def allocations(admin, consumer):
    return admin.execute('select count(*) from control.nexloop_experiment_allocations where consumer_id=%s', (consumer,)).fetchone()[0]


def online(admin, experiment):
    publish(admin, strategy('inbound.rule', follow_up_delay_hours=48), request='s-' + experiment)
    if not admin.execute("select count(*) from control.nexloop_strategy_versions where strategy_id='followup.default'").fetchone()[0]:
        publish(admin, strategy('followup.default', follow_up_delay_hours=24), request='s-control-' + experiment)
    register(admin, registration('real', None, experiment_id=experiment, stage='online', candidate={'strategy_id': 'inbound.rule', 'version': 1}))
    admin.execute("""insert into control.nexloop_experiment_events(tenant_id,experiment_id,status,recorded_by,request_id)
        values(%s,%s,'started','owner-principal',%s)""", (TENANT_A, experiment, 'seeded-' + experiment))


def test_the_rule_on_every_record_shape_the_writers_produce(admin):
    from nexloop_eios.bootstrap import bootstrap
    bootstrap(admin)
    rule = lambda record: admin.execute('select runtime.nexloop_message_is_inbound(%s::jsonb)', (record,)).fetchone()[0]  # noqa: E731
    consumer = '{"id":"m","conversation_id":"c","sequence":1,"actor":"p","body":"在吗","accepted_at":"t","status":"accepted"}'  # 0046/0138
    agent = '{"id":"m","conversation_id":"c","sequence":2,"actor":"a","body":"好的","direction":"outbound","sender_kind":"agent"}'  # 0079
    staff = '{"id":"m","conversation_id":"c","sequence":3,"actor":"s","body":"人工","status":"accepted","direction":"outbound","sender_kind":"human_takeover"}'
    assert (rule(consumer), rule(agent), rule(staff)) == (True, False, False)
    assert rule('{"actor":"p","body":"x","direction":"inbound"}') is True
    assert (rule('{}'), rule('{"body":"x"}'), rule('[]'), rule('{"actor":"p","body":"x","direction":"other"}')) == (False, False, False, False)
    assert rule(None) is False  # never null: the triggers test not(...)


def test_a_consumer_message_through_the_channel_is_copied_and_allocated(conversations, admin):
    f = conversations
    port = f['port']
    world = create_world(admin)
    switch(admin, world, True)
    online(admin, 'inbound.rule.online')
    created = port.create_conversation(idempotency_key='synthetic-inbound-rule-conversation')
    before = shadow_events(admin, world)
    accepted = port.accept_message(conversation_id=created['id'], idempotency_key='synthetic-inbound-rule-1', body='合成：明天上午还有空位吗？')
    record = admin.execute('select record from runtime.nexloop_conversation_messages where message_id=%s', (accepted['message']['id'],)).fetchone()[0]
    assert 'direction' not in record  # the production shape (stage: all 118 real inbound messages)
    assert shadow_events(admin, world) == before + 1
    assert allocations(admin, f['consumer']) == 1


pytestmark_staff = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)


@pytestmark_staff
def test_a_persons_reply_in_a_takeover_is_neither_copied_nor_allocated(commitments, admin, identity, uow):
    from test_staff_reply_pg import reply, setup
    from test_takeover_pg import take
    c = commitments
    actions, _, conversation = setup(c, identity, uow)
    take(actions, 'conversation', conversation, 'take-inbound-rule')
    inbound = c.inbound('我的退款到了吗？')
    world = create_world(admin, tenant=TENANT)
    switch(admin, world, True, tenant=TENANT)
    online(admin, 'inbound.rule.staff')
    before, allocated = shadow_events(admin, world), allocations(admin, c['consumer'])
    actions, _ = c.owner(identity, uow)
    created = reply(actions, conversation, inbound, '您好，我是人工客服。', 'staff-inbound-rule')
    assert created['message']['sender_kind'] == 'human_takeover'
    assert shadow_events(admin, world) == before and allocations(admin, c['consumer']) == allocated
