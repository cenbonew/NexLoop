"""Stale action claims (dispatcher 2026-10-31, migration action_claim_settle, alert rules v26): real PostgreSQL and the real workbench.
A world-real action claim that stayed active more than an hour past its lease is counted in the metrics snapshot and fires
action_claim_stale (an effect claim also action_claim_stale_effect); the owner ends it through nexloop.action_claim.settle only when its
result is settled - uncommitted (adapter nexloop.core / target postgres, nothing written under the intent) or confirmed_not_delivered
(the owner's 0210 determination on a not_found observation). The claim ends permanent_failure with an append-only settlement and a later
reserve replays it; operators, a fresh lease, the wrong class and an unconfirmed effect are refused with a code. Synthetic data only."""
import io
import json
import secrets
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from commercial_fixture import publish_registry_actions
from nexloop_eios.observability import load_rules
from support.tenants import TENANT_A as TENANT
from test_workbench_actions_http import act
from test_workbench_read_pg import (browser_business, conversations, identity, provision, published_action, uow, workbench,  # noqa: F401
                                    workbench_login)

ACTION = 'nexloop.action_claim.settle'
RULES = Path(__file__).resolve().parents[1] / 'deploy/configuration'


def stale_claim(admin, tenant, action, intent, *, target='postgres', adapter='nexloop.core', lease="clock_timestamp()-interval '2 hours'"):
    """A claim as 0007 leaves it after a reserve whose execution never committed (lease on the database clock)."""
    lease_at = admin.execute(f'select {lease}').fetchone()[0]
    claim = {'key': {'tenant_id': tenant, 'action_stable_name': action, 'idempotency_key': intent},
             'binding': {'invocation_id': intent, 'action_reference': {'stable_name': action, 'version': 1}, 'request_digest': 'a' * 64,
                         'capability_binding': {}, 'adapter_id': adapter, 'target_system': target},
             'state': 'active', 'claim_revision': 4, 'fencing_token': secrets.token_hex(24), 'lease_expires_at': lease_at.isoformat(),
             'terminal_outcome': None}
    admin.execute('insert into runtime.nexloop_action_claims values(%s,%s,%s,%s,%s,%s)', (tenant, 'real', action, intent, 'synthetic-relay', Jsonb(claim)))


def claim_of(admin, action, intent):
    return admin.execute('select claim from runtime.nexloop_action_claims where action_name=%s and intent_id=%s', (action, intent)).fetchone()[0]


def health(admin, tenant=TENANT):
    return admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'action_claims'", (tenant,)).fetchone()[0]


def fired(snapshot):
    out = set()
    for r in load_rules(RULES / 'alert-rules.v26.json')['rules']:
        head, _, path = r['metric'].partition('.')
        if head != 'action_claims':
            continue
        value = snapshot[path] if 'selector' not in r else snapshot[path][r['selector']]
        if value > r['threshold']:
            out.add(r['rule_id'])
    return out


def settle(client, csrf, action, intent, category, *, reason='负责人决定：结束过期认领', key=None):
    return act(client, csrf, 'settle_action_claim', {'claim_action': action, 'intent_id': intent, 'category': category, 'reason': reason}, key=key)


@pytest.fixture
def desk(workbench):
    w = workbench
    publish_registry_actions(w['admin'], (ACTION,), tenant=TENANT)
    provision(w)
    return w


def test_v26_adds_exactly_the_two_stale_claim_rules_on_v24():
    v24, v26 = json.loads((RULES / 'alert-rules.v24.json').read_text()), load_rules(RULES / 'alert-rules.v26.json')
    assert v26['version'] == 26 and len(v26['rules']) == len(v24['rules']) + 2 and v26['rules'][:len(v24['rules'])] == v24['rules']
    stale, effect = v26['rules'][len(v24['rules']):]
    assert (stale['rule_id'], stale['metric'], stale['threshold'], stale['severity']) == (
        'action_claim_stale', 'action_claims.stale_active_oldest_age_seconds', 3600, 'warning')
    assert (effect['rule_id'], effect['metric'], effect['selector'], effect['threshold'], effect['for_seconds'], effect['severity']) == (
        'action_claim_stale_effect', 'action_claims.stale_active_by_class', 'effect', 0, 900, 'critical')


def test_the_owner_ends_an_uncommitted_claim_once_and_the_metric_clears(desk):
    w = desk; admin = w['admin']
    zero = {'stale_active': 0, 'stale_active_oldest_age_seconds': 0, 'stale_active_by_class': {'postgres': 0, 'effect': 0}}
    assert health(admin) == zero and fired(health(admin)) == set()  # a visible zero
    intent, fresh = 'message-relay-' + secrets.token_hex(32), 'message-relay-' + secrets.token_hex(32)
    stale_claim(admin, TENANT, 'Goal.create', intent)
    stale_claim(admin, TENANT, 'Goal.create', fresh, lease="clock_timestamp()-interval '10 minutes'")  # stale for the metric, not yet settleable
    snap = health(admin)
    assert snap['stale_active'] == 2 and snap['stale_active_by_class'] == {'postgres': 2, 'effect': 0}
    assert 7100 < snap['stale_active_oldest_age_seconds'] < 7300 and fired(snap) == {'action_claim_stale'}
    assert admin.execute("select runtime.nexloop_metrics_snapshot(%s,'test') ? 'action_claims'", (TENANT,)).fetchone()[0] is False
    with w['client']() as client:
        csrf = workbench_login(client, w['operator']).json()['csrf_token']
        assert settle(client, csrf, 'Goal.create', intent, 'uncommitted').status_code == 403
    assert claim_of(admin, 'Goal.create', intent)['state'] == 'active'
    with w['client']() as client:
        csrf = workbench_login(client, w['owner']).json()['csrf_token']
        refused = settle(client, csrf, 'Goal.create', fresh, 'uncommitted')
        assert refused.status_code == 409 and refused.json()['details'] == {'reason': 'not_stale'}
        wrong = settle(client, csrf, 'Goal.create', intent, 'confirmed_not_delivered')
        assert wrong.status_code == 409 and wrong.json()['details'] == {'reason': 'wrong_class'}
        missing = settle(client, csrf, 'Goal.create', 'message-relay-' + secrets.token_hex(32), 'uncommitted')
        assert missing.status_code == 409 and missing.json()['details'] == {'reason': 'not_found'}
        for bad in ({'claim_action': 'Goal.create', 'intent_id': intent, 'category': 'abandoned', 'reason': 'x'},
                    {'claim_action': 'Goal.create', 'intent_id': intent, 'category': 'uncommitted'},
                    {'claim_action': 'Goal create', 'intent_id': intent, 'category': 'uncommitted', 'reason': 'x'},
                    {'claim_action': 'Goal.create', 'intent_id': intent, 'category': 'uncommitted', 'reason': 'x', 'tenant_id': 'other'}):
            assert act(client, csrf, 'settle_action_claim', bad).status_code == 422, bad
        done = settle(client, csrf, 'Goal.create', intent, 'uncommitted', key='claim-settle-test-0001')
        assert done.status_code == 200, done.text
        body = done.json()
        assert body['replayed'] is False and body['category'] == 'uncommitted' and body['claim_revision'] == 4
        assert settle(client, csrf, 'Goal.create', intent, 'uncommitted', key='claim-settle-test-0001').json()['replayed'] is True
        # Another request for the same claim replays the settlement; another category is refused.
        assert settle(client, csrf, 'Goal.create', intent, 'uncommitted').json()['replayed'] is True
        other = settle(client, csrf, 'Goal.create', intent, 'confirmed_not_delivered')
        assert other.status_code == 409 and other.json()['details'] == {'reason': 'already_settled'}
    claim = claim_of(admin, 'Goal.create', intent)
    assert claim['state'] == 'terminal' and claim['claim_revision'] == 4
    assert claim['terminal_outcome']['status'] == 'permanent_failure' and claim['terminal_outcome']['outcome_id'] == str(body['settlement_id'])
    row = admin.execute('select category,claim_revision,evidence,reason,actor_principal,request_intent is not null from runtime.nexloop_action_claim_settlements '
                        'where intent_id=%s', (intent,)).fetchone()
    assert row[:2] == ('uncommitted', 4) and row[2]['target_system'] == 'postgres' and row[2]['objects_under_intent'] == 0
    assert row[3:] == ('负责人决定：结束过期认领', w['owner']['principal_id'], True)
    assert health(admin)['stale_active'] == 1  # only the fresh one is left
    with pytest.raises(psycopg.Error), admin.transaction():  # append-only
        admin.execute("update runtime.nexloop_action_claim_settlements set reason='x' where intent_id=%s", (intent,))
    for role in ('nexloop_api', 'nexloop_domain_worker', 'nexloop_action_worker', 'nexloop_scheduler', 'nexloop_configurator'):
        assert admin.execute("select has_table_privilege(%s,'runtime.nexloop_action_claim_settlements','select')", (role,)).fetchone() == (False,)
        assert admin.execute("select has_function_privilege(%s,'runtime.nexloop_governed_settle_action_claim(text,text,text,text,text,jsonb)','execute')",
                             (role,)).fetchone() == (False,)


def test_an_effect_claim_is_not_uncommitted_and_counts_as_effect(desk):
    w = desk; admin = w['admin']
    intent = str(uuid.uuid4())
    stale_claim(admin, TENANT, 'nexloop.service.request', intent, target='service', adapter='nexloop.effect')
    assert health(admin)['stale_active_by_class'] == {'postgres': 0, 'effect': 1}
    assert fired(health(admin)) == {'action_claim_stale', 'action_claim_stale_effect'}
    with w['client']() as client:
        csrf = workbench_login(client, w['owner']).json()['csrf_token']
        assert settle(client, csrf, 'nexloop.service.request', intent, 'uncommitted').json()['details'] == {'reason': 'wrong_class'}
        # No effect intent (or one not failed by the owner's determination): never ended here, reconcile first.
        assert settle(client, csrf, 'nexloop.service.request', intent, 'confirmed_not_delivered').json()['details'] == {'reason': 'intent_not_failed'}
    assert claim_of(admin, 'nexloop.service.request', intent)['state'] == 'active'


def test_an_object_written_under_the_intent_is_refused(desk):
    w = desk; admin = w['admin']
    intent = 'message-relay-' + secrets.token_hex(32)
    stale_claim(admin, TENANT, 'Goal.create', intent)
    code = "select runtime.nexloop_action_claim_settle_check(%s,'real','Goal.create',%s,'uncommitted')"
    assert admin.execute(code, (TENANT, intent)).fetchone()[0] == 'eligible'
    with admin.transaction():  # fault injection: an object under the intent (0010 would have made the claim terminal with it)
        admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
        admin.execute('alter table ontology.objects disable trigger user')
        now = admin.execute('select clock_timestamp()').fetchone()[0]
        values = {'tenant_id': TENANT, 'world': 'real', 'type_name': 'Goal', 'object_id': secrets.token_hex(32), 'schema_version': 1,
                  'properties': Jsonb({}), 'source_system': 'nexloop-action', 'source_ref': intent, 'created_at': now, 'updated_at': now}
        admin.execute(f"insert into ontology.objects({','.join(values)}) values({','.join(['%s'] * len(values))})", list(values.values()))
        assert admin.execute(code, (TENANT, intent)).fetchone()[0] == 'object_exists'
        raise psycopg.Rollback()


def test_the_owner_ends_claims_with_one_command_and_a_rerun_replays(desk, tmp_path, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/deploy'))
    import action_claim_settle as T
    from test_browser_http import ORIGIN
    from test_real_dispatch_switch_tool import AppTransport, private
    w = desk; admin = w['admin']
    intents = ['message-relay-' + secrets.token_hex(32) for _ in range(2)]
    for intent in intents:
        stale_claim(admin, TENANT, 'Goal.create', intent)
    claims = [{'action_name': 'Goal.create', 'intent_id': i, 'category': 'uncommitted'} for i in intents]
    cfg = private(tmp_path, 'settle.json', json.dumps({'schema': T.SCHEMA, 'confirmation_ref': 'stale-claims:test-1', 'reason': '结束过期认领', 'claims': claims}))
    user = private(tmp_path, 'user', w['owner']['username'] + '\n')

    def run(*extra):
        with w['client']() as client:
            code = T.main(['--origin', ORIGIN, '--password-stdin', '--username-file', str(user), '--config', str(cfg), *extra],
                          stdin=io.StringIO(w['owner']['password'] + '\n'), transport=AppTransport(client))
        return code, json.loads(capsys.readouterr().out)
    code, dry = run('--dry-run')
    assert code == 0 and len(dry['claims']) == 2 and {claim_of(admin, 'Goal.create', i)['state'] for i in intents} == {'active'}
    code, out = run()
    assert code == 0 and [c['result'] for c in out['claims']] == ['settled', 'settled']
    assert {claim_of(admin, 'Goal.create', i)['state'] for i in intents} == {'terminal'}
    text = json.dumps(out, ensure_ascii=False)
    assert w['owner']['password'] not in text and '结束过期认领' not in text
    code, again = run()
    assert code == 0 and [c['result'] for c in again['claims']] == ['replayed', 'replayed']


def test_the_example_is_valid_and_bad_configurations_are_refused():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/deploy'))
    import action_claim_settle as T
    example = (Path(__file__).resolve().parents[1] / 'deploy/examples/action-claim-settle.example.json').read_text()
    assert len(T.load_config(example)['claims']) == 2
    good = json.loads(example)
    for bad in ({**good, 'claims': []}, {**good, 'claims': good['claims'] * 2}, {**good, 'extra': 1},
                {**good, 'claims': [{**good['claims'][0], 'category': 'abandoned'}]}, {**good, 'confirmation_ref': ' x'}):
        with pytest.raises(T.Refused):
            T.load_config(json.dumps(bad))


from effect_execution_fixture import governed_effect_executor, execution_plan  # noqa: E402,F401
from test_effect_confirm_not_delivered_pg import PLAN, commitments, observe, owner_desk, unknown_intent  # noqa: E402,F401


@PLAN
def test_an_effect_the_owner_confirmed_not_delivered_ends_and_its_reply_was_never_shown(owner_desk, admin, governed_effect_executor):
    """The stage case: 0210 failed the intent before 0325/0345 existed, so its claim stayed active (rebuilt here by inserting the claim
    after the determination; 0345 only closes a claim on a new failure row)."""
    c = owner_desk;c['executor_fixture'] = governed_effect_executor
    publish_registry_actions(admin, (ACTION,))
    intent, tenant = unknown_intent(c, admin)
    observe(admin, intent, tenant, 'not_found', 30)
    owner = c['owner']()
    action = admin.execute('select action_name from runtime.nexloop_effect_intents where intent_id=%s', (intent,)).fetchone()[0]
    stale_claim(admin, tenant, action, intent, target='service', adapter='nexloop.effect')
    early = act(owner.client, owner.csrf, 'settle_action_claim', {'claim_action': action, 'intent_id': intent, 'category': 'confirmed_not_delivered', 'reason': '未确认'})
    assert early.status_code == 409 and early.json()['details'] == {'reason': 'intent_not_failed'}  # unknown: reconcile first
    # Today 0345 closes a reserved claim with the determination's failure row; the stage claims predate it, so the claim is put back after.
    admin.execute('delete from runtime.nexloop_action_claims where intent_id=%s', (intent,))
    assert owner.confirm(intent).status_code == 200
    stale_claim(admin, tenant, action, intent, target='service', adapter='nexloop.effect')
    determination = admin.execute('select determination_id from runtime.nexloop_effect_determinations where intent_id=%s', (intent,)).fetchone()[0]
    assert claim_of(admin, action, intent)['state'] == 'active' and health(admin, tenant)['stale_active_by_class']['effect'] == 1
    sent = admin.execute('select count(*),max(attempt_revision) from runtime.nexloop_effect_attempts where intent_id=%s', (intent,)).fetchone()
    done = act(owner.client, owner.csrf, 'settle_action_claim', {'claim_action': action, 'intent_id': intent, 'category': 'confirmed_not_delivered',
                                                                 'reason': '10-18 已确认未送达'})
    assert done.status_code == 200, done.text
    claim = claim_of(admin, action, intent)
    assert claim['state'] == 'terminal' and claim['terminal_outcome']['status'] == 'permanent_failure'
    evidence = admin.execute('select evidence from runtime.nexloop_action_claim_settlements where intent_id=%s', (intent,)).fetchone()[0]
    assert evidence['determination_id'] == str(determination) and evidence['intent_state'] == 'failed'
    assert admin.execute('select count(*),max(attempt_revision) from runtime.nexloop_effect_attempts where intent_id=%s', (intent,)).fetchone() == sent  # nothing sent
    assert health(admin, tenant)['stale_active'] == 0


@PLAN
def test_a_provider_acceptance_blocks_the_effect_settlement(owner_desk, admin, governed_effect_executor):
    c = owner_desk;c['executor_fixture'] = governed_effect_executor
    publish_registry_actions(admin, (ACTION,))
    intent, tenant = unknown_intent(c, admin)
    observe(admin, intent, tenant, 'not_found', 30)
    owner = c['owner']()
    assert owner.confirm(intent).status_code == 200
    action = admin.execute('select action_name from runtime.nexloop_effect_intents where intent_id=%s', (intent,)).fetchone()[0]
    stale_claim(admin, tenant, action, intent, target='service', adapter='nexloop.effect')
    observe(admin, intent, tenant, 'accepted', 5)  # contradicting evidence arriving later
    refused = act(owner.client, owner.csrf, 'settle_action_claim', {'claim_action': action, 'intent_id': intent, 'category': 'confirmed_not_delivered', 'reason': 'x'})
    assert refused.status_code == 409 and refused.json()['details'] == {'reason': 'provider_observed'}
    assert claim_of(admin, action, intent)['state'] == 'active'
