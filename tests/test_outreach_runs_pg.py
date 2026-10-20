"""Proactive outreach slice 5: a plan reevaluation Run asks for a proactive message through the runtime worker facade
(nexloop.outreach.run_submit:1). A real governed Run (issued, bound, accepted, claimed and activated, as the incentive Run
tests) submits only purpose and body; SQL re-checks the live activation, the lease and the exact command, requires a plan
reevaluation Run, the Run's resources (and Role ceiling) naming nexloop.outreach.send:1, and derives the Consumer, the origin
(the plan) and the business intent id. Accepted is not sent: the slice 4 executor admits it at dispatch, the real dispatch
switch stays closed. Synthetic data; admin seeds/probes."""
import secrets

import psycopg
import pytest
from psycopg.types.json import Jsonb
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
import runtime_effect_fixture as fixture
from runtime_effect_fixture import runtime_effect_plan  # noqa: F401
from test_incentive_runs_pg import restrict, run_with
from test_plan_reevaluation_pg import planning, services, spec  # noqa: F401
from nexloop_eios.outreach import LocalOutreachOutbox, OutreachSender, RunOutreachPort

SEND = 'eios:action:nexloop.outreach.send:1'
ASK = {'purpose': 'commitment_followup', 'body': '您好，您预约的服务明天上午开始，如需调整请直接回复。'}


@pytest.fixture(autouse=True)
def source_may_name_the_send(monkeypatch):
    monkeypatch.setattr(fixture, 'SOURCE_EXTRA_TARGETS', ((SEND, ResourceType.ACTION, Operation.EXECUTE),))


def quiet_hours_off(admin, tenant):
    """The tenant's outreach controls with no quiet hours (as the governed handler leaves them), so what these tests decide never depends
    on the time of day they run (the default 21:00-09:00 would refuse first: quiet hours come before the real dispatch switch)."""
    settings = admin.execute('select runtime.nexloop_outreach_default_settings()').fetchone()[0]
    with admin.transaction():
        admin.execute("select set_config('eios.tenant_id',%s,true)", (tenant,))
        admin.execute('''insert into control.nexloop_outreach_controls(tenant_id,settings,revision,updated_by,request_intent) values(%s,%s,1,'synthetic-owner','synthetic')
            on conflict(tenant_id) do update set settings=excluded.settings''', (tenant, Jsonb({**settings, 'quiet_hours': None})))


@pytest.fixture
def agent(planning):
    f = planning; plan = f['plan']; admin = f['admin']
    from commercial_fixture import publish_registry_actions
    publish_registry_actions(admin, ('nexloop.outreach.send',), tenant=plan['tenant'])  # a Run may name only published Actions
    token = fixture.seed_multi_uuid(admin, plan['tenant'], [('eios:action:' + fixture.QUEUE + ':1', ResourceType.ACTION, Operation.EXECUTE),
        ('eios:action:nexloop.outreach.run_submit:1', ResourceType.ACTION, Operation.EXECUTE)], suffix='-outreach-runtime-worker')
    sender_token = fixture.seed_multi_uuid(admin, plan['tenant'], [('eios:action:nexloop.outreach.execute:1', ResourceType.ACTION, Operation.EXECUTE)],
                                           suffix='-outreach-sender')
    s = spec(plan); f['port']().establish(s)
    quiet_hours_off(admin, plan['tenant'])
    yield dict(f, worker=plan['backend_worker'].authenticate(token, world='real'),
               sender=lambda: plan['backend_worker'].authenticate(sender_token, world='real'), spec=s)


def plan_run(f, label, resources=None):
    plan = f['plan']
    run, command, ref = run_with(plan, f['worker'], label, resources or ['eios:action:' + fixture.EFFECT + ':1', SEND])
    f['port']().link_run(plan_id=f['spec']['plan_id'], version=1, run_id=run.run_id, triggers=[{'kind': 'reassess_due', 'cause': 'system'}])
    return run, command, ref


def role_ceiling(f, run, resources):
    """The Run as a Role Run: a RoleDefinition whose RoleExecutionCeiling names these Action resources (as their governed writers leave them)."""
    plan = f['plan']; admin = f['admin']; role, ceiling = secrets.token_hex(32), secrets.token_hex(32)
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        for type_name, object_id, properties in (('RoleExecutionCeiling', ceiling, {'action_resources': resources}), ('RoleDefinition', role, {'ceiling_ref': ceiling})):
            admin.execute('''insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
                values(%s,'real',%s,%s,1,%s,'nexloop-action','synthetic',now(),now())''', (plan['tenant'], type_name, object_id, Jsonb(properties)))
        admin.execute('''insert into authz.nexloop_role_run_bindings(run_id,source_digest,tenant_id,world,consumer_id,link_id,role_id,step_id,link_revision,role_revision,
            step_revision,role_ref,scope,expires_at) values(%s,'synthetic',%s,'real',%s,'synthetic',%s,'synthetic',1,1,1,'role:plan-reevaluator','plan',now()+interval '1 hour')''',
            (run.run_id, plan['tenant'], plan['consumer'], role))
    return ceiling


def set_ceiling(f, ceiling, resources):
    with f['admin'].transaction():
        f['admin'].execute('set local session_replication_role=replica')
        f['admin'].execute("update ontology.objects set properties=%s where type_name='RoleExecutionCeiling' and object_id=%s", (Jsonb({'action_resources': resources}), ceiling))


def rows(admin):
    return admin.execute('select initiator,principal_id,run_id::text,origin_ref,purpose_id,state,refusal_code from runtime.nexloop_outreach_sends order by created_at').fetchall()


def test_a_plan_run_asks_inside_its_run_and_the_closed_switch_refuses_at_dispatch(agent, tmp_path):
    f = agent; admin = f['admin']; w = f['worker']
    run, command, ref = plan_run(f, 'a')
    out = w.send_run_outreach(activation_ref=ref, command=command, parameters=ASK)
    assert out['state'] == 'accepted' and out['replayed'] is False and out['run_id'] == run.run_id and out['intent_id'].startswith('outreach:')
    # The business intent is derived from consumer, purpose, the plan and the day: the same call replays, a different body does not.
    assert w.send_run_outreach(activation_ref=ref, command=command, parameters=ASK)['replayed'] is True
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='intent_exists'):
        w.send_run_outreach(activation_ref=ref, command=command, parameters={**ASK, 'body': '另一段文字'})
    assert rows(admin) == [('agent', 'run:' + run.run_id, run.run_id, 'plan:' + f['spec']['plan_id'], 'commitment_followup', 'accepted', None)]
    # Every slice 4 gate applies at dispatch; the Action's real dispatch switch is closed by default.
    outbox = LocalOutreachOutbox(tmp_path / 'outbox')
    assert OutreachSender(*services(f['sender']()), outbox).run_once()['refused'] == 1
    assert rows(admin)[0][5:] == ('refused', 'real_dispatch_closed') and outbox.requests == []


def test_the_contact_restriction_refuses_a_plan_runs_request_first(agent, tmp_path):
    f = agent; admin = f['admin']; w = f['worker']
    run, command, ref = plan_run(f, 'b')
    w.send_run_outreach(activation_ref=ref, command=command, parameters=ASK)
    restrict(admin, f['plan']['tenant'], f['plan']['consumer'])
    outbox = LocalOutreachOutbox(tmp_path / 'outbox')
    OutreachSender(*services(f['sender']()), outbox).run_once()
    assert rows(admin)[0][5:] == ('refused', 'contact_restricted') and outbox.requests == []


def test_only_a_plan_reevaluation_run_naming_the_send_may_ask(agent):
    f = agent; admin = f['admin']; w = f['worker']
    # A Run that is not a plan reevaluation Run (never linked to the plan): refused, nothing accepted.
    run, command, ref = run_with(f['plan'], w, 'c', ['eios:action:' + fixture.EFFECT + ':1', SEND])
    with pytest.raises(Exception):
        RunOutreachPort(*services(w)).submit(activation_ref=ref, command=command, parameters=ASK)
    # A plan Run whose credential does not name the send.
    run, command, ref = plan_run(f, 'd', ['eios:action:' + fixture.EFFECT + ':1'])
    with pytest.raises(Exception):
        RunOutreachPort(*services(w)).submit(activation_ref=ref, command=command, parameters=ASK)
    # A purpose outside the list, or a field the model may not choose.
    run, command, ref = plan_run(f, 'e')
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='purpose_not_allowed'):
        RunOutreachPort(*services(w)).submit(activation_ref=ref, command=command, parameters={**ASK, 'purpose': 'flash_sale'})
    with pytest.raises(ValueError):
        RunOutreachPort(*services(w)).submit(activation_ref=ref, command=command, parameters={**ASK, 'consumer_id': 'a' * 64})
    assert rows(admin) == []


def test_a_role_run_needs_its_ceiling_and_gets_the_outreach_section(agent, tmp_path):
    f = agent; admin = f['admin']; w = f['worker']; plan = f['plan']
    section = "select runtime.nexloop_outreach_run_section(%s,'real',%s,%s)"
    run, command, ref = plan_run(f, 'f')
    assert admin.execute(section, (plan['tenant'], plan['consumer'], run.run_id)).fetchone()[0] is None  # not a Role Run: no section
    ceiling = role_ceiling(f, run, ['eios:action:' + fixture.EFFECT + ':1'])
    assert admin.execute(section, (plan['tenant'], plan['consumer'], run.run_id)).fetchone()[0] is None  # the ceiling lacks the send
    with pytest.raises(Exception):
        RunOutreachPort(*services(w)).submit(activation_ref=ref, command=command, parameters=ASK)
    set_ceiling(f, ceiling, ['eios:action:' + fixture.EFFECT + ':1', SEND])
    view = admin.execute(section, (plan['tenant'], plan['consumer'], run.run_id)).fetchone()[0]
    assert view['schema'] == 'nexloop-outreach-context/1' and view['marketing'] == {'enabled': False, 'consent_mode': 'opt_out'}
    assert [p['purpose_id'] for p in view['purposes']] == ['commitment_followup', 'service_followup']  # only the enabled ones
    assert RunOutreachPort(*services(w)).submit(activation_ref=ref, command=command, parameters=ASK)['state'] == 'accepted'
    # The ceiling loses the send before dispatch: the request no longer stands.
    set_ceiling(f, ceiling, ['eios:action:' + fixture.EFFECT + ':1'])
    outbox = LocalOutreachOutbox(tmp_path / 'outbox')
    OutreachSender(*services(f['sender']()), outbox).run_once()
    assert rows(admin)[0][5:] == ('refused', 'requester_unauthorized') and outbox.requests == []
