"""NX-037 A-class prep on clean catalog PostgreSQL (synthetic environment): the real tenant initialization script.

Dry-run lists the governed steps and executes nothing; execute goes only through trusted configuration (the configurator role)
and the owner's governed Budget.set (the owner's own session, as from the workbench); verify (the read-only metrics role, 0161)
passes only with every real dispatch layer closed, the D-g budget set and the alert rules in effect. The tenant here is a
synthetic test tenant inside a disposable cluster standing in for the real one; no real channel, model or switch."""
import json
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from goal_fixture import ACTIONS, authenticate_human, register_goal_actions, seed_human_owner
from nexloop_eios.goal_controls import GoalGovernedActions
from support.tenants import TENANT_A
from test_action_definitions import published_action  # noqa: F401
from test_browser_identity_reads import identity  # noqa: F401
from test_browser_session_creation import uow  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/nx037'))
import real_tenant_init as I  # noqa: E402

NOW = datetime.now(UTC).replace(microsecond=0)
PERIOD = (NOW - timedelta(hours=1), NOW + timedelta(days=3))


def private(path, value):
    path.write_text(value)
    path.chmod(0o600)
    return path


@pytest.fixture
def catalog(tmp_path):
    return private(tmp_path / 'value-catalog.json', json.dumps({'offerings': ['synthetic-offering']}))


def params(catalog, **kw):
    return I.parameters(tenant=TENANT_A, owner_principal='owner-principal-synthetic', value_catalog=catalog, channel_ref='channel:webchat:synthetic',
                        period_start=PERIOD[0].isoformat(), period_end=PERIOD[1].isoformat(), **kw)


def test_dry_run_lists_the_governed_steps_and_refuses_the_demo_tenant(catalog, capsys):
    p = params(catalog)
    out = I.plan(p)
    assert out['executed'] is False and out['cost_caps'] == {'cumulative': '100', 'per_run': '2', 'tenant_monthly': '300'}
    assert [(s['step'], s['kind']) for s in out['steps']] == [
        ('service_grants', 'trusted_configuration'), ('effect_categories', 'trusted_configuration'), ('alert_rules', 'trusted_configuration'),
        ('model_budget', 'owner_action'), ('value_catalog', 'reference'), ('channel_configuration', 'reference'), ('verify', 'check')]
    budget = out['steps'][3]['request']
    assert budget['action_name'] == 'Budget.set' and budget['budget_kind'] == 'model' and budget['unit'] == 'CNY' and budget['limit_amount'] == '100'
    assert I.budget_request(params(catalog))['request_id'] == budget['request_id']  # a stable business-intent id
    assert I.plan(params(catalog, cumulative='50', per_run='1', monthly='200'))['cost_caps'] == {'cumulative': '50', 'per_run': '1', 'tenant_monthly': '200'}
    from nexloop_eios.sandbox_profile import SANDBOX_TENANT
    base = ['--owner-principal', 'owner-x', '--value-catalog', str(catalog), '--channel-ref', 'channel:x',
            '--period-start', PERIOD[0].isoformat(), '--period-end', PERIOD[1].isoformat()]
    assert I.main(['--tenant', SANDBOX_TENANT, *base]) == 2
    assert I.main(['--tenant', TENANT_A, *base, '--per-run', '500']) == 2  # per-Run above the cumulative cap
    assert I.main(['execute', '--tenant', TENANT_A, *base]) == 2  # no trusted configuration files: nothing runs
    capsys.readouterr()
    assert I.main(['--tenant', TENANT_A, *base]) == 0
    assert json.loads(capsys.readouterr().out)['mode'] == 'dry_run'


def test_execute_through_trusted_configuration_and_the_owner_action_then_verify(pg, admin, catalog, tmp_path, published_action, identity, uow, monkeypatch):  # noqa: F811
    reader, base, capability = published_action
    admin.execute('alter role nexloop_configurator login')
    configurator = private(tmp_path / 'configurator-dsn', make_conninfo(pg, user='nexloop_configurator'))
    files = dict(configurator_dsn_file=configurator, signing_key_file=private(tmp_path / 'signing', secrets.token_bytes(32).hex()),
                 signing_key_id='nx037-init', credential_expires_at=(NOW + timedelta(hours=2)).isoformat())
    manifest = json.loads(I.GRANTS.read_text())
    files['service_secrets_file'] = private(tmp_path / 'secrets', json.dumps({x['credential_reference']: secrets.token_urlsafe(48) for x in manifest['principals']}))
    p = params(catalog)
    publish_incentive(admin)
    metrics = psycopg.connect(make_conninfo(pg, user='nexloop_metrics'), autocommit=True)
    with metrics:
        before = I.verify(p, metrics, environ={})
        assert before['ready'] is False and before['checks']['alert_rules_in_effect'] is False
        out = I.execute(p, **files)
        assert out['steps']['model_budget']['status'] == 'owner_pending' and out['steps']['service_grants']['grants_added'] > 0
        categories = out['steps']['effect_categories']  # only the published Action is declared; the others stay external (closed)
        assert categories['declared'] == 1 and categories['deferred'] == len(json.loads((I.CONFIG / json.loads(I.TRIAL.read_text())['effect_categories']).read_text())['actions']) - 1
        assert categories['externality_written'] == 1
        pending = I.verify(p, metrics, environ={})
        assert pending['ready'] is False
        assert {k for k, v in pending['checks'].items() if not v} == {'model_budget_is_cumulative_cap', 'model_budget_covers_trial'}
        # The owner submits Budget.set from their own session (the workbench): the governed path, not a table write.
        register_goal_actions(admin, base, capability)
        browser = seed_human_owner(admin, reader.pool, list(ACTIONS), identity, uow)
        owner = GoalGovernedActions(reader.pool, authenticate_human(reader.pool, browser), reader.signer)
        again = I.execute(p, **files, owner_actions=owner)  # trusted configuration is idempotent
        assert again['steps']['model_budget']['status'] == 'submitted'
        ready = I.verify(p, metrics, environ={})
        assert ready['ready'] is True, ready
        assert ready['enforced_by_executor'] == {'per_run': '2', 'tenant_monthly': '300'}
        # Any open layer fails the check: the deployment layer from the environment, the tenant layer from its switch.
        assert I.verify(p, metrics, environ={'REAL_DISPATCH_ENABLED': 'true'})['checks']['deployment_switch_closed'] is False
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')  # test harness: a switch row as if the owner had opened it
            admin.execute("""insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
                values(%s,'real','tenant','*',true,'test',1,'synthetic','synthetic-owner','synthetic-intent')""", (TENANT_A,))
        opened = I.verify(p, metrics, environ={})
        assert opened['ready'] is False and opened['checks']['tenant_switch_closed'] is False
        # A different cap than the one set is not ready.
        assert I.verify(params(catalog, cumulative='50'), metrics, environ={})['checks']['model_budget_is_cumulative_cap'] is False
    assert not any(v in json.dumps(out) + json.dumps(ready) for v in (pg, configurator.read_text()))


def publish_incentive(admin):
    """A published effect Action in the tenant (business manifest, as the configuration fixtures publish it); the loopback
    harness declaration is removed so that only the script's trusted configuration declares it."""
    from psycopg.types.json import Jsonb
    from commercial_fixture import publish_registry_actions
    from support.loopback_effects import make_external
    from test_business_actions import consumer_type
    admin.execute('insert into ontology.object_type_versions(tenant_id,type_name,version,definition) values(%s,%s,1,%s) on conflict do nothing',
                  (TENANT_A, 'Consumer', Jsonb(consumer_type().model_dump(mode='json'))))
    publish_registry_actions(admin, ('nexloop.incentive.issue',), tenant=TENANT_A)
    make_external(admin, TENANT_A, 'eios:action:nexloop.incentive.issue:1')


def test_run_evidence_is_numbers_only_for_the_metrics_role(pg, admin, published_action):  # noqa: F811
    import uuid
    run = uuid.uuid4()
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("insert into runtime.nexloop_run_tool_timings(tenant_id,world,run_id,calls,total_ms,max_ms) values(%s,'real',%s,2,75.4,50)", (TENANT_A, run))
        admin.execute("""insert into runtime.nexloop_cost_entries(tenant_id,world,entry_id,data_mode,cost_kind,amount,currency,amount_unit,basis,source_ref,run_id,occurred_at,recorded_by)
            values(%s,'real','model:synthetic-1','real','model',0.12,'CNY','major','provider_reported','model-call:1',%s,now(),'synthetic')""", (TENANT_A, run))
    with psycopg.connect(make_conninfo(pg, user='nexloop_metrics'), autocommit=True) as metrics:
        evidence = metrics.execute("select authz.nexloop_nx037_run_evidence(%s,'real',%s)", (TENANT_A, run)).fetchone()[0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            metrics.execute('select runtime.nexloop_nx037_run_evidence(%s,%s,%s)', (TENANT_A, 'real', run))
    assert evidence['tool'] == {'calls': 2, 'ms': 75, 'max_ms': 50}
    assert evidence['costs'] == {'model': {'amount': '0.12000000', 'currencies': ['CNY']}}
    assert evidence['model']['calls'] == 0 and evidence['intents'] == {} and evidence['intent_ids'] == []


def test_real_run_entry_refuses_until_verify_and_the_planned_switches_hold(pg, admin, catalog, tmp_path, published_action, identity, uow, capsys):  # noqa: F811
    """scripts/nx037/run_interactions.py --driver real: refused before anything runs unless the tenant check passes (budget,
    alert rules) and the three switch layers are exactly the plan's; with both, it stops at the missing real driver."""
    import run_interactions as R
    reader, base, capability = published_action
    admin.execute('alter role nexloop_configurator login')
    manifest = json.loads(I.GRANTS.read_text())
    files = dict(configurator_dsn_file=private(tmp_path / 'configurator-dsn', make_conninfo(pg, user='nexloop_configurator')),
                 signing_key_file=private(tmp_path / 'signing', secrets.token_bytes(32).hex()), signing_key_id='nx037-init',
                 credential_expires_at=(NOW + timedelta(hours=2)).isoformat(),
                 service_secrets_file=private(tmp_path / 'secrets', json.dumps({x['credential_reference']: secrets.token_urlsafe(48) for x in manifest['principals']})))
    p = params(catalog)
    I.execute(p, **files)
    metrics_dsn = private(tmp_path / 'metrics-dsn', make_conninfo(pg, user='nexloop_metrics'))
    session = private(tmp_path / 'owner-session', 'synthetic-session')
    host_config = tmp_path / 'host.json'  # D-g option 2: the Host bills in CNY with the shipped tariff
    host_config.write_text(json.dumps({'runtime_profile': 'deepseek-flash', 'cost_currency': 'CNY',
                                       'model_tariff_file': str(ROOT / 'deploy/configuration/model-tariffs/deepseek-flash.cny.v1.json')}))
    args = ['--driver', 'real', '--trial-id', '37b00000-0000-4000-8000-000000000002', '--out', str(tmp_path / 'real'), '--tenant', TENANT_A,
            '--owner-principal', 'owner-principal-synthetic', '--value-catalog', str(catalog), '--channel-ref', 'channel:webchat:synthetic',
            '--period-start', PERIOD[0].isoformat(), '--period-end', PERIOD[1].isoformat(), '--metrics-dsn-file', str(metrics_dsn),
            '--owner-session-file', str(session), '--host-config', str(host_config), '--interaction-timeout', '1', '--poll-seconds', '0.2']
    prompted = []

    class AbsentOperator:  # the person playing the customer does not act: the interaction times out
        def prompt(self, interaction, instruction, deadline_seconds):
            prompted.append((interaction['seq'], interaction['kind'], deadline_seconds))

    def attempt(environ):
        code = R.main(args, environ=environ, connect=lambda f: psycopg.connect(Path(f).read_text().strip(), autocommit=True),
                      operator=AbsentOperator())
        return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    code, out = attempt({})
    assert code == 3 and 'reply_fallback_currency_differs' not in out['refused']  # reply-guarantee.v3.json is CNY
    assert code == 3 and set(out['refused']) == {'model_budget_is_cumulative_cap', 'model_budget_covers_trial', 'switch_deployment_differs',
                                                 'switch_tenant_differs', 'switch_data_mode_differs', 'switch_actions_differs'}
    register_goal_actions(admin, base, capability)
    owner = GoalGovernedActions(reader.pool, authenticate_human(reader.pool, seed_human_owner(admin, reader.pool, list(ACTIONS), identity, uow)), reader.signer)
    I.execute(p, **files, owner_actions=owner)
    code, out = attempt({'REAL_DISPATCH_ENABLED': 'true'})
    assert code == 3 and set(out['refused']) == {'switch_tenant_differs', 'switch_data_mode_differs', 'switch_actions_differs'}
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')  # test harness: the switches as the owner would have opened them
        for kind, ref, mode in (('tenant', '*', 'real'), *(('action_type', action, None) for action in json.loads(R.PLAN.read_text())['real_dispatch']['actions'])):
            admin.execute("""insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id)
                values(%s,'real',%s,%s,true,%s,1,'synthetic','synthetic-owner','synthetic-intent')""", (TENANT_A, kind, ref, mode))
    code, out = attempt({})
    assert code == 3 and out['refused'] == ['switch_deployment_differs']
    assert prompted == []  # nothing was ever prompted while a precondition failed
    code, out = attempt({'REAL_DISPATCH_ENABLED': 'true'})
    # Preconditions met: the real trial runs; the first interaction is prompted and, not completed in time, pauses the trial.
    assert code == 1 and out['status'] == 'paused' and out['rules'] == ['interaction_timeout'] and out['recorded'] == 1, out
    plan_kind = json.loads(R.PLAN.read_text())['order'][0]
    assert prompted == [(1, plan_kind, 1)]
    [record] = [json.loads(line) for line in (tmp_path / 'real' / 'interactions.jsonl').read_text().splitlines()]
    assert record['error_code'] == 'interaction_timeout' and record['run_id'] is None
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("""insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,revision,reason,changed_by,intent_id)
            values(%s,'real','action_type','eios:action:nexloop.incentive.issue:1',true,1,'synthetic','synthetic-owner','synthetic-intent')""", (TENANT_A,))
    code, out = attempt({'REAL_DISPATCH_ENABLED': 'true'})
    assert code == 3 and out['refused'] == ['switch_actions_differs']  # an Action open beyond the plan
    printed = json.dumps(out)
    assert 'synthetic-session' not in printed and pg not in printed


def test_month_model_cost_is_per_currency_and_this_month_for_the_metrics_role(pg, admin, published_action):  # noqa: F811
    """0185: the tenant's model cost this UTC month in one currency, read by the metrics role; no conversion, other months,
    other currencies, other cost kinds and other tenants are not counted."""
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        for n, (kind, amount, currency, when) in enumerate((('model', '1.25', 'CNY', "date_trunc('month',now() at time zone 'UTC') at time zone 'UTC'"),
                                                            ('model', '0.75', 'CNY', 'now()'), ('model', '9', 'USD', 'now()'),
                                                            ('model', '5', 'CNY', "date_trunc('month',now() at time zone 'UTC') at time zone 'UTC' - interval '1 second'"),
                                                            ('channel', '3', 'CNY', 'now()'))):
            admin.execute(f"""insert into runtime.nexloop_cost_entries(tenant_id,world,entry_id,data_mode,cost_kind,amount,currency,amount_unit,basis,source_ref,occurred_at,recorded_by)
                values(%s,'real',%s,'real',%s,%s,%s,'major','provider_reported','synthetic',{when},'synthetic')""", (TENANT_A, f'month:{n}', kind, amount, currency))
    with psycopg.connect(make_conninfo(pg, user='nexloop_metrics'), autocommit=True) as metrics:
        cost = lambda currency: metrics.execute("select authz.nexloop_nx037_month_model_cost(%s,'real',%s)", (TENANT_A, currency)).fetchone()[0]
        assert cost('CNY') == 2 and cost('USD') == 9 and cost('EUR') == 0
        with pytest.raises(psycopg.Error):
            cost('cny')
