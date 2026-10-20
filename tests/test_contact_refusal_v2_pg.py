"""Contact refusal rules v2 (ADR-023 §4; owner decision 2026-10-20, option B; migration 0240), on clean catalog PostgreSQL with the
outreach fixture (owner sends, loopback outbox, gates open only in this disposable synthetic database). Right after marketing (latest
dispatched proactive message is marketing, within 30 days) 退订 / TD / stop stop marketing only; everything else, and the same words
outside that condition, restrict all contact as in v1. Synthetic data only."""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from nexloop_eios.contact_restrictions import load_refusal_rules
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_outreach_send_pg import outreach, row  # noqa: F401
from test_outreach_unsubscribe_consent_pg import marketing, optouts, restricted, sent  # noqa: F401
from test_restore_fence_pg import restoring  # noqa: F401

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
ROOT = Path(__file__).resolve().parents[1] / 'deploy/configuration'
V1, V2 = (load_refusal_rules(ROOT / f'contact-refusal-rules.v{n}.json') for n in (1, 2))


def service_reaches(c, admin, origin):
    return row(admin, sent(c, admin, purpose='commitment_followup', origin=origin))[0] == 'fulfilled'


def test_v2_is_published_beside_v1_and_the_switch_is_recorded(outreach, admin):
    rows = dict(admin.execute('select version,definition from control.nexloop_contact_refusal_rules').fetchall())
    assert rows == {1: V1, 2: V2}  # v1 unchanged, v2 the published file
    assert V2['marketing_only'] == {'rule_ids': ['unsubscribe', 'sms-stop-keyword'], 'window_days': 30}
    assert [r['id'] for r in V2['refuse']] == ['stop-contact', 'unsubscribe', 'block-or-erase', 'threat-if-contacted', 'sms-stop-keyword']
    assert V2['uncertain'] == V1['uncertain'] and V2['exclusions'] == V1['exclusions']
    assert admin.execute('select from_version,to_version,decision_ref from control.nexloop_contact_refusal_rule_switches').fetchall() == [
        (1, 2, 'adr/ADR-023-explicit-refusal-hard-stop.md §4; docs/implementation/outreach-impl-plan.md §7')]
    with pytest.raises(Exception, match='append-only'):
        admin.execute("update control.nexloop_contact_refusal_rule_switches set decision='x'")


@pytest.mark.parametrize('body,rule', [('退订', 'unsubscribe'), ('TD', 'sms-stop-keyword'), ('stop', 'sms-stop-keyword')])
def test_right_after_marketing_these_words_stop_marketing_only(marketing, admin, body, rule):
    c = marketing
    first = sent(c, admin)
    c.inbound(body)
    assert not restricted(admin, c)
    assert admin.execute('select source,rule_version,rule_id,outreach_intent from runtime.nexloop_marketing_optouts').fetchall() == [('refusal_rule', 2, rule, first)]
    assert admin.execute('select count(*) from control.nexloop_contact_refusal_hits where consumer_id=%s', (c['consumer'],)).fetchone() == (0,)
    assert row(admin, sent(c, admin, origin='owner:summer'))[:2] == ('refused', 'marketing_opted_out')
    assert service_reaches(c, admin, 'commitment:synthetic-1')  # service outreach goes on


def test_outside_the_window_or_when_the_latest_was_not_marketing_everything_stops(marketing, admin):
    c = marketing
    old = sent(c, admin)
    with admin.transaction():
        admin.execute('update runtime.nexloop_outreach_sends set admitted_at=admitted_at-%s where intent_id=%s', (timedelta(days=31), old))
    c.inbound('退订')
    assert restricted(admin, c) and optouts(admin) == []
    assert admin.execute('select rule_version,rule_id from control.nexloop_contact_refusal_hits where consumer_id=%s', (c['consumer'],)).fetchone() == (2, 'unsubscribe')


def test_when_the_latest_proactive_message_was_service_everything_stops(marketing, admin):
    c = marketing
    sent(c, admin)
    assert service_reaches(c, admin, 'commitment:synthetic-1')  # the latest is now a service message
    c.inbound('TD')
    assert restricted(admin, c) and optouts(admin) == []


def test_without_any_proactive_message_everything_stops(marketing, admin):
    c = marketing
    c.inbound('退订')
    assert restricted(admin, c) and optouts(admin) == []


@pytest.mark.parametrize('body,rule', [('别再给我发消息了', 'stop-contact'), ('把我拉黑吧', 'block-or-erase'), ('再发我就投诉你们', 'threat-if-contacted'),
                                       ('烦死了', 'annoyed'), ('退订，别再联系我了', 'stop-contact'), ('退订！以后不要再打扰我', 'stop-contact')])
def test_other_refusals_and_mixed_messages_stop_everything_even_after_marketing(marketing, admin, body, rule):
    c = marketing
    sent(c, admin)
    c.inbound(body)
    assert restricted(admin, c)
    assert not any(o[1] == 'refusal_rule' for o in optouts(admin))  # a full-stop hit wins: no marketing-only record
    hit = admin.execute('select rule_version,rule_id from control.nexloop_contact_refusal_hits where consumer_id=%s', (c['consumer'],)).fetchone()
    assert hit[0] == 2 and (hit[1] == rule or rule == 'annoyed')
    assert row(admin, sent(c, admin, purpose='commitment_followup', origin='commitment:synthetic-1'))[:2] == ('refused', 'contact_restricted')


def test_a_customer_who_wants_everything_stopped_says_so_again(marketing, admin):
    c = marketing
    sent(c, admin)
    c.inbound('退订')
    assert not restricted(admin, c)
    c.inbound('别联系我了')
    assert restricted(admin, c)
