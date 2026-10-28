"""NX-057 R1 (migration remediation_intents; docs/design/remediation-intents.md v4 §3, §5, §6.1, §9.2, §9.4, §9.5, §11): remediation
intents, suggestions only. Real PostgreSQL through the NX-030 fixture: the real configurator publishes the rules, a real domain-worker
service credential with eios:action:nexloop.remediation.plan:1 runs the planner's signed port, the owner and the operator log in to the
real workbench for the reads and the two owner Actions. Admin seeds synthetic effect intents (replica mode: the context and Run they
would reference are not seeded) and their R0 failure rows, and the effect category of the synthetic Action, as trusted configuration.
"""
import hashlib
import json
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from psycopg.types.json import Jsonb

from commercial_fixture import publish_registry_actions
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.remediation import (PLAN_ACTION, RemediationPlanner, RemediationPort, RemediationRulesRejected, apply_rules, load_rules,
                                      validate_rules)
from pathlib import Path
from test_browser_http import ORIGIN
from test_observability_pg import TENANT, observed  # noqa: F401
from support.single_clock import issued_once
from test_workbench_actions_http import act
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench, workbench_login  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
OPEN = {'REMEDIATION_ENABLED': 'true'}
SOURCE = 'synthetic.send'


def manifest(*rules, version=1, tenant_per_hour=20, consumer_per_day=3, plan_wakes_per_week=2, breaker_per_day=3):
    return {'schema_version': 'nexloop-remediation-rules/1', 'manifest_version': version, 'decision': 'synthetic test rules',
            'limits': {'tenant_per_hour': tenant_per_hour, 'consumer_per_day': consumer_per_day, 'plan_wakes_per_week': plan_wakes_per_week,
                       'breaker_per_day': breaker_per_day}, 'rules': list(rules)}


def rule(rule_id='synthetic_escalate', *, kinds=('origin_expired', 'confirmed_not_delivered'), mode='manual', version=1, kind='escalate', **extra):
    return {'rule_id': rule_id, 'source': {'stable_name': SOURCE, 'version': version}, 'failure_kinds': list(kinds), 'kind': kind, 'mode': mode,
            'due_after_seconds': 604800, 'purpose': 'synthetic', **extra}


def declare_source(admin, version=1):
    # The tenant declared the synthetic Action external (effect categories, 0153): trusted configuration seeded as admin.
    admin.execute("""insert into control.nexloop_action_effect_externality(tenant_id,world,resource_id,definition_digest,external_effect,manifest_version)
        values(%s,'real',%s,%s,true,1)""", (TENANT, f'eios:action:{SOURCE}:{version}', secrets.token_hex(32)))


def failed(admin, reason='origin_expired', *, consumer='consumer-synthetic', action_version=1, state='failed', claim=None):
    intent_id = uuid.uuid4()
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # synthetic row: its context and Run are not seeded
        admin.execute("""insert into runtime.nexloop_effect_intents(intent_id,receipt_id,context_id,tenant_id,world,consumer_id,goal_identity,
            plan_step_identity,slot_identity,action_name,action_version,business_digest,provider_payload_digest,frozen_request,action_definition,
            capability,executor_principal,control_revision,created_at,state,governed_claim_finalized) values(%s,%s,%s,%s,'real',%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,'executor-synthetic',1,clock_timestamp(),%s,false)""",
            (intent_id, uuid.uuid4(), uuid.uuid4(), TENANT, consumer, 'goal-' + intent_id.hex[:8], 'step-' + intent_id.hex[:8], 'slot-' + intent_id.hex[:8],
             SOURCE, action_version, 'a' * 64, 'b' * 64, Jsonb({}), Jsonb({}), Jsonb({}), state))
        if claim is not None:  # the intent's action claim as the effect executor's reservation leaves it (key: Action + intent id)
            admin.execute('insert into runtime.nexloop_action_claims(tenant_id,world,action_name,intent_id,principal_id,claim) values(%s,%s,%s,%s,%s,%s)',
                          (TENANT, 'real', SOURCE, str(intent_id), 'executor-synthetic', Jsonb(claim)))
    admin.execute("insert into runtime.nexloop_effect_failures(intent_id,tenant_id,world,reason_code,evidence) values(%s,%s,'real',%s,'{}')",
                  (intent_id, TENANT, reason))
    return intent_id


@pytest.fixture
def remediating(observed):  # noqa: F811
    o = observed
    admin, w = o['admin'], o['w']
    declare_source(admin)
    publish_registry_actions(admin, ('nexloop.remediation.resolve', 'nexloop.remediation.switch', 'nexloop.remediation.rule_release'), tenant=TENANT)
    session, token = seed_multi_authority(admin, o['domain'], [(PLAN_ACTION, ResourceType.ACTION, Operation.EXECUTE)], identity_suffix='-remediation')

    def plan(environ=OPEN):
        return RemediationPort(o['domain'], authenticate_service(o['domain'], token, world='real'), w['f']['reader'].signer, environ=environ).round()

    def rules(m):
        # G1: a publication names the revision it expects (the tenant's current rules version; test_remediation_rule_publications_pg).
        current = admin.execute("select coalesce(max(manifest_version),0) from control.nexloop_remediation_rules where tenant_id=%s and world='real'",
                                (TENANT,)).fetchone()[0]
        return apply_rules(m, TENANT, expected_revision=current, database_url_file=w['configurator'])
    yield dict(o, plan=plan, rules=rules, token=token)


def remediations(admin, intent=None):
    return admin.execute("""select source_intent_id,rule_id,kind,mode,state,last_code from runtime.nexloop_remediation_intents
        where tenant_id=%s and (%s::uuid is null or source_intent_id=%s) order by created_at""", (TENANT, intent, intent)).fetchall()


def test_the_rules_manifest_is_checked_fail_closed():
    v1 = load_rules(ROOT / 'deploy/configuration/remediation-rules.v1.json')
    assert v1['limits'] == {'tenant_per_hour': 20, 'consumer_per_day': 3, 'plan_wakes_per_week': 2, 'breaker_per_day': 3}  # Q-5
    # Owner 2026-10-25 (Q-1 A, Q-3 A): one rule, the one Action the effect categories declare external, both failures reevaluated.
    assert [(x['rule_id'], x['source'], x['failure_kinds'], x['kind'], x['mode']) for x in v1['rules']] == [
        ('deliver_final_failure', {'stable_name': 'nexloop.enterprise.deliver', 'version': 1}, ['origin_expired', 'confirmed_not_delivered'],
         'reevaluate', 'automatic')]
    categories = json.loads((ROOT / 'deploy/configuration/action-effect-categories.v3.json').read_text())
    assert {(a['stable_name'], a['version']) for a in categories['actions'] if a.get('external_effect')} == {('nexloop.enterprise.deliver', 1)}
    validate_rules(manifest(rule(kind='reevaluate', mode='automatic')))  # R2 opens reevaluate
    validate_rules(manifest(rule()))
    for bad, why in [
            (manifest(rule(kind='governed_action')), 'not available yet'),      # R3
            (manifest(rule(kinds=('refused_one_reply',))), 'rule'),             # only the two R0 reasons
            (manifest(rule(), rule('again')), 'duplicate'),                     # one rule per source version, kind and failure kind
            (manifest(rule(mode='later')), 'rule'),
            (manifest(rule(due_after_seconds=60)), 'rule'),
            (manifest(rule(), tenant_per_hour=0), 'limits')]:
        with pytest.raises(RemediationRulesRejected, match=why):
            validate_rules(bad)
    service = rule(); service['source'] = {'stable_name': 'nexloop.service.request', 'version': 1}
    with pytest.raises(RemediationRulesRejected, match='bound replies'):
        validate_rules(manifest(service))
    validate_rules(manifest({**service, 'exclude_bound_replies': True}))


def test_sql_publishes_rules_only_for_declared_effect_sources_and_versions_only_increase(remediating):
    r, admin = remediating, remediating['admin']
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='source_not_effect'):
        r['rules'](manifest(rule(version=2)))                                 # version 2 was never declared external
    assert r['rules'](manifest(rule()))['replay'] is False
    assert r['rules'](manifest(rule()))['replay'] is True                    # the same manifest again: a replay
    with pytest.raises(psycopg.errors.SerializationFailure, match='published with another text'):
        r['rules'](manifest(rule(mode='automatic')))                          # version 1 again with another text
    assert r['rules'](manifest(rule(), version=3))['replay'] is False        # versions may skip
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='version_must_increase'):
        r['rules'](manifest(rule(), version=2))                              # the current version is 3: 2 does not increase
    with pytest.raises(psycopg.Error, match='trusted configuration only'):
        admin.execute("select control.nexloop_configure_remediation_rules(%s,'real',%s)", (TENANT, Jsonb(manifest(rule(), version=9))))
    with pytest.raises(psycopg.Error), admin.transaction():                  # append-only
        admin.execute('delete from control.nexloop_remediation_rules')


def test_the_planner_proposes_once_per_failure_and_rule_and_only_for_eligible_failures(remediating):
    r, admin = remediating, remediating['admin']
    r['rules'](manifest(rule()))
    expired, confirmed = failed(admin, 'origin_expired'), failed(admin, 'confirmed_not_delivered')
    refused = failed(admin, 'refused_one_reply:already_answered')            # a rule's refusal is no defect: never remediated
    other = failed(admin, action_version=2)                                  # no rule for that exact version
    accepted = failed(admin, state='accepted')                               # a failure row whose intent is not failed
    out = r['plan']()
    assert out['status'] == 'open' and out['decisions'] == {'generated': 2, 'not_eligible': 1, 'no_rule': 1, 'not_failed': 1}, out
    assert [x[1:] for x in remediations(admin)] == [('synthetic_escalate', 'escalate', 'manual', 'proposed', 'proposed')] * 2
    assert {x[0] for x in remediations(admin)} == {expired, confirmed}
    assert not remediations(admin, refused) and not remediations(admin, other) and not remediations(admin, accepted)
    assert r['plan']()['decisions'] == {}                                    # each failure row is considered once
    events = admin.execute('select from_state,to_state,code,actor_kind from runtime.nexloop_remediation_events order by recorded_at').fetchall()
    assert events == [(None, 'proposed', 'proposed', 'service')] * 2
    for (remediation,) in admin.execute('select remediation_id from runtime.nexloop_remediation_intents where tenant_id=%s', (TENANT,)).fetchall():
        issued_once(admin, TENANT, 'runtime.nexloop_remediation_intents', 'remediation_id', remediation, timedelta(seconds=604800),
                    start='created_at', end='due_by', audit=('runtime.nexloop_remediation_events', 'recorded_at', 'remediation_id', remediation))


def test_an_automatic_escalation_goes_to_the_owner_at_once(remediating):
    r, admin = remediating, remediating['admin']
    r['rules'](manifest(rule(mode='automatic')))
    intent = failed(admin)
    assert r['plan']()['decisions'] == {'generated': 1}
    assert [x[4:] for x in remediations(admin, intent)] == [('escalated', 'escalated_automatic')]
    # Escalated is open: it counts as waiting and the owner closes it (L6 R1 review 2).
    admin.execute("update runtime.nexloop_remediation_intents set created_at=clock_timestamp()-interval '25 hours' where source_intent_id=%s", (intent,))
    assert admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'remediation'->'waiting_over_24h'", (TENANT,)).fetchone()[0] == 1
    with r['w']['client']() as client:
        csrf = workbench_login(client, r['w']['owner']).json()['csrf_token']
        rid = admin.execute('select remediation_id from runtime.nexloop_remediation_intents where source_intent_id=%s', (intent,)).fetchone()[0]
        done = act(client, csrf, 'resolve_remediation', {'remediation_id': str(rid), 'outcome': 'handled', 'reason': '已联系客户'})
        assert done.status_code == 200 and done.json()['state'] == 'handled', done.text
    assert [x[4:] for x in remediations(admin, intent)] == [('handled', 'marked_handled')]
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='illegal_transition'), admin.transaction():  # handled is terminal
        admin.execute("select runtime.nexloop_remediation_move(%s,'proposed','synthetic','service','synthetic',null)", (rid,))


def test_closed_switches_generate_nothing_now_or_later(remediating):
    r, admin = remediating, remediating['admin']
    r['rules'](manifest(rule()))
    first = failed(admin)
    assert r['plan'](environ={})['status'] == 'closed:deployment'           # REMEDIATION_ENABLED absent: closed
    assert r['plan'](environ={'REMEDIATION_ENABLED': 'yes'})['decisions'] == {}  # only the exact 'true' opens it
    assert r['plan']()['decisions'] == {}                                    # reopened: the failure of the closed time stays unremediated
    assert admin.execute('select decision from runtime.nexloop_remediation_scans where failure_id=(select failure_id from runtime.nexloop_effect_failures where intent_id=%s)',
                         (first,)).fetchone() == ('switch_closed',)
    # The tenant control pause (NXC01 scope) closes it too.
    admin.execute("insert into control.nexloop_control_scopes(tenant_id,world,scope_kind,scope_ref,paused,revision,reason) values(%s,'real','tenant','*',true,1,'synthetic')",
                  (TENANT,))
    failed(admin)
    assert r['plan']() == {'status': 'closed:control', 'expired': 0, 'decisions': {'switch_closed': 1}, 'held': []}
    with admin.transaction():  # synthetic: the pause row lifted (the control scopes are append-only for every writer)
        admin.execute('set local session_replication_role=replica')
        admin.execute("delete from control.nexloop_control_scopes where tenant_id=%s", (TENANT,))
    failed(admin)
    assert r['plan']()['decisions'] == {'generated': 1}
    assert len(remediations(admin)) == 1


def test_the_tenant_rate_limit_aggregates_and_the_consumer_cap_escalates_only(remediating):
    r, admin = remediating, remediating['admin']
    r['rules'](manifest(rule(), tenant_per_hour=2, consumer_per_day=1))
    a1, a2 = failed(admin, consumer='consumer-a'), failed(admin, consumer='consumer-a')  # the second of consumer a: past its cap
    failed(admin, consumer='consumer-b'), failed(admin, consumer='consumer-c')           # past the tenant's hourly limit
    out = r['plan']()
    assert out['decisions'] == {'generated': 2, 'rate_capped': 2}, out
    assert [x[4:] for x in remediations(admin, a1)] == [('proposed', 'proposed')]
    assert [x[2:] for x in remediations(admin, a2)] == [('escalate', 'manual', 'escalated', 'chain_capped')]
    # One aggregated row per hour carries the excess (S-2), no remediation per failure.
    assert admin.execute('select over_count from runtime.nexloop_remediation_rate_caps where tenant_id=%s', (TENANT,)).fetchall() == [(2,)]
    snapshot = admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'remediation'", (TENANT,)).fetchone()[0]
    assert snapshot['rate_capped_last_hour'] == 2 and snapshot['generated_last_hour'] == 2 and snapshot['by_state'] == {'proposed': 1, 'escalated': 1}


def test_a_due_proposal_expires_whatever_the_switches(remediating):
    r, admin = remediating, remediating['admin']
    r['rules'](manifest(rule()))
    intent = failed(admin)
    r['plan']()
    admin.execute("update runtime.nexloop_remediation_intents set due_by=clock_timestamp()-interval '1 second' where source_intent_id=%s", (intent,))
    assert r['plan'](environ={})['expired'] == 1
    assert [x[4:] for x in remediations(admin, intent)] == [('expired', 'due_by_passed')]


def test_the_planner_runs_once_a_minute_and_is_off_without_its_grant(remediating, monkeypatch):
    from nexloop_eios import remediation
    r = remediating
    clock = iter([1000.0, 1010.0, 1061.0])
    session = authenticate_service(r['domain'], r['token'], world='real')
    # What the 26th batch CI met: an earlier test of the same process left a later round in the process-wide state for this tenant
    # (planners of other tests, other clocks). A planner with its own state is not decided by it.
    monkeypatch.setitem(remediation._LAST_ROUND, (TENANT, 'real'), 10.0 ** 9)
    rounds = {}  # this test's own round state; the process-wide one belongs to the worker
    make = lambda: RemediationPlanner(r['domain'], session, r['w']['f']['reader'].signer, environ=OPEN, clock=lambda: next(clock), rounds=rounds)  # noqa: E731
    assert make().run_once()['status'] == 'open' and make().run_once() == {'status': 'not_due'} and make().run_once()['status'] == 'open'
    other, token = seed_multi_authority(r['admin'], r['domain'], [('eios:action:nexloop.alert.evaluate:1', ResourceType.ACTION, Operation.EXECUTE)],
                                        identity_suffix='-not-a-planner')
    assert RemediationPlanner(r['domain'], authenticate_service(r['domain'], token, world='real'), r['w']['f']['reader'].signer,
                              environ=OPEN, clock=lambda: 5000.0, rounds={}).run_once() == {'status': 'unavailable'}
    # Without its own state a planner uses the process's (the worker builds a new planner every tick).
    assert RemediationPlanner(r['domain'], session, r['w']['f']['reader'].signer).rounds is remediation._LAST_ROUND


def test_the_owner_resolves_and_switches_from_the_workbench_and_every_member_reads(remediating):
    r, admin, w = remediating, remediating['admin'], remediating['w']
    r['rules'](manifest(rule()))
    handled, cancelled = failed(admin), failed(admin)
    r['plan']()
    ids = dict(admin.execute('select source_intent_id,remediation_id from runtime.nexloop_remediation_intents').fetchall())
    with w['client']() as client:
        csrf = workbench_login(client, w['owner']).json()['csrf_token']
        listed = client.get('/api/v1/workbench/remediations', headers={'Origin': ORIGIN})
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert len(body['remediations']) == 2 and body['paused'] is False and body['hidden'] == 0
        assert {x['state'] for x in body['remediations']} == {'proposed'} and 'reason' not in str(body)
        done = act(client, csrf, 'resolve_remediation', {'remediation_id': str(ids[handled]), 'outcome': 'handled', 'reason': '已人工联系'})
        assert done.status_code == 200, done.text
        assert act(client, csrf, 'resolve_remediation', {'remediation_id': str(ids[cancelled]), 'outcome': 'cancelled', 'reason': '不需要'}).status_code == 200
        again = act(client, csrf, 'resolve_remediation', {'remediation_id': str(ids[handled]), 'outcome': 'cancelled', 'reason': '重复'})
        assert again.status_code == 409 and again.json()['details']['reason'] == 'not_open'
        assert act(client, csrf, 'resolve_remediation', {'remediation_id': str(ids[handled]), 'outcome': 'later', 'reason': 'x'}).status_code == 422
        paused = act(client, csrf, 'switch_remediation', {'paused': True, 'reason': '排查中'})
        assert paused.status_code == 200 and paused.json()['revision'] == 1
        assert client.get('/api/v1/workbench/remediations?state=declined', headers={'Origin': ORIGIN}).json()['remediations'][0]['state'] == 'declined'
        assert client.get('/api/v1/workbench/remediations?state=later', headers={'Origin': ORIGIN}).status_code == 422
    assert [x[4:] for x in remediations(admin, handled)] == [('handled', 'marked_handled')]
    assert [x[4:] for x in remediations(admin, cancelled)] == [('declined', 'cancelled')]
    assert admin.execute("select actor_kind,reason from runtime.nexloop_remediation_events where code='marked_handled'").fetchone() == ('human', '已人工联系')
    failed(admin)
    assert r['plan']()['status'] == 'closed:tenant'                          # paused by the owner: generates nothing
    assert admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'remediation'->'paused'", (TENANT,)).fetchone()[0] == 1
    with w['client']() as client:
        csrf = workbench_login(client, w['operator']).json()['csrf_token']
        assert client.get('/api/v1/workbench/remediations', headers={'Origin': ORIGIN}).status_code == 200  # every member reads
        assert act(client, csrf, 'switch_remediation', {'paused': False, 'reason': '恢复'}).status_code == 403  # the owner's only
    with w['client']() as client:
        assert workbench_login(client, w['customer']).status_code == 200
        assert client.get('/api/v1/workbench/remediations', headers={'Origin': ORIGIN}).status_code == 403


def test_application_roles_hold_nothing_on_the_remediation_tables(remediating):
    admin = remediating['admin']
    tables = ('runtime.nexloop_remediation_intents', 'runtime.nexloop_remediation_events', 'runtime.nexloop_remediation_scans',
              'runtime.nexloop_remediation_rate_caps', 'control.nexloop_remediation_switches', 'control.nexloop_remediation_rules',
              'control.nexloop_remediation_rule_holds', 'control.nexloop_remediation_rule_publications')
    for table in tables:
        assert admin.execute('select relforcerowsecurity from pg_class where oid=%s::regclass', (table,)).fetchone() == (True,)
        for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_runtime', 'nexloop_metrics'):
            assert admin.execute("select has_table_privilege(%s,%s,'select,insert,update,delete')", (role, table)).fetchone() == (False,)
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker'):  # the plan feed is touched only inside the definer wake (design §6.1)
        assert admin.execute("select has_function_privilege(%s,'authz.nexloop_plan_feed_touch(text,text,uuid,jsonb,timestamptz)','execute')",
                             (role,)).fetchone() == (False,)
    for role in ('nexloop_api', 'nexloop_action_worker', 'nexloop_scheduler'):
        assert admin.execute("select has_function_privilege(%s,'authz.nexloop_remediation_plan_command(text,text,text,text,text)','execute')",
                             (role,)).fetchone() == (False,)
    append_only = {r[0] for r in admin.execute("""select c.oid::regclass::text from pg_trigger g join pg_class c on c.oid=g.tgrelid
        where g.tgfoid='control.nexloop_nx022_append_only()'::regprocedure and c.relname like 'nexloop_remediation_%%'""").fetchall()}
    assert append_only == {'runtime.nexloop_remediation_events', 'runtime.nexloop_remediation_scans', 'control.nexloop_remediation_switches',
                           'control.nexloop_remediation_rules', 'control.nexloop_remediation_rule_publications'}


APPROVAL_RULES = ['approval_waiting', 'approval_unconsumed', 'approval_sweep_stale', 'approval_solo_mode', 'approval_solo_self_approved',
                  'approval_solo_eligibility_reduced']  # NX-056's v14 (L3), merged with batch 24


def test_alert_rules_v15_are_nx056_v14_plus_the_remediation_rules():
    from nexloop_eios.observability import load_rules as load_alert_rules
    v13 = json.loads((ROOT / 'deploy/configuration/alert-rules.v13.json').read_text())
    v15 = load_alert_rules(ROOT / 'deploy/configuration/alert-rules.v15.json')
    assert v15['version'] == 15 and v15['rules'][:len(v13['rules'])] == v13['rules']
    assert [x['rule_id'] for x in v15['rules'][61:67]] == APPROVAL_RULES
    v14 = ROOT / 'deploy/configuration/alert-rules.v14.json'
    if v14.exists():  # once batch 24 is merged: a superset of NX-056's v14, rule for rule
        assert v15['rules'][:67] == json.loads(v14.read_text())['rules']
    assert [(x['rule_id'], x['metric'], x['severity']) for x in v15['rules'][67:]] == [
        ('remediation_waiting', 'remediation.waiting_over_24h', 'warning'), ('remediation_rate_capped', 'remediation.rate_capped_last_hour', 'warning'),
        ('remediation_paused', 'remediation.paused', 'warning'), ('remediation_rule_held', 'remediation.held_rules', 'critical')]


# ---- R2: reevaluate wakes the waiting plan; origin, plan cap and breaker -------------------------------------------------------------
def plan_for(admin, intent, *, consumer='c' * 64):
    """An active plan whose current version's step names the intent (the plan the NX-024 observation trigger would wake), seeded as
    its port leaves it (replica mode: the goal and Context it references are not seeded)."""
    plan = uuid.uuid4()
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into runtime.nexloop_plans(tenant_id,world,plan_id,version,consumer_id,goal_version_ref,context_strategy_ref,recipe,settings,
            control_snapshot,created_by) values(%s,'real',%s,1,%s,'goal:synthetic@1','context-strategy:synthetic@1','{}','{}','{}','synthetic')""",
            (TENANT, plan, consumer))
        admin.execute("insert into runtime.nexloop_plan_events(tenant_id,world,plan_id,version,status,reason) values(%s,'real',%s,1,'active','synthetic')",
                      (TENANT, plan))
        admin.execute("""insert into runtime.nexloop_plan_steps(tenant_id,world,plan_id,version,step_key,prerequisites,expected_result,stop_if,budget,intent_ref)
            values(%s,'real',%s,1,'deliver','[]','delivered','[]','{}',%s)""", (TENANT, plan, intent))
    return plan


def triggers(admin, plan):
    row = admin.execute("select payload->'triggers' from runtime.nexloop_work_feed where tenant_id=%s and feed='plan-reevaluate' and item_key=%s",
                        (TENANT, f'plan:{plan}')).fetchone()
    return row[0] if row else None


def test_rules_open_reevaluate_and_still_refuse_governed_action(remediating):
    r = remediating
    assert r['rules'](manifest(rule(kind='reevaluate', mode='automatic')))['replay'] is False
    with pytest.raises(RemediationRulesRejected, match='not available yet'):
        validate_rules(manifest(rule(kind='governed_action')))
    text = json.dumps(manifest(rule(kind='governed_action'), version=2))   # past the Python check: SQL refuses it too (G1 entry)
    with psycopg.connect(Path(r['w']['configurator']).read_text().strip()) as db, pytest.raises(psycopg.errors.InvalidParameterValue, match='kind_unavailable'):
        db.execute("select control.nexloop_publish_remediation_rules(%s,'real',%s,%s,1)", (TENANT, text, hashlib.sha256(text.encode()).hexdigest()))


def test_an_automatic_reevaluate_wakes_the_waiting_plan_once_as_a_self_trigger(remediating):
    r, admin = remediating, remediating['admin']
    r['rules'](manifest(rule(kind='reevaluate', mode='automatic')))
    intent = failed(admin)
    plan = plan_for(admin, intent)
    intents_before = admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()[0]
    assert r['plan']()['decisions'] == {'generated': 1}
    row = admin.execute('select state,last_code,plan_id,woken_at is not null from runtime.nexloop_remediation_intents where source_intent_id=%s',
                        (intent,)).fetchone()
    assert row == ('succeeded', 'woken', plan, True)
    (trigger,) = triggers(admin, plan)
    assert trigger['kind'] == 'final_failure' and trigger['cause'] == 'self' and trigger['intent_id'] == str(intent)
    assert trigger['failure_kind'] == 'origin_expired' and 'remediation_id' in trigger
    # A wake is no effect: no intent was added; the reevaluation Run decides what to do, and its intents meet every dispatch check.
    assert admin.execute('select count(*) from runtime.nexloop_effect_intents').fetchone()[0] == intents_before
    # Without a waiting plan there is nothing to wake.
    lonely = failed(admin)
    r['plan']()
    assert [x[4:] for x in remediations(admin, lonely)] == [('not_applicable', 'no_plan')]


def test_the_plan_cap_escalates_instead_of_waking_again(remediating):
    r, admin = remediating, remediating['admin']
    r['rules'](manifest(rule(kind='reevaluate', mode='automatic'), plan_wakes_per_week=1, consumer_per_day=9, breaker_per_day=1))
    first, second = failed(admin), failed(admin)
    plan = plan_for(admin, first)
    admin.execute("""insert into runtime.nexloop_plan_steps(tenant_id,world,plan_id,version,step_key,prerequisites,expected_result,stop_if,budget,intent_ref)
        values(%s,'real',%s,1,'again','[]','delivered','[]','{}',%s)""", (TENANT, plan, second))
    out = r['plan']()
    assert [x[4:] for x in remediations(admin, first)] == [('succeeded', 'woken')]
    assert [x[4:] for x in remediations(admin, second)] == [('escalated', 'chain_capped')]   # one wake of that plan in 7 days
    assert len(triggers(admin, plan)) == 1
    # The capped escalation is counted by its event (not the row's last code): with breaker_per_day 1 the rule is held.
    assert out['held'] == ['synthetic_escalate']
    assert admin.execute('select reason_code,observed from control.nexloop_remediation_rule_holds').fetchall() == [('chain_capped', 1)]


def test_a_run_consumes_the_wake_and_its_failed_intents_only_escalate_then_the_breaker_holds_the_rule(remediating):
    """Origin by the remediation table (L6 K-2): the Run started for the plan consumes its wakes; an intent of that Run that fails is an
    escalation only; past breaker_per_day such failures the rule is held until the owner releases it (design §9.3)."""
    r, admin, w = remediating, remediating['admin'], remediating['w']
    r['rules'](manifest(rule(kind='reevaluate', mode='automatic'), breaker_per_day=1, consumer_per_day=9))
    source = failed(admin)
    plan = plan_for(admin, source)
    r['plan']()
    run = uuid.uuid4()
    admin.execute("insert into runtime.nexloop_plan_runs(tenant_id,world,run_id,plan_id,version,triggers) values(%s,'real',%s,%s,1,%s)",
                  (TENANT, run, plan, Jsonb(triggers(admin, plan))))
    assert admin.execute('select consumed_by_run from runtime.nexloop_remediation_intents where source_intent_id=%s', (source,)).fetchone() == (run,)
    # The Run's own intent fails (its origin Run is the remediation Run).
    child = failed(admin)
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute('update runtime.nexloop_effect_intents set origin_run_id=%s where intent_id=%s', (run, child))
    plan_for(admin, child)
    out = r['plan']()
    assert out['held'] == ['synthetic_escalate'], out
    row = admin.execute('select kind,state,last_code,origin_remediation_id is not null from runtime.nexloop_remediation_intents where source_intent_id=%s',
                        (child,)).fetchone()
    assert row == ('escalate', 'escalated', 'remediation_origin', True)
    held = failed(admin)
    plan_for(admin, held)
    assert r['plan']()['decisions'] == {'rule_held': 1} and not remediations(admin, held)
    assert admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'remediation'->'held_rules'", (TENANT,)).fetchone()[0] == 1
    with w['client']() as client:
        csrf = workbench_login(client, w['owner']).json()['csrf_token']
        listed = client.get('/api/v1/workbench/remediations', headers={'Origin': ORIGIN}).json()
        assert [h['rule_id'] for h in listed['held_rules']] == ['synthetic_escalate']
        released = act(client, csrf, 'release_remediation_rule', {'rule_id': 'synthetic_escalate', 'reason': '已排查'})
        assert released.status_code == 200, released.text
        again = act(client, csrf, 'release_remediation_rule', {'rule_id': 'synthetic_escalate', 'reason': '再次'})
        assert again.status_code == 409 and again.json()['details']['reason'] == 'not_held'
    # After the release the counts start again: two rounds without a new failure hold nothing (L6 R2 review).
    assert r['plan']()['held'] == [] and r['plan']()['held'] == []
    plan_for(admin, failed(admin))
    out = r['plan']()
    assert out['decisions'] == {'generated': 1} and out['held'] == []


def test_the_owner_wakes_a_manual_reevaluate_and_cannot_wake_an_escalation(remediating):
    r, admin, w = remediating, remediating['admin'], remediating['w']
    r['rules'](manifest(rule(kind='reevaluate', mode='manual'), rule('synthetic_handover', kinds=('confirmed_not_delivered',))))
    intent = failed(admin)
    plan = plan_for(admin, intent)
    other = failed(admin, 'confirmed_not_delivered')
    r['plan']()
    ids = {(x[0], x[1]): x for x in admin.execute('select source_intent_id,kind,remediation_id from runtime.nexloop_remediation_intents').fetchall()}
    wake = ids[(intent, 'reevaluate')][2]
    escalation = next(x[2] for k, x in ids.items() if k[0] == other and k[1] == 'escalate')
    with w['client']() as client:
        csrf = workbench_login(client, w['owner']).json()['csrf_token']
        done = act(client, csrf, 'resolve_remediation', {'remediation_id': str(wake), 'outcome': 'wake', 'reason': '确认唤醒'})
        assert done.status_code == 200 and done.json()['state'] == 'succeeded', done.text
        refused = act(client, csrf, 'resolve_remediation', {'remediation_id': str(escalation), 'outcome': 'wake', 'reason': '误操作'})
        assert refused.status_code == 409 and refused.json()['details']['reason'] == 'not_reevaluate'
    assert triggers(admin, plan)[0]['cause'] == 'self'
    assert admin.execute("select actor_kind,code from runtime.nexloop_remediation_events where remediation_id=%s and to_state='succeeded'",
                         (wake,)).fetchone() == ('human', 'woken')


# ---- L6 R1 review: one round per tenant, the 5 s statement bound, errors stay in the round ----------------------------------------
def test_a_second_planner_of_the_tenant_answers_busy(remediating, pg):
    r = remediating
    r['rules'](manifest(rule()))
    failed(r['admin'])
    with psycopg.connect(pg) as other:  # another planner of the tenant holds the round
        other.execute("select pg_advisory_xact_lock(hashtext(%s))", (f'nexloop-remediation-plan:{TENANT}:real',))
        assert r['plan']() == {'status': 'busy'}
    assert r['plan']()['decisions'] == {'generated': 1}


def test_the_port_bounds_the_round_at_5_seconds_and_the_planner_reports_it(remediating):
    r, admin = remediating, remediating['admin']
    # A synthetic command that outlasts the bound, in place of the planner's (admin installs it for this test and drops it).
    admin.execute("create function authz.nexloop_synthetic_slow_plan(text,text,text,text,text) returns jsonb language plpgsql as $$"
                  " begin perform pg_sleep(8); return '{}'::jsonb; end $$")
    admin.execute('grant execute on function authz.nexloop_synthetic_slow_plan(text,text,text,text,text) to nexloop_domain_worker')

    class Slow(RemediationPort):
        FUNCTION = 'nexloop_synthetic_slow_plan'
    session = authenticate_service(r['domain'], r['token'], world='real')
    started = time.monotonic()
    with pytest.raises(psycopg.errors.QueryCanceled):
        Slow(r['domain'], session, r['w']['f']['reader'].signer, environ=OPEN).round()
    assert 4.5 < time.monotonic() - started < 7.5
    planner = RemediationPlanner(r['domain'], session, r['w']['f']['reader'].signer, environ=OPEN, clock=lambda: 9000.0, rounds={})
    planner.port = Slow(r['domain'], session, r['w']['f']['reader'].signer, environ=OPEN)
    try:
        assert planner.run_once() == {'status': 'error', 'code': '57014'}  # the round ends, the reevaluator's tick does not
    finally:
        admin.execute('drop function authz.nexloop_synthetic_slow_plan(text,text,text,text,text)')


# ---- R4: the action claim of a final failure, outcomes.remediation, the evidence chain ------------------------------------------------
ACTIVE = {'state': 'active', 'claim_revision': 2, 'fencing_token': 'f' * 48, 'lease_expires_at': '2026-10-25T00:00:00+00:00', 'terminal_outcome': None}


def claim_of(admin, intent):
    return admin.execute('select claim from runtime.nexloop_action_claims where intent_id=%s', (str(intent),)).fetchone()[0]


def test_a_final_failure_ends_its_action_claim_permanent_failure_and_a_remediation_does_not_change_it(remediating):
    """Design §10 R4 and owner Q-4 A: in the failure's own transaction the claim ends terminal permanent_failure (outcome = the failure
    row); a remediation that wakes the plan afterwards leaves it so (no compensated); an intent never reserved has no claim."""
    r, admin = remediating, remediating['admin']
    r['rules'](manifest(rule(kind='reevaluate', mode='automatic')))
    intent = failed(admin, claim=ACTIVE)
    failure = admin.execute('select failure_id::text,recorded_at from runtime.nexloop_effect_failures where intent_id=%s', (intent,)).fetchone()
    c = claim_of(admin, intent)
    assert c['state'] == 'terminal' and c['claim_revision'] == 2 and c['fencing_token'] == 'f' * 48
    assert c['terminal_outcome']['status'] == 'permanent_failure' and c['terminal_outcome']['outcome_id'] == failure[0]
    assert c['terminal_outcome']['outcome_revision'] == 1 and len(c['terminal_outcome']['outcome_digest']) == 64
    plan_for(admin, intent)
    assert r['plan']()['decisions'] == {'generated': 1}
    assert [x[4:] for x in remediations(admin, intent)] == [('succeeded', 'woken')]
    assert claim_of(admin, intent) == c                                      # still permanent_failure: never compensated
    # A terminal claim stays as it is; an intent without a claim fails as before.
    done = {**ACTIVE, 'state': 'terminal', 'terminal_outcome': {'outcome_id': 'x', 'outcome_revision': 1, 'status': 'succeeded',
                                                                 'outcome_digest': 'e' * 64, 'finalized_at': '2026-10-25T00:00:00+00:00'}}
    kept = failed(admin, claim=done)
    assert claim_of(admin, kept) == done
    bare = failed(admin)
    assert admin.execute('select count(*) from runtime.nexloop_action_claims where intent_id=%s', (str(bare),)).fetchone() == (0,)


def test_outcomes_report_remediation_changes_and_the_defects_left_unremediated(remediating):
    r, admin = remediating, remediating['admin']
    r['rules'](manifest(rule(kinds=('confirmed_not_delivered',))))
    covered = failed(admin, 'confirmed_not_delivered')                       # a rule: proposed
    uncovered = failed(admin, 'origin_expired')                              # no rule for this kind
    failed(admin, 'refused_one_reply:already_answered')                      # no defect: counted under its refusal
    r['plan']()
    waiting = failed(admin, 'confirmed_not_delivered')                       # not considered yet (no round since)
    block = admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'outcomes'->'remediation'", (TENANT,)).fetchone()[0]
    assert block['window_seconds'] == 3600 and block['changes'] == {'proposed': 1}
    assert block['unremediated_defects'] == 2 and block['unremediated_by_decision'] == {'no_rule': 1, 'not_considered': 1}
    assert remediations(admin, covered) and not remediations(admin, uncovered) and not remediations(admin, waiting)
    # The failure rate is untouched by remediation (design §8): all three defects of the hour count, remediated or not; the refused
    # one-reply does not (it counts under its refusal).
    outcomes = admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'outcomes'->'classes'", (TENANT,)).fetchone()[0]
    assert int(outcomes['defect']) == 3


def test_the_workbench_shows_a_remediation_evidence_chain_without_reason_text(remediating):
    r, admin, w = remediating, remediating['admin'], remediating['w']
    r['rules'](manifest(rule(kind='reevaluate', mode='automatic')))
    intent = failed(admin, claim=ACTIVE)
    plan = plan_for(admin, intent)
    r['plan']()
    rid = admin.execute('select remediation_id from runtime.nexloop_remediation_intents where source_intent_id=%s', (intent,)).fetchone()[0]
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200    # every member reads
        got = client.get(f'/api/v1/workbench/remediations/{rid}', headers={'Origin': ORIGIN})
        assert got.status_code == 200, got.text
        e = got.json()
        assert e['source_intent']['intent_id'] == str(intent) and e['source_intent']['consumer_id'] is None  # not a readable Consumer
        assert e['failure']['reason_code'] == 'origin_expired' and e['claim'] == {'state': 'terminal', 'status': 'permanent_failure',
                                                                                  'finalized_at': e['claim']['finalized_at']}
        assert e['rule']['rule_id'] == 'synthetic_escalate' and e['plan']['plan_id'] == str(plan) and e['plan']['consumed_by_run'] is None
        assert [(x['from_state'], x['to_state'], x['code']) for x in e['events']] == [(None, 'proposed', 'proposed'), ('proposed', 'succeeded', 'woken')]
        assert 'reason' not in {k for x in e['events'] for k in x}
        assert client.get(f'/api/v1/workbench/remediations/{uuid.uuid4()}', headers={'Origin': ORIGIN}).status_code == 404
        assert client.get('/api/v1/workbench/remediations/not-an-id', headers={'Origin': ORIGIN}).status_code == 422
    with w['client']() as client:
        assert workbench_login(client, w['customer']).status_code == 200
        assert client.get(f'/api/v1/workbench/remediations/{rid}', headers={'Origin': ORIGIN}).status_code == 403
