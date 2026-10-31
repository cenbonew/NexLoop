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


def test_v26_adds_exactly_the_two_stale_claim_rules_on_v25():
    v25, v26 = json.loads((RULES / 'alert-rules.v25.json').read_text()), load_rules(RULES / 'alert-rules.v26.json')
    assert v26['version'] == 26 and len(v25['rules']) == 89 and len(v26['rules']) == 91 and v26['rules'][:89] == v25['rules']
    stale, effect = v26['rules'][89:]
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
    # Batch 71: the action_claims wrapper is stacked on L4 I31's service_credentials wrapper (and the earlier semantic_reads); all present.
    keys = admin.execute("select array(select jsonb_object_keys(runtime.nexloop_metrics_snapshot(%s,'real')))", (TENANT,)).fetchone()[0]
    assert {'action_claims', 'service_credentials', 'semantic_reads'} <= set(keys)
    chain = [r[0] for r in admin.execute("select proname from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='runtime' "
                                         "and proname in ('nexloop_metrics_snapshot_before_action_claims','nexloop_metrics_snapshot_before_service_credentials')")]
    assert sorted(chain) == ['nexloop_metrics_snapshot_before_action_claims', 'nexloop_metrics_snapshot_before_service_credentials']
    assert 'nexloop_metrics_snapshot_before_service_credentials' not in admin.execute(
        "select prosrc from pg_proc where oid='runtime.nexloop_metrics_snapshot(text,text)'::regprocedure").fetchone()[0]
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
    intent, tenant, action, owner = confirmed_ready(c, admin)
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


def confirmed_ready(c, admin):
    """An unknown effect with a not_found observation, ready for the owner's 0210."""
    publish_registry_actions(admin, (ACTION,))
    intent, tenant = unknown_intent(c, admin)
    observe(admin, intent, tenant, 'not_found', 30)
    action = admin.execute('select action_name from runtime.nexloop_effect_intents where intent_id=%s', (intent,)).fetchone()[0]
    return intent, tenant, action, c['owner']()


def confirmed(c, admin):
    intent, tenant, action, owner = confirmed_ready(c, admin)
    done = owner.confirm(intent)
    assert done.status_code == 200, done.text
    stale_claim(admin, tenant, action, intent, target='service', adapter='nexloop.effect')
    return intent, tenant, action, owner


def refusal(owner, action, intent):
    r = act(owner.client, owner.csrf, 'settle_action_claim', {'claim_action': action, 'intent_id': intent, 'category': 'confirmed_not_delivered', 'reason': 'x'})
    assert r.status_code == 409, r.text
    return r.json()['details']['reason']


def outbound(admin, tenant, intent, state):
    """An outbound reply of the intent in a given delivery state (0079 row; the state as the ledger triggers leave it)."""
    conversation = secrets.token_hex(32)
    admin.execute("insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key) "
                  "values(%s,'real',%s,%s,'synthetic-human',%s)", (tenant, conversation, secrets.token_hex(8), 'claim-settle-' + conversation[:12]))
    admin.execute("insert into runtime.nexloop_outbound_messages(tenant_id,world,intent_id,receipt_id,conversation_id,trigger_message_id,run_id,sender_kind,"
                  "sender_principal,role_ref,body_digest,delivery_state) values(%s,'real',%s,gen_random_uuid(),%s,%s,gen_random_uuid(),'agent','synthetic-agent',"
                  "'synthetic-role',%s,%s)", (tenant, intent, conversation, secrets.token_hex(32), 'b' * 64, state))


@PLAN
def test_a_provider_acceptance_blocks_the_effect_settlement(owner_desk, admin, governed_effect_executor):
    c = owner_desk;c['executor_fixture'] = governed_effect_executor
    intent, tenant, action, owner = confirmed(c, admin)
    observe(admin, intent, tenant, 'accepted', 5)  # contradicting evidence arriving later
    assert refusal(owner, action, intent) == 'provider_observed'
    assert claim_of(admin, action, intent)['state'] == 'active'


@PLAN
@pytest.mark.parametrize('state', ['dispatching', 'unknown', 'provider_accepted', 'delivered'])
def test_an_outbound_reply_not_persisted_or_failed_is_unknown(owner_desk, admin, governed_effect_executor, state):
    """L6 I32: only persisted or failed (or no outbound) is eligible; dispatching and unknown are unknown results - reconcile first."""
    c = owner_desk;c['executor_fixture'] = governed_effect_executor
    intent, tenant, action, owner = confirmed(c, admin)
    outbound(admin, tenant, intent, state)
    assert refusal(owner, action, intent) == 'outbound_unknown'
    assert claim_of(admin, action, intent)['state'] == 'active'


@PLAN
def test_an_outbound_reply_that_failed_is_eligible(owner_desk, admin, governed_effect_executor):
    c = owner_desk;c['executor_fixture'] = governed_effect_executor
    intent, tenant, action, owner = confirmed(c, admin)
    outbound(admin, tenant, intent, 'failed')
    code = "select runtime.nexloop_action_claim_settle_check(%s,'real',%s,%s,'confirmed_not_delivered')"
    assert admin.execute(code, (tenant, action, intent)).fetchone()[0] == 'eligible'


@PLAN
@pytest.mark.parametrize('change,reason', [
    ("provider_reference='provider-ref-9'", 'provider_reference'),
    ("attempt_revision=attempt_revision+1,updated_at=clock_timestamp()", 'attempt_after_determination')])
def test_any_sign_the_provider_may_have_received_it_goes_to_reconcile(owner_desk, admin, governed_effect_executor, change, reason):
    """L6 I32: a provider reference on the attempt, or an attempt that moved after the determination, refuses the settlement."""
    c = owner_desk;c['executor_fixture'] = governed_effect_executor
    intent, tenant, action, owner = confirmed(c, admin)
    with admin.transaction():
        admin.execute('alter table runtime.nexloop_effect_attempts disable trigger user')
        admin.execute(f'update runtime.nexloop_effect_attempts set {change} where intent_id=%s', (intent,))
        admin.execute('alter table runtime.nexloop_effect_attempts enable trigger user')
    assert refusal(owner, action, intent) == reason
    assert claim_of(admin, action, intent)['state'] == 'active'


UNCOMMITTED = ('Goal.create', 'PlanStep.create', 'nexloop.experiment.start', 'nexloop.approvals.decide', 'nexloop.real_dispatch.set',
               'nexloop.consumer_twin.calibrate')


def test_uncommitted_takes_only_the_proven_single_transaction_actions(desk):
    """L6 I33: each whitelisted Action writes its business rows and its terminal claim in one SQL function (one transaction). The two
    creates are authz.nexloop_create_object_action; the four others are governed handlers that authz.nexloop_goal_governed_action runs
    before it turns the claim terminal. Other nexloop.core / postgres callers (relations, two-step context artifacts, ...) are refused."""
    admin = desk['admin']
    def body(name):
        return admin.execute('select prosrc from pg_proc where oid=%s::regprocedure', (name + '(text,text,text,text,text)',)).fetchone()[0]
    terminal = "update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal'"
    # The create entry is renamed and wrapped across migrations; the function that inserts the object also ends the claim.
    creates = [r[0] for r in admin.execute("select prosrc from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='authz' "
                                           "and p.proname like 'nexloop_create_object_action%%' and p.prosrc like '%%insert into ontology.objects%%'")]
    assert creates and all(terminal in src for src in creates)
    entry = body('authz.nexloop_goal_governed_action')
    assert entry.index('cap.handler') < entry.index(terminal)
    manifest = {a['stable_name']: a for a in json.loads((RULES / 'business-actions.v1.json').read_text())['actions']}
    registered = {r[0] for r in admin.execute('select capability from control.nexloop_governed_capabilities')}
    for action in UNCOMMITTED[2:]:
        assert manifest[action]['target_systems'] == ['postgres'] and manifest[action]['capability_name'] in registered, action
    code = "select runtime.nexloop_action_claim_settle_check(%s,'real',%s,%s,'uncommitted')"
    for action in UNCOMMITTED + ('Relation.link', 'nexloop.context.bind', 'nexloop.service.scope_denial.record'):
        intent = 'synthetic-' + secrets.token_hex(16)
        stale_claim(admin, TENANT, action, intent)
        assert admin.execute(code, (TENANT, action, intent)).fetchone()[0] == ('eligible' if action in UNCOMMITTED else 'wrong_class'), action


@PLAN
def test_a_settled_create_replays_permanent_failure_and_writes_nothing(owner_desk, admin, governed_effect_executor):
    """L6 S63: after the settlement the original Action's reserve of the same intent replays the permanent_failure outcome; the create
    is refused before any write and no object appears."""
    from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
    c = owner_desk;c['executor_fixture'] = governed_effect_executor
    publish_registry_actions(admin, (ACTION,))
    plan = governed_effect_executor.plan
    planner = plan['backend'].authenticate(plan['planner_token'], world='real')  # after the role and Action publications above
    from datetime import UTC, datetime, timedelta
    expiry = (datetime.now(UTC) + timedelta(minutes=3)).isoformat()
    intent, properties = 'message-relay-' + secrets.token_hex(32), {'consumer_id': plan['consumer'], 'state': 'active', 'valid_until': expiry}
    # The stage failure: the reserve committed, the create transaction did not (fault injection on a disposable database).
    admin.execute("create function public.nx_refuse() returns trigger language plpgsql as $$ begin raise exception 'synthetic create failure'; end $$")
    admin.execute(f"create trigger nx_refuse before insert on ontology.objects for each row when (new.source_ref = '{intent}') execute function public.nx_refuse()")
    with pytest.raises(psycopg.Error, match='synthetic create failure'):
        planner.create_object(action_name='Goal.create', action_version=1, intent_id=intent, type_name='Goal', properties=properties)
    admin.execute('drop trigger nx_refuse on ontology.objects')
    admin.execute('drop function public.nx_refuse()')
    claim = claim_of(admin, 'Goal.create', intent)
    assert claim['state'] == 'active' and claim['binding']['target_system'] == 'postgres'
    admin.execute("update runtime.nexloop_action_claims set claim=jsonb_set(claim,'{lease_expires_at}',to_jsonb(clock_timestamp()-interval '2 hours')) "
                  "where action_name='Goal.create' and intent_id=%s", (intent,))
    owner = c['owner']()
    done = act(owner.client, owner.csrf, 'settle_action_claim', {'claim_action': 'Goal.create', 'intent_id': intent, 'category': 'uncommitted', 'reason': 'S63'})
    assert done.status_code == 200, done.text
    settled = claim_of(admin, 'Goal.create', intent)
    with pytest.raises(ActionAuthorizationDenied, match='non-success terminal outcome'):
        planner.create_object(action_name='Goal.create', action_version=1, intent_id=intent, type_name='Goal', properties=properties)
    assert claim_of(admin, 'Goal.create', intent) == settled  # replayed, not reclaimed (same revision, fence and outcome)
    assert settled['terminal_outcome'] == {**settled['terminal_outcome'], 'status': 'permanent_failure', 'outcome_id': str(done.json()['settlement_id'])}
    assert admin.execute('select count(*) from ontology.objects where source_ref=%s', (intent,)).fetchone()[0] == 0
