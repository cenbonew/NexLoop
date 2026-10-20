"""Proactive outreach slice 7 (docs/implementation/outreach-impl-plan.md; design §4.4/§4.5): unsubscribe after marketing and
marketing consent, on clean catalog PostgreSQL with the slice 4 fixture (owner sends, loopback outbox, every gate open only in this
disposable synthetic database). Contact refusal rules still run first (regression below). Synthetic data only."""
import json
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from commercial_fixture import publish_registry_actions
from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_outreach_send_pg import OPEN, controls, open_everything, outreach, purpose_state, row, send, tick  # noqa: F401
from test_restore_fence_pg import restoring  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
ROOT = Path(__file__).resolve().parents[1]
RULES = json.loads((ROOT / 'deploy/configuration/unsubscribe-rules.v1.json').read_text())
MARKETING_ONLY = '不想收到活动消息了'  # an unsubscribe entry that contact refusal rules v1 do not match


@pytest.fixture
def marketing(outreach, admin, monkeypatch):
    c = outreach
    publish_registry_actions(admin, ('nexloop.marketing_consent.record',))
    open_everything(c, admin, monkeypatch)
    purpose_state(admin, 'marketing_general', True)
    controls(admin, marketing={**OPEN['marketing'], 'enabled': True})
    return c


def sent(c, admin, purpose='marketing_general', origin='owner:spring'):
    intent = send(c, purpose=purpose, origin=origin).json()['intent_id']
    tick(c)
    return intent


def optouts(admin):
    return admin.execute('select kind,source,rule_id,outreach_intent from runtime.nexloop_marketing_optouts order by event_id').fetchall()


def restricted(admin, c):
    return admin.execute('select coalesce(bool_or(active),false) from control.nexloop_contact_restrictions where consumer_id=%s', (c['consumer'],)).fetchone()[0]


def record(c, **body):
    with c['app']() as client:
        csrf = workbench_login(client, body.pop('member', None) or c['owner_member']).json()['csrf_token']
        return act(client, csrf, 'record_marketing_consent', {'consumer_id': c['consumer'], 'message_id': None, 'evidence_ref': None, 'reason': '合成留证', **body})


def test_the_rule_row_is_the_published_file_and_matching_is_deterministic(outreach, admin):
    assert admin.execute('select definition from control.nexloop_unsubscribe_rules where version=1').fetchone()[0] == RULES
    match = lambda body: admin.execute('select runtime.nexloop_unsubscribe_match(%s)', (body,)).fetchone()[0]
    assert match('退订')['rule_id'] == 'unsubscribe-word' and match(' ＴＤ。')['rule_id'] == 'sms-td'  # NFKC, case, punctuation
    assert match('以后别给我推活动了')['matched'] is True and match(MARKETING_ONLY)['matched'] is True
    assert match('怎么退订？')['matched'] is False and match('退订了吗')['matched'] is False  # exclusions first
    assert match('退订一下吧')['matched'] is False and match('明天几点开始')['matched'] is False  # exact entries are whole messages


def test_an_unsubscribe_after_marketing_stops_marketing_only(marketing, admin):
    c = marketing
    first = sent(c, admin)
    assert row(admin, first)[0] == 'fulfilled'
    c.inbound(MARKETING_ONLY)
    assert [(k, s, i) for k, s, _, i in optouts(admin)] == [('unsubscribed', 'unsubscribe_rule', first)]
    assert not restricted(admin, c)  # not a general contact restriction
    refused = sent(c, admin, origin='owner:summer')
    assert row(admin, refused)[:2] == ('refused', 'marketing_opted_out')
    assert row(admin, sent(c, admin, purpose='commitment_followup', origin='commitment:synthetic-1'))[0] == 'fulfilled'  # service still reaches the customer


def test_an_unsubscribe_counts_only_after_marketing_and_inside_the_window(marketing, admin):
    c = marketing
    service = sent(c, admin, purpose='commitment_followup', origin='commitment:synthetic-1')
    c.inbound(MARKETING_ONLY)
    assert row(admin, service)[0] == 'fulfilled' and optouts(admin) == []  # the latest proactive message was not marketing
    old = sent(c, admin)
    with admin.transaction():
        admin.execute("update runtime.nexloop_outreach_sends set admitted_at=admitted_at-%s where intent_id=%s", (timedelta(days=RULES['window_days'] + 1), old))
    c.inbound(MARKETING_ONLY)
    assert optouts(admin) == []  # outside window_days
    recent = sent(c, admin, origin='owner:summer')
    assert row(admin, recent)[0] == 'fulfilled'
    c.inbound('这个活动什么时候结束')  # not an unsubscribe (exclusions are covered by the matching test above)
    assert optouts(admin) == []
    c.inbound(MARKETING_ONLY)
    assert [(k, s, i) for k, s, _, i in optouts(admin)] == [('unsubscribed', 'unsubscribe_rule', recent)]


def test_a_full_stop_refusal_after_marketing_still_restricts_all_contact(marketing, admin):
    """Refusal rules still run first (v2 since ADR-023 §4; the narrow marketing case is in tests/test_contact_refusal_v2_pg.py)."""
    c = marketing
    sent(c, admin)
    c.inbound('别再给我发消息了')
    assert restricted(admin, c) and optouts(admin) == []
    refused = sent(c, admin, purpose='commitment_followup', origin='commitment:synthetic-2')
    assert row(admin, refused)[:2] == ('refused', 'contact_restricted')  # the restriction stops everything, service included


def test_opt_in_needs_recorded_consent_and_withdrawal_stops_it_again(marketing, admin):
    c = marketing
    controls(admin, marketing={**OPEN['marketing'], 'enabled': True, 'consent_mode': 'opt_in'})
    assert row(admin, sent(c, admin))[:2] == ('refused', 'marketing_consent_missing')
    said = c.inbound('可以给我发活动消息')
    granted = record(c, kind='consent_granted', source='consumer_message', message_id=said)
    assert granted.status_code == 200 and granted.json()['consent'] is True, granted.text
    assert row(admin, sent(c, admin, origin='owner:summer'))[0] == 'fulfilled'
    withdrawn = record(c, kind='consent_withdrawn', source='offline', evidence_ref='call-log:synthetic-1')
    assert withdrawn.json()['consent'] is False
    assert row(admin, sent(c, admin, origin='owner:autumn'))[:2] == ('refused', 'marketing_consent_missing')
    events = admin.execute('select kind,source,message_id,evidence_ref,recorded_by from runtime.nexloop_marketing_consents order by event_id').fetchall()
    assert events == [('granted', 'consumer_message', said, None, c['owner_member']['principal_id']),
                      ('withdrawn', 'offline', None, 'call-log:synthetic-1', c['owner_member']['principal_id'])]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute("update runtime.nexloop_marketing_consents set kind='granted'")


def test_only_the_owner_records_with_evidence_and_resumes_after_an_unsubscribe(marketing, admin):
    c = marketing
    assert record(c, kind='consent_granted', source='offline', evidence_ref='form:1', member=c['operator_member']).status_code == 403
    unknown = record(c, kind='consent_granted', source='consumer_message', message_id='e' * 64)
    assert unknown.status_code == 409 and unknown.json()['details'] == {'reason': 'evidence_unavailable'}
    # An offline record needs its evidence reference: refused before any write.
    assert record(c, kind='consent_granted', source='offline').status_code != 200
    assert admin.execute('select count(*) from runtime.nexloop_marketing_consents').fetchone() == (0,)
    assert record(c, kind='marketing_resumed', source='offline', evidence_ref='call-log:1').json()['details'] == {'reason': 'not_opted_out'}
    sent(c, admin)
    c.inbound(MARKETING_ONLY)
    assert row(admin, sent(c, admin, origin='owner:summer'))[1] == 'marketing_opted_out'
    said = c.inbound('还是给我发活动吧')
    resumed = record(c, kind='marketing_resumed', source='consumer_message', message_id=said)
    assert resumed.status_code == 200 and resumed.json()['opted_out'] is False
    assert row(admin, sent(c, admin, origin='owner:autumn'))[0] == 'fulfilled'
    assert [o[:2] for o in optouts(admin)] == [('unsubscribed', 'unsubscribe_rule'), ('resumed', 'owner')]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute('delete from runtime.nexloop_marketing_optouts')
