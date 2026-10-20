"""Proactive outreach slice 4 (docs/implementation/outreach-impl-plan.md; design §3.1/§3.2): the governed send
nexloop.outreach.send:1 and its dispatch gates, on clean catalog PostgreSQL. The owner asks through the workbench's governed
entry; an executor service (granted nexloop.outreach.execute:1 here) admits each intent at dispatch and calls a loopback outbox
once. The real dispatch switch of the Action is closed by default; the tests that need a pass open it only in this disposable
synthetic database (as the switch tests do). Synthetic data only; nothing reaches a real channel."""
from datetime import UTC, datetime, timedelta

import pytest
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from psycopg.types.json import Jsonb

from commitment_fixture import TENANT, commitments  # noqa: F401
from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: F401
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.outreach import EXECUTE_ACTION, SEND_ACTION, LocalOutreachOutbox, OutreachPort, OutreachSender
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401
from test_contact_effect_categories_pg import REFUSAL
from test_contact_preferences_pg import statement
from test_real_dispatch_switch_pg import set_switch
from test_restore_fence_pg import restoring  # noqa: F401
from test_workbench_actions_http import act
from test_workbench_read_pg import workbench_login
from commercial_fixture import publish_registry_actions

pytestmark = pytest.mark.parametrize('execution_plan', ['commitment-service'], indirect=True)
OPEN = {'time_zone': 'Asia/Shanghai', 'quiet_hours': None, 'per_24h': 100, 'per_7d': 1000,
        'marketing': {'enabled': False, 'consent_mode': 'opt_out', 'unsubscribe_notice': '回复“退订”即可不再收到。', 'per_7d': 10, 'sender_label': ''}}
NOTHING = {'admitted': 0, 'refused': 0, 'fulfilled': 0, 'failed': 0, 'unknown': 0}


@pytest.fixture
def outreach(restoring, admin, tmp_path):
    c = restoring
    publish_registry_actions(admin, ('nexloop.outreach.send',))
    _, token = seed_multi_authority(admin, c['worker'], [(EXECUTE_ACTION, ResourceType.ACTION, Operation.EXECUTE)],
                                    identity_suffix='-outreach-sender', tenant=TENANT)
    c.update(sender_session=lambda: authenticate_service(c['worker'], token, world='real'), outbox=LocalOutreachOutbox(tmp_path / 'outbox'))
    return c


def tick(c, outbox=None):
    return OutreachSender(c['worker'], c['sender_session'](), c['signer'], outbox or c['outbox']).run_once()


def send(c, *, purpose='commitment_followup', origin='commitment:synthetic-1', body='您好，您预约的服务明天上午开始。', member=None, **extra):
    with c['app']() as client:
        csrf = workbench_login(client, member or c['owner_member']).json()['csrf_token']
        return act(client, csrf, 'send_outreach', {'consumer_id': c['consumer'], 'purpose': purpose, 'origin_ref': origin, 'body': body,
                                                   'channel': 'native.webchat', **extra})


def row(admin, intent_id):
    return admin.execute('select state,refusal_code,fence from runtime.nexloop_outreach_sends where intent_id=%s', (intent_id,)).fetchone()


def controls(admin, **change):
    settings = {**OPEN, **change}
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute('''insert into control.nexloop_outreach_controls(tenant_id,settings,revision,updated_by,request_intent) values(%s,%s,1,'synthetic-owner','synthetic')
            on conflict(tenant_id) do update set settings=excluded.settings,revision=control.nexloop_outreach_controls.revision+1''', (TENANT, Jsonb(settings)))


def purpose_state(admin, purpose, enabled):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute('''insert into control.nexloop_outreach_purpose_states(tenant_id,purpose_id,enabled,revision,updated_by,request_intent) values(%s,%s,%s,1,'synthetic-owner','synthetic')
            on conflict(tenant_id,purpose_id) do update set enabled=excluded.enabled''', (TENANT, purpose, enabled))


def model_budget(admin, *, start=-1, end=30, limit=1000):
    now = datetime.now(UTC)
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute("""insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
            values(%s,'real','model','CNY',%s,%s,%s,1) on conflict(tenant_id,world,budget_kind)
            do update set limit_amount=excluded.limit_amount,period_start=excluded.period_start,period_end=excluded.period_end""",
            (TENANT, str(limit), now + timedelta(days=start), now + timedelta(days=end)))


def open_everything(c, admin, monkeypatch):
    """Every gate passes: only in this synthetic database (deployment, tenant and Action switches, budget, relaxed controls)."""
    monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'true')
    set_switch(admin, TENANT, 'tenant', '*', True, 'real')
    set_switch(admin, TENANT, 'action_type', SEND_ACTION, True)
    model_budget(admin)
    controls(admin)


def test_the_switch_is_closed_by_default_and_refuses_before_the_provider(outreach, admin, monkeypatch):
    c = outreach
    monkeypatch.setenv('REAL_DISPATCH_ENABLED', 'true')
    model_budget(admin)
    controls(admin)
    assert admin.execute("select count(*) from control.nexloop_real_dispatch_switches where scope_ref=%s", (SEND_ACTION,)).fetchone() == (0,)
    accepted = send(c)
    assert accepted.status_code == 200, accepted.text
    out = accepted.json()
    assert out['state'] == 'accepted' and out['replayed'] is False and out['intent_id'].startswith('outreach:')
    assert c['outbox'].requests == [] and row(admin, out['intent_id'])[0] == 'accepted'  # accepted is not sent
    assert tick(c) == {**NOTHING, 'refused': 1}
    assert row(admin, out['intent_id'])[:2] == ('refused', 'real_dispatch_closed') and c['outbox'].requests == []
    assert tick(c) == NOTHING  # terminal: never admitted again


def test_an_open_send_is_sent_once_and_the_business_intent_is_stable(outreach, admin, monkeypatch):
    c = outreach
    open_everything(c, admin, monkeypatch)
    first = send(c).json()
    again = send(c).json()  # a retry of the same request (or a second one the same day) is the same intent
    assert again['intent_id'] == first['intent_id'] and again['replayed'] is True
    other = send(c, body='另一段文字')
    assert other.status_code == 409 and other.json()['details'] == {'reason': 'intent_exists'}
    assert tick(c) == {**NOTHING, 'admitted': 1, 'fulfilled': 1}
    assert row(admin, first['intent_id'])[0] == 'fulfilled' and c['outbox'].requests == [('send', first['intent_id'])]
    assert send(c).json()['replayed'] is True and tick(c) == NOTHING and len(c['outbox'].requests) == 1
    # The admitted send counts against the tenant frequency: a second purpose origin the same day is capped at dispatch.
    controls(admin, per_24h=1)
    second = send(c, origin='commitment:synthetic-2').json()
    assert tick(c) == {**NOTHING, 'refused': 1} and row(admin, second['intent_id'])[:2] == ('refused', 'frequency_capped')
    assert len(c['outbox'].requests) == 1


def _restrict(c, admin):
    c.inbound(REFUSAL)


def _pause(c, admin):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute("insert into control.nexloop_control_scopes(tenant_id,world,scope_kind,scope_ref,paused,revision,reason) values(%s,'real','consumer',%s,true,1,'合成暂停') "
                      "on conflict (tenant_id,world,scope_kind,scope_ref) do update set paused=true", (TENANT, c['consumer']))


def _persona(c, admin):
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute("insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by) values(%s,'real',%s,'synthetic',1,'synthetic-owner')",
                      (TENANT, c['consumer']))


def _revoke_requester(c, admin):
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("update control.nexloop_browser_memberships set payload=jsonb_set(payload,'{status}','\"suspended\"') where tenant_id=%s and principal_id=%s",
                      (TENANT, c['owner_member']['principal_id']))


GATES = {
    'contact_restricted': _restrict,
    'requester_unauthorized': _revoke_requester,
    'control_paused': _pause,
    'purpose_disabled': lambda c, a: purpose_state(a, 'commitment_followup', False),
    'persona_excluded': _persona,
    'quiet_hours': lambda c, a: controls(a, quiet_hours='00:00-23:59'),
    'frequency_capped': lambda c, a: controls(a, per_24h=0),
    'preference_topic': lambda c, a: statement(c, 'contact_topic', 'commitment_followup', polarity='negated'),
    'real_dispatch_closed': lambda c, a: set_switch(a, TENANT, 'action_type', SEND_ACTION, False),
    'budget_exhausted': lambda c, a: model_budget(a, start=-30, end=-1),
}


@pytest.mark.parametrize('code', list(GATES))
def test_every_gate_is_checked_again_at_dispatch(outreach, admin, monkeypatch, code):
    c = outreach
    open_everything(c, admin, monkeypatch)
    if code == 'requester_unauthorized':  # the member's login must precede the suspension
        intent = send(c).json()['intent_id']
        _revoke_requester(c, admin)
    else:
        intent = send(c).json()['intent_id']  # accepted while everything passes
        GATES[code](c, admin)  # then the world changes before dispatch
    assert tick(c) == {**NOTHING, 'refused': 1}
    assert row(admin, intent)[:2] == ('refused', code) and c['outbox'].requests == []  # the provider received nothing
    gates = admin.execute('select refusal_gates from runtime.nexloop_outreach_sends where intent_id=%s', (intent,)).fetchone()[0]
    assert gates is None or any(g['result'] != 'pass' for g in gates)


def test_the_contact_restriction_is_checked_first(outreach, admin, monkeypatch):
    c = outreach
    open_everything(c, admin, monkeypatch)
    intent = send(c).json()['intent_id']
    _pause(c, admin)
    _persona(c, admin)
    controls(admin, per_24h=0, quiet_hours='00:00-23:59')
    _restrict(c, admin)
    tick(c)
    assert row(admin, intent)[:2] == ('refused', 'contact_restricted') and c['outbox'].requests == []


def test_marketing_needs_the_switch_and_consent_where_the_tenant_or_purpose_asks_for_it(outreach, admin, monkeypatch):
    c = outreach
    open_everything(c, admin, monkeypatch)
    purpose_state(admin, 'marketing_general', True)
    off = send(c, purpose='marketing_general', origin='owner:spring').json()['intent_id']
    tick(c)
    assert row(admin, off)[:2] == ('refused', 'marketing_disabled')
    controls(admin, marketing={**OPEN['marketing'], 'enabled': True, 'consent_mode': 'opt_in'})
    opt_in = send(c, purpose='marketing_general', origin='owner:summer').json()['intent_id']
    tick(c)
    assert row(admin, opt_in)[:2] == ('refused', 'marketing_consent_missing')  # consent records arrive with slice 7
    controls(admin, marketing={**OPEN['marketing'], 'enabled': True})
    statement(c, 'contact_topic', 'marketing', polarity='negated')  # opt-out: the customer refused the marketing topic
    refused = send(c, purpose='marketing_general', origin='owner:autumn').json()['intent_id']
    tick(c)
    assert row(admin, refused)[:2] == ('refused', 'preference_topic') and c['outbox'].requests == []


def test_a_topic_preference_refuses_every_topic_below_it(outreach, admin):
    c = outreach
    statement(c, 'contact_topic', 'marketing', polarity='negated')
    refusal = "select runtime.nexloop_contact_preference_refusal(%s,'real',%s,'native.webchat',%s,gen_random_uuid(),clock_timestamp())"
    assert admin.execute(refusal, (TENANT, c['consumer'], 'marketing.spring_sale')).fetchone()[0] == 'preference_topic'
    assert admin.execute(refusal, (TENANT, c['consumer'], 'marketing')).fetchone()[0] == 'preference_topic'
    assert admin.execute(refusal, (TENANT, c['consumer'], 'marketingx')).fetchone()[0] is None
    assert admin.execute(refusal, (TENANT, c['consumer'], 'service_followup')).fetchone()[0] is None


def test_an_unknown_result_is_queried_first_and_never_sent_again(outreach, admin, monkeypatch, tmp_path):
    c = outreach
    open_everything(c, admin, monkeypatch)
    outbox = LocalOutreachOutbox(tmp_path / 'outbox-a', fault='timeout_after_write')
    intent = send(c).json()['intent_id']
    assert tick(c, outbox) == {**NOTHING, 'admitted': 1, 'unknown': 1} and row(admin, intent)[0] == 'unknown'
    with pytest.raises(Exception, match='fenced'):
        OutreachPort(c['worker'], c['sender_session'](), c['signer']).observe(intent, 0, 'failed')  # stale fence
    outbox.fault = None
    assert tick(c, outbox) == {**NOTHING, 'fulfilled': 1}
    assert outbox.requests == [('send', intent), ('query', intent)] and row(admin, intent)[0] == 'fulfilled'  # queried, not re-sent
    # Lost before the provider wrote it: the query answers not_found, a definite failure; still never sent twice.
    lost = LocalOutreachOutbox(tmp_path / 'outbox-b', fault='timeout_before_write')
    second = send(c, origin='commitment:synthetic-lost').json()['intent_id']
    tick(c, lost)
    lost.fault = None
    tick(c, lost)
    assert lost.requests == [('send', second), ('query', second)] and row(admin, second)[0] == 'failed'


def test_only_the_owner_sends_and_bad_requests_are_refused(outreach, admin):
    c = outreach
    assert send(c, member=c['operator_member']).status_code == 403
    assert send(c, purpose='flash_sale').json()['details'] == {'reason': 'purpose_not_allowed'}
    assert send(c, body='长' * 301).json()['details'] == {'reason': 'body_too_long'}  # the purpose's max_chars
    assert send(c, channel='sms').status_code == 422
    assert admin.execute('select count(*) from runtime.nexloop_outreach_sends').fetchone() == (0,)
    assert admin.execute("select has_function_privilege('nexloop_api','authz.nexloop_outreach_command(text,text,text,text,text)','execute')").fetchone() == (False,)
