"""Proactive outreach slice 1 (0231; docs/implementation/outreach-impl-plan.md §3): candidates from the existing triggers and a
dispatch dry run through the design's gates, read only. Clean catalog PostgreSQL; actual governed Run submissions, the
independent effect Worker with a loopback provider, a registered Commitment; admin seeds the consumer's messages and Claims
as in the commitment tests. Nothing is written or sent by the read; no outreach Action exists yet."""
from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb
from support.effect_provider import effect_provider

from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_http import ORIGIN
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_commitments_pg import fresh
from test_contact_effect_categories_pg import REFUSAL
from test_contact_preferences_pg import statement
from test_restore_fence_pg import restoring  # noqa: F401
from test_workbench_read_pg import workbench_login

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
# Outreach slice 4: the contact restriction first (ADR-023) and the marketing gate.
GATES = ['contact_restricted', 'control_paused', 'purpose', 'marketing', 'persona_excluded', 'quiet_hours', 'frequency', 'consumer_preference', 'real_dispatch', 'budget']


def candidates(admin):
    return admin.execute("select runtime.nexloop_outreach_candidates(%s,'real',clock_timestamp())", (TENANT,)).fetchone()[0]


def gate(item, name):
    return next(g for g in item['gates'] if g['gate'] == name)


def written(admin):
    return admin.execute('select (select count(*) from runtime.nexloop_effect_intents),(select count(*) from runtime.nexloop_outbound_messages),'
                         '(select count(*) from runtime.nexloop_effect_events)').fetchone()


def test_candidates_come_from_the_triggers_and_every_gate_is_evaluated_without_writing(restoring, admin, tmp_path):
    c = restoring
    c.configure_categories()
    commitment, _ = fresh(c, due_in=timedelta(minutes=30))  # due within lead_seconds (3600)
    fresh(c, due_in=timedelta(days=2), predicate='另一项承诺')  # a different promise, not yet due: no candidate
    with effect_provider(tmp_path / 'provider.sqlite') as provider:
        delivered = c.submit({'service': '后台退款处理'})['intent_id']
        c.deliver(provider, delivered)
    before = written(admin)
    view = candidates(admin)
    assert written(admin) == before  # a dry run: no intent, outbound record or effect event
    # 0232: the tenant's settings, the owner's defaults while none are stored.
    assert {k: view['settings'][k] for k in ('time_zone', 'quiet_hours', 'per_24h', 'per_7d', 'source', 'revision')} == {
        'time_zone': 'Asia/Shanghai', 'quiet_hours': '21:00-09:00', 'per_24h': 1, 'per_7d': 2, 'source': 'default', 'revision': 0}
    by = {item['purpose']: item for item in view['candidates']}
    assert set(by) == {'commitment_followup', 'service_followup'} and len(view['candidates']) == 2
    assert by['commitment_followup']['origin_ref'] == 'commitment:' + commitment and by['commitment_followup']['dedupe_key'] == commitment + ':due_soon'
    assert by['service_followup']['origin_ref'] == 'intent:' + str(delivered) and by['service_followup']['dedupe_key'] == f'{delivered}:fulfilled'
    for item in view['candidates']:
        assert [g['gate'] for g in item['gates']] == GATES and item['consumer_id'] == c['consumer']
        assert gate(item, 'purpose') == {'gate': 'purpose', 'result': 'pass', 'detail': 'list v2'}  # outreach slice 3: the purpose list
        assert gate(item, 'budget') == {'gate': 'budget', 'result': 'refuse', 'detail': 'budget_unconfigured'}  # slice 4: the model budget
        assert gate(item, 'real_dispatch')['result'] == 'refuse' and item['would_dispatch'] is False and 'real_dispatch' in item['blocked_by']
        assert gate(item, 'contact_restricted')['result'] == gate(item, 'persona_excluded')['result'] == gate(item, 'control_paused')['result'] == 'pass'
        assert gate(item, 'quiet_hours')['detail'] == '21:00-09:00'
    # The default quiet hours at fixed instants (the gate function takes its time as a parameter), never the time the test runs.
    for at, result in (('2026-10-20T22:07:00+08:00', 'refuse'), ('2026-10-21T08:59:00+08:00', 'refuse'), ('2026-10-21T09:00:00+08:00', 'pass'),
                       ('2026-10-21T14:00:00+08:00', 'pass'), ('2026-10-21T20:59:00+08:00', 'pass'), ('2026-10-21T21:00:00+08:00', 'refuse')):
        gates = admin.execute("select runtime.nexloop_outreach_gates(%s,'real',%s,'commitment_followup',%s::timestamptz)", (TENANT, c['consumer'], at)).fetchone()[0]
        assert next(g for g in gates if g['gate'] == 'quiet_hours')['result'] == result, at
        assert gate(item, 'frequency') == {'gate': 'frequency', 'result': 'pass', 'detail': {'last_24h': 0, 'last_7d': 0, 'marketing_7d': 0}}  # replies and service work do not count


def test_each_gate_refuses_for_its_own_reason(restoring, admin, tmp_path):
    c = restoring
    c.configure_categories()
    fresh(c, due_in=timedelta(minutes=30))
    consumer = c['consumer']
    # A delivered notification contacts the customer: it counts against 1 per 24 h.
    with effect_provider(tmp_path / 'provider.sqlite') as provider:
        c.deliver(provider, c.submit({'service': '退款处理', 'message': '您的退款已处理'})['intent_id'])
    item = next(i for i in candidates(admin)['candidates'] if i['purpose'] == 'commitment_followup')
    assert gate(item, 'frequency') == {'gate': 'frequency', 'result': 'refuse', 'detail': {'last_24h': 1, 'last_7d': 1, 'marketing_7d': 0}}
    # The consumer refuses the topic: the operator persona chain's check answers for the dry run.
    statement(c, 'contact_topic', 'commitment_followup', polarity='negated')
    item = next(i for i in candidates(admin)['candidates'] if i['purpose'] == 'commitment_followup')
    assert gate(item, 'consumer_preference') == {'gate': 'consumer_preference', 'result': 'refuse', 'detail': 'preference_topic'}
    # ADR-034 test persona; an owner pause of the consumer; then a refusal (contact restriction, ADR-023).
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute("insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by) values(%s,'real',%s,'synthetic',1,'synthetic-owner')",
                      (TENANT, consumer))
        admin.execute("insert into control.nexloop_control_scopes(tenant_id,world,scope_kind,scope_ref,paused,revision,reason) values(%s,'real','consumer',%s,true,1,'合成暂停') "
                      "on conflict (tenant_id,world,scope_kind,scope_ref) do update set paused=true", (TENANT, consumer))
    c.inbound(REFUSAL)
    item = next(i for i in candidates(admin)['candidates'] if i['purpose'] == 'commitment_followup')
    assert {g['gate'] for g in item['gates'] if g['result'] == 'refuse'} >= {'control_paused', 'contact_restricted', 'persona_excluded', 'frequency', 'consumer_preference', 'real_dispatch'}
    assert gate(item, 'persona_excluded')['detail'] == 'synthetic' and item['would_dispatch'] is False


def test_the_read_is_the_owners(restoring, admin):
    c = restoring
    with c['app']() as client:
        workbench_login(client, c['operator_member'])
        assert client.get('/api/v1/workbench/outreach-candidates', headers={'Origin': ORIGIN}).status_code == 403
    with c['app']() as client:
        workbench_login(client, c['owner_member'])
        assert client.get('/api/v1/workbench/outreach-candidates?x=1', headers={'Origin': ORIGIN}).status_code == 422
        view = client.get('/api/v1/workbench/outreach-candidates', headers={'Origin': ORIGIN})
        assert view.status_code == 200 and view.json()['candidates'] == [] and 'nothing is sent' in view.json()['note']
