"""Proactive outreach slice 8 (docs/implementation/outreach-impl-plan.md; design §5, P7): metrics, the evidence chain and the owner's
view, on clean catalog PostgreSQL with the slice 4/6/7 fixtures (owner sends, a second owner approves, loopback outbox; gates opened
only in this disposable synthetic database). Positive controls: an approval, a fulfilled send, a marketing send and its unsubscribe, a
consent record and a frequency refusal each move exactly their counters. The evidence chain of a send carries its trigger, request,
approval, gate conclusions, dispatch, events and the customer's records; no body. Reading writes nothing; only the owner reads."""
import json
from pathlib import Path

import pytest

from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from nexloop_eios.observability import load_rules
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_outreach_approval_pg import PURPOSE, approvals, decide  # noqa: F401
from test_outreach_send_pg import OPEN, controls, open_everything, outreach, purpose_state, row, send, tick  # noqa: F401
from test_outreach_unsubscribe_consent_pg import MARKETING_ONLY, record
from test_real_dispatch_switch_pg import owner_sql
from test_restore_fence_pg import restoring  # noqa: F401
from test_workbench_read_pg import workbench_login

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
V8 = Path(__file__).resolve().parents[1] / 'deploy/configuration/alert-rules.v8.json'  # v7 is NX-039 V5's; slice 8 is v8 (integration)
BODY = '您好，您预约的服务明天上午开始。'


def metrics(admin):
    return owner_sql(admin, "select runtime.nexloop_outreach_metrics(%s,'real')", (TENANT,))


def get(c, path, member=None):
    with c['app']() as client:
        workbench_login(client, member or c['owner_member'])
        return client.get(path)


def writes(admin):
    return admin.execute("""select (select count(*) from runtime.nexloop_outreach_sends),(select count(*) from runtime.nexloop_outreach_approvals),
        (select count(*) from runtime.nexloop_marketing_optouts),(select count(*) from runtime.nexloop_marketing_consents),
        (select count(*) from runtime.audit_events),(select count(*) from control.nexloop_alert_events)""").fetchone()


@pytest.fixture
def ready(approvals, admin, monkeypatch):
    c = approvals
    publish_registry_actions(admin, ('nexloop.marketing_consent.record',))
    open_everything(c, admin, monkeypatch)
    purpose_state(admin, 'marketing_general', True)
    controls(admin, marketing={**OPEN['marketing'], 'enabled': True})
    return c


def test_each_counter_moves_with_its_positive_control(ready, admin):
    c = ready
    m0 = metrics(admin)
    assert (m0['accepted_7d'], m0['pending_approval'], m0['sent_7d'], m0['approvals_7d'], m0['refused_7d']) == (0, 0, 0, {}, {})
    # An approval: pending, then approved, admitted and fulfilled.
    intent = send(c, purpose=PURPOSE, body=BODY).json()['intent_id']
    m = metrics(admin)
    assert (m['accepted_7d'], m['pending_approval'], m['approvals_7d']) == (1, 1, {'pending': 1})
    assert decide(c, intent, 'approve').status_code == 200
    tick(c)
    m = metrics(admin)
    assert (m['pending_approval'], m['approvals_7d'], m['sent_7d'], m['fulfilled_7d']) == (0, {'approved': 1}, 1, 1)
    assert m['by_purpose_7d'] == {PURPOSE: {'accepted': 1, 'sent': 1, 'refused': 0}}
    # Marketing and its unsubscribe (below 20 marketing sends the rate is not reported, never a misleading 100%).
    send(c, purpose='marketing_general', origin='owner:spring'); tick(c)
    c.inbound(MARKETING_ONLY)
    m = metrics(admin)['marketing']
    assert (m['sent_7d'], m['unsubscribes_7d'], m['unsubscribe_rate_7d'], m['min_volume'], m['allowance_per_7d'], m['consumers_at_allowance']) == (1, 1, None, 20, 10, 0)
    controls(admin, marketing={**OPEN['marketing'], 'enabled': True, 'per_7d': 1})
    assert metrics(admin)['marketing']['consumers_at_allowance'] == 1  # the allowance is used up for this customer
    # A consent record.
    said = c.inbound('可以给我发活动消息')
    assert record(c, kind='consent_granted', source='consumer_message', message_id=said).status_code == 200
    k = metrics(admin)['consent']
    assert (k['granted_7d'], k['withdrawn_7d'], k['consumers_with_record'], k['consumers_consented'], k['consent_rate']) == (1, 0, 1, 1, 1.0)
    # A frequency refusal.
    controls(admin, per_24h=1, marketing={**OPEN['marketing'], 'enabled': True})
    send(c, origin='commitment:synthetic-9'); tick(c)
    m = metrics(admin)
    assert m['frequency_capped_7d'] == 1 and m['refused_7d'] == {'frequency_capped': 1}
    # The snapshot carries the same block (metrics export, alert evaluator, owner metrics read).
    snap = owner_sql(admin, "select runtime.nexloop_metrics_snapshot(%s,'real')->'outreach'", (TENANT,))
    assert {k: v for k, v in snap.items() if k != 'computed_at'} == {k: v for k, v in metrics(admin).items() if k != 'computed_at'}
    assert BODY not in json.dumps(snap, ensure_ascii=False)


def test_the_evidence_chain_of_a_send_is_complete_and_never_carries_the_body(ready, admin):
    c = ready
    intent = send(c, purpose=PURPOSE, body=BODY).json()['intent_id']
    decide(c, intent, 'approve'); tick(c)
    response = get(c, f'/api/v1/workbench/outreach-evidence?intent_id={intent}')
    assert response.status_code == 200, response.text
    e = response.json()
    assert BODY not in response.text and e['found'] is True
    assert e['trigger'] == {'origin_kind': 'proactive', 'origin': 'commitment', 'origin_ref': 'commitment:synthetic-1', 'initiator': 'human', 'run_id': None}
    assert e['request']['intent_id'] == intent and e['request']['purpose_id'] == PURPOSE and e['request']['body_chars'] == len(BODY)
    assert len(e['request']['body_digest']) == 64 and e['request']['persona'] == 'real'
    assert e['approval']['decision'] == 'approved' and e['approval']['decided_by'] == c['approver']['principal_id']
    assert e['gates']['stage'] == 'admitted' and e['gates']['gates'] == 'all_passed'
    assert e['dispatch']['state'] == 'fulfilled' and e['dispatch']['provider'] == 'loopback' and e['dispatch']['provider_reference']
    assert [x['action'] for x in e['events']] == ['nexloop.outreach.send.accepted', 'nexloop.outreach.send.fulfilled']
    # A refused send: every gate's conclusion as recorded at admission.
    controls(admin, per_24h=0)
    refused = send(c, origin='commitment:synthetic-2').json()['intent_id']; tick(c)
    r = get(c, f'/api/v1/workbench/outreach-evidence?intent_id={refused}').json()
    assert r['gates']['stage'] == 'refused' and r['gates']['code'] == 'frequency_capped'
    assert {g['gate']: g['result'] for g in r['gates']['gates']}['frequency'] == 'refuse' and r['approval'] is None
    # A marketing send and the unsubscribe that answered it.
    controls(admin, marketing={**OPEN['marketing'], 'enabled': True})
    marketing = send(c, purpose='marketing_general', origin='owner:spring').json()['intent_id']; tick(c)
    c.inbound(MARKETING_ONLY)
    mk = get(c, f'/api/v1/workbench/outreach-evidence?intent_id={marketing}').json()
    assert [(o['kind'], o['answers_this_send']) for o in mk['responses']['optouts']] == [('unsubscribed', True)]
    # The list, newest first; an unknown id is "not found", not an error.
    items = get(c, '/api/v1/workbench/outreach-evidence?limit=10').json()['items']
    assert [i['intent_id'] for i in items] == [marketing, refused, intent] and items[1]['refusal_code'] == 'frequency_capped'
    assert get(c, '/api/v1/workbench/outreach-evidence?intent_id=outreach:' + '0' * 64).json() == {'found': False}


def test_the_demonstration_view_lists_synthetic_personas_only(ready, admin):
    c = ready
    intent = send(c).json()['intent_id']; tick(c)
    assert get(c, '/api/v1/workbench/outreach-evidence?synthetic_only=true').json() == {'synthetic_only': True, 'items': []}
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute("insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by) values(%s,'real',%s,'synthetic',1,'synthetic-owner')",
                      (TENANT, c['consumer']))
    demo = get(c, '/api/v1/workbench/outreach-evidence?synthetic_only=true').json()['items']
    assert [(i['intent_id'], i['persona']) for i in demo] == [(intent, 'synthetic')]
    # A synthetic persona never reaches a provider: the next send is refused at the persona gate (real dispatch stays as it was).
    later = send(c, origin='commitment:synthetic-3').json()['intent_id']; tick(c)
    assert row(admin, later)[:2] == ('refused', 'persona_excluded')


def test_reading_writes_nothing_only_the_owner_reads_and_rules_v8_load(ready, admin):
    c = ready
    send(c); tick(c)
    before = writes(admin)
    assert get(c, '/api/v1/workbench/outreach-metrics').status_code == 200
    assert get(c, '/api/v1/workbench/outreach-evidence').status_code == 200
    metrics(admin)
    assert writes(admin) == before
    for path in ('/api/v1/workbench/outreach-metrics', '/api/v1/workbench/outreach-evidence', '/api/v1/workbench/outreach-evidence?intent_id=outreach:' + '0' * 64):
        assert get(c, path, c['operator_member']).status_code == 403
    for bad in ('?x=1', '?intent_id=bad', '?limit=0', '?limit=201', '?synthetic_only=yes', '?intent_id=outreach:' + '0' * 64 + '&limit=5'):
        assert get(c, '/api/v1/workbench/outreach-evidence' + bad).status_code == 422, bad
    assert get(c, '/api/v1/workbench/outreach-metrics?x=1').status_code == 422
    rules = load_rules(V8)
    ours = {r['rule_id']: r for r in rules['rules'] if r['rule_id'].startswith('outreach_')}
    assert set(ours) == {'outreach_unsubscribe_rate_high', 'outreach_approvals_waiting'}
    assert ours['outreach_unsubscribe_rate_high']['metric'] == 'outreach.marketing.unsubscribe_rate_7d'
    assert {r['rule_id'] for r in load_rules(V8.parent / 'alert-rules.v7.json')['rules']} < set(r['rule_id'] for r in rules['rules'])
