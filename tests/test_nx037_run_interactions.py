"""NX-037: the controlled interactions run entry (scripts/nx037/run_interactions.py) in the synthetic environment: a normal
run, the four D-i immediate stops, the three pauses with an explicit owner resume, the owner-editable plan, and the real mode
refused when its preconditions do not hold. No real channel, model or switch."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/nx037'))
import run_interactions as R  # noqa: E402

from nexloop_eios import controlled_interactions as X  # noqa: E402

TRIAL_ID = '37b00000-0000-4000-8000-000000000001'
ORDER = json.loads(R.PLAN.read_text())['order']


def run(tmp_path, *extra, out='trial', capsys=None):
    code = R.main(['--driver', 'synthetic', '--trial-id', TRIAL_ID, '--out', str(tmp_path / out), *extra])
    return code, (json.loads(capsys.readouterr().out.strip().splitlines()[-1]) if capsys else None)


def seq_of(kind, after=3):
    return next(i + 1 for i, k in enumerate(ORDER) if k == kind and i + 1 > after)


def test_the_shipped_plan_holds_the_mix_and_a_normal_run_completes(tmp_path, capsys):
    trial = X.load_plan(R.TRIAL)
    plan = R.load_interaction_plan(R.PLAN, trial)
    assert {k: plan['order'].count(k) for k in X.KINDS} == trial['mix'] and len(plan['order']) == 30
    code, out = run(tmp_path, capsys=capsys)
    assert code == 0 and out['status'] == 'completed' and out['recorded'] == 30
    assert [r['kind'] for r in X.read_records(tmp_path / 'trial')] == ORDER  # the plan's order, not the default interleave


@pytest.mark.parametrize('fault,kind,rule', [('unauthorized_send', 'human_takeover', 'unauthorized_send'),
                                             ('wrong_recipient', 'ordinary_reply', 'wrong_recipient'),
                                             ('contact_after_refusal', 'restricted_after_refusal', 'contact_after_refusal'),
                                             ('duplicate_external_effect', 'real_delivery', 'duplicate_external_effect')])
def test_each_immediate_stop_and_a_stopped_trial_never_resumes(tmp_path, capsys, fault, kind, rule):
    seq = seq_of(kind)
    code, out = run(tmp_path, '--inject', f'{seq}={fault}', capsys=capsys)
    assert code == 2 and out['status'] == 'stopped' and out['rules'] == [rule] and out['at_seq'] == seq
    assert len(X.read_records(tmp_path / 'trial')) == seq
    code, out = run(tmp_path, '--resume', 'owner-confirmation:1', capsys=capsys)
    assert code == 3 and out['detail'] == 'trial_stopped'


def test_error_rate_pause_needs_an_explicit_resume_with_the_owner_confirmation(tmp_path, capsys):
    code, out = run(tmp_path, '--inject', '5=error', '--inject', '9=error', capsys=capsys)
    assert code == 1 and out['rules'] == ['error_rate'] and out['at_seq'] == 9
    code, out = run(tmp_path, capsys=capsys)
    assert code == 3 and out['detail'] == 'paused_needs_resume' and out['rules'] == ['error_rate']
    code, out = run(tmp_path, '--resume', 'not a reference', capsys=capsys)
    assert code == 3 and len(X.read_records(tmp_path / 'trial')) == 9
    code, out = run(tmp_path, '--resume', 'owner-confirmation:pause-1', capsys=capsys)
    assert code == 0 and out['recorded'] == 30 and out['resumes'] == 1
    [resume] = X.read_resumes(tmp_path / 'trial')
    assert resume['after_seq'] == 9 and resume['confirmation_ref'] == 'owner-confirmation:pause-1' and resume['rules'] == ['error_rate']
    code, out = run(tmp_path, '--resume', 'owner-confirmation:again', capsys=capsys)
    assert code == 3  # nothing to resume


def test_cost_pause_resumes_into_the_same_pause_while_the_cost_stays(tmp_path, capsys):
    config = json.loads(R.TRIAL.read_text())
    config['cost_caps'] = {'cumulative': '1', 'per_run': '0.5', 'tenant_monthly': '300'}
    trial = tmp_path / 'trial.json'
    trial.write_text(json.dumps(config))
    code, out = run(tmp_path, '--trial-config', str(trial), capsys=capsys)
    assert code == 1 and out['rules'] == ['cumulative_cost']
    first = out['at_seq']
    code, out = run(tmp_path, '--trial-config', str(trial), '--resume', 'owner-confirmation:cost', capsys=capsys)
    assert code == 1 and out['rules'] == ['cumulative_cost'] and out['at_seq'] == first + 1  # one more, then paused again


def test_unresolved_unknown_effect_pauses_then_resumes(tmp_path, capsys):
    seq = seq_of('unknown_reconciliation', after=0)
    code, out = run(tmp_path, '--inject', f'{seq}=unresolved_unknown_effect', capsys=capsys)
    assert code == 1 and out['rules'] == ['unresolved_unknown_effect']
    code, out = run(tmp_path, '--resume', 'owner-confirmation:reconciled', capsys=capsys)
    assert code == 0 and out['recorded'] == 30


def test_calibration_pause_and_the_plan_file_rules(tmp_path, capsys):
    code, out = run(tmp_path, '--calibration-pause', capsys=capsys)
    assert code == 1 and out['rules'] == ['calibration'] and out['at_seq'] == 3
    edited = json.loads(R.PLAN.read_text())
    edited['order'] = list(reversed(edited['order']))
    swapped = tmp_path / 'plan-reversed.json'
    swapped.write_text(json.dumps(edited))
    code, out = run(tmp_path, '--plan-file', str(swapped), '--resume', 'owner-confirmation:c', capsys=capsys)
    assert code == 3 and out['detail'] == 'the interaction order changed'  # the order is fixed once a trial started
    bad = dict(edited, order=edited['order'][:-1] + ['human_takeover' if edited['order'][-1] != 'human_takeover' else 'real_delivery'])
    broken = tmp_path / 'plan-bad.json'
    broken.write_text(json.dumps(bad))
    code, out = run(tmp_path, '--plan-file', str(broken), out='other', capsys=capsys)
    assert code == 3 and out['detail'] == 'order does not match the mix'
    code, out = run(tmp_path, '--plan-file', str(swapped), out='reversed', capsys=capsys)
    assert code == 0 and [r['kind'] for r in X.read_records(tmp_path / 'reversed')] == edited['order']
    assert run(tmp_path, '--inject', '2=unknown_fault', out='x', capsys=capsys)[0] == 3
    assert run(tmp_path, '--channel-ref', 'channel:x', out='y', capsys=capsys)[0] == 3  # no real configuration in synthetic mode


def real_args(tmp_path, **override):
    session = tmp_path / 'owner-session'
    session.write_text('synthetic-session')
    session.chmod(0o600)
    catalog = tmp_path / 'catalog.json'
    catalog.write_text('{}')
    values = {'--tenant': '00000000-0000-4000-8000-00000000000a', '--owner-principal': 'owner-principal-synthetic', '--value-catalog': str(catalog),
              '--channel-ref': 'channel:webchat:synthetic', '--period-start': '2026-10-12T00:00:00+00:00', '--period-end': '2026-10-15T00:00:00+00:00',
              '--metrics-dsn-file': str(tmp_path / 'metrics-dsn'), '--owner-session-file': str(session), '--host-config': str(tmp_path / 'host.json'), **override}
    return ['--driver', 'real', '--trial-id', TRIAL_ID, '--out', str(tmp_path / 'real'), *[x for k, v in values.items() if v is not None for x in (k, v)]]


def test_real_mode_refuses_missing_configuration_injection_and_a_shared_session_file(tmp_path, capsys):
    def no_connection(_):
        raise AssertionError('must refuse before connecting')
    assert R.main(real_args(tmp_path, **{'--metrics-dsn-file': None}), connect=no_connection) == 3
    assert R.main(real_args(tmp_path) + ['--inject', '1=error'], connect=no_connection) == 3
    args = real_args(tmp_path)
    (tmp_path / 'owner-session').chmod(0o644)
    assert R.main(args, connect=no_connection) == 3
    capsys.readouterr()
    assert not (tmp_path / 'real').exists()


def test_currency_preconditions_need_the_host_tariff_and_the_fallback_budget_in_the_trial_currency(tmp_path):
    """D-g option 2: the Host bills in the trial currency with a tariff, and the reply fallback Run budget uses it too."""
    tariff = ROOT / 'deploy/configuration/model-tariffs/deepseek-flash.cny.v1.json'
    reply = json.loads((ROOT / 'deploy/configuration/reply-guarantee.v2.json').read_text())
    reply['fallback']['run_budget']['currency'] = 'CNY'
    cny_reply = tmp_path / 'reply.json'
    cny_reply.write_text(json.dumps(reply))

    def host(**config):
        path = tmp_path / 'host.json'
        path.write_text(json.dumps({'runtime_profile': 'deepseek-flash', **config}))
        return path

    good = host(cost_currency='CNY', model_tariff_file=str(tariff))
    assert R.currency_preconditions('CNY', good, cny_reply) == []
    assert R.currency_preconditions('CNY', good) == []  # the shipped CNY policy (reply-guarantee.v3.json)
    v2 = ROOT / 'deploy/configuration/reply-guarantee.v2.json'
    assert R.currency_preconditions('CNY', good, v2) == ['reply_fallback_currency_differs']  # v2 stays USD
    assert R.currency_preconditions('CNY', host(model_tariff_file=str(tariff)), cny_reply) == ['host_currency_differs']
    assert R.currency_preconditions('CNY', host(cost_currency='CNY'), cny_reply) == ['model_tariff_missing']
    other = tmp_path / 'usd-tariff.json'
    other.write_text(json.dumps({**json.loads(tariff.read_text()), 'currency': 'USD'}))
    assert R.currency_preconditions('CNY', host(cost_currency='CNY', model_tariff_file=str(other)), cny_reply) == ['model_tariff_invalid']
    assert R.currency_preconditions('CNY', tmp_path / 'absent.json', cny_reply) == ['host_config_unreadable']
    path = tmp_path / 'det.json'
    path.write_text(json.dumps({'runtime_profile': 'deterministic-test', 'cost_currency': 'CNY'}))
    assert R.currency_preconditions('CNY', path, cny_reply) == ['host_not_real_model', 'model_tariff_missing']


def test_month_cost_before_subtracts_what_this_trial_already_recorded():
    from decimal import Decimal

    class Db:
        def __init__(self, value):
            self.value, self.args = value, None

        def execute(self, sql, args):
            self.args = args
            return self

        def fetchone(self):
            return (self.value,)

    db = Db(Decimal('12.50'))
    assert R.month_cost_before(db, 'tenant-a', 'CNY', [{'cost': {'total': '2.5'}}, {'cost': {'total': '1'}}]) == Decimal('9.00')
    assert db.args == ('tenant-a', 'CNY')
    assert R.month_cost_before(Db(Decimal('1')), 't', 'CNY', [{'cost': {'total': '3'}}]) == 0


def test_cny_budgets_fit_the_tariff_floor_and_the_per_run_cap():
    """D-g option 2: the shipped CNY Host example reserves at least the tariff floor per request, every CNY Run budget admits its
    model turns at that reservation, and none exceeds the NX-037 per-Run cap. The floor bounds input by the request size."""
    from decimal import Decimal
    from nexloop_eios.contact_restrictions import load_reply_policy
    from nexloop_eios.plan_reevaluation import load_settings
    config = ROOT / 'deploy/configuration'
    tariff = json.loads((config / 'model-tariffs/deepseek-flash.cny.v1.json').read_text())
    rates = {k: Decimal(str(v)) for k, v in tariff['rates'].items()}
    floor = (min(1000000, 196608) * max(rates['input'], rates['cache_read'], rates['cache_write']) + 4096 * rates['output']) / 1000000
    host = json.loads((ROOT / 'deploy/stage/agent-host.runtime-config.example.json').read_text())
    per_request, cap = Decimal(host['maximum_request_cost']), Decimal(X.load_plan(R.TRIAL)['cost_caps']['per_run'])
    assert host['cost_currency'] == 'CNY' == tariff['currency'] and floor == Decimal('0.425984') and per_request >= floor
    budgets = [load_reply_policy(config / 'reply-guarantee.v3.json')['fallback']['run_budget'],
               load_settings(config / 'plan-reevaluation.v2.json')['run_budget']]
    for budget in budgets:
        assert budget['currency'] == 'CNY'
        assert budget['maximum_model_turns'] * per_request <= Decimal(budget['maximum_cost']) <= cap
    assert load_reply_policy(config / 'reply-guarantee.v2.json')['fallback']['run_budget']['currency'] == 'USD'  # published, unchanged


def test_the_test_stack_is_refused_outside_a_synthetic_tenant_the_real_driver_and_cny(tmp_path, capsys):
    """main(..., test_stack=True) (no CLI option) runs the real driver on the local synthetic stack: a real tenant uuid, the
    synthetic driver and a trial currency other than CNY are refused before any connection."""
    def no_connection(_):
        raise AssertionError('must refuse before connecting')
    routes = loopback_routes_file(tmp_path)
    real_tenant = '7d3c0a5e-1b2f-4c6d-8e9f-0a1b2c3d4e5f'
    assert R.main(real_args(tmp_path, **{'--tenant': real_tenant}), connect=no_connection, test_stack=True, provider_routes_file=routes) == 3
    assert json.loads(capsys.readouterr().out)['refused'] == ['test_stack_needs_synthetic_tenant']
    assert R.main(['--driver', 'synthetic', '--trial-id', TRIAL_ID, '--out', str(tmp_path / 'syn')], connect=no_connection, test_stack=True,
                  provider_routes_file=routes) == 3
    assert json.loads(capsys.readouterr().out)['refused'] == ['test_stack_needs_real_driver', 'test_stack_needs_synthetic_tenant']
    # A non-loopback enterprise route (a documentation address standing in for a real endpoint), or no routes file: refused.
    remote = loopback_routes_file(tmp_path / 'remote', enterprise='https://' + '.'.join(['203', '0', '113', '7']) + ':443')
    assert R.main(real_args(tmp_path), connect=no_connection, test_stack=True, provider_routes_file=remote) == 3
    assert json.loads(capsys.readouterr().out)['refused'] == ['test_stack_routes_not_loopback']
    assert R.main(real_args(tmp_path), connect=no_connection, test_stack=True) == 3
    assert json.loads(capsys.readouterr().out)['refused'] == ['test_stack_routes_not_loopback']
    usd = json.loads(R.TRIAL.read_text())
    usd['currency'] = 'USD'
    usd_file = tmp_path / 'usd-trial.json'
    usd_file.write_text(json.dumps(usd))
    assert R.main(real_args(tmp_path) + ['--trial-config', str(usd_file)], connect=no_connection, test_stack=True, provider_routes_file=routes) == 3
    assert json.loads(capsys.readouterr().out)['refused'] == ['test_stack_currency_not_cny']
    assert '--test-stack' not in json.dumps(R.__doc__) and R.main(real_args(tmp_path) + ['--test-stack'], connect=no_connection) == 3
    assert not (tmp_path / 'real').exists() and not (tmp_path / 'syn').exists()


def test_the_test_stack_host_is_deterministic_and_still_in_the_trial_currency(tmp_path):
    reply = ROOT / 'deploy/configuration/reply-guarantee.v3.json'

    def host(**config):
        path = tmp_path / 'host.json'
        path.write_text(json.dumps(config))
        return path

    deterministic = host(runtime_profile='deterministic-test', cost_currency='CNY')
    assert R.currency_preconditions('CNY', deterministic, reply, test_stack=True) == []
    assert R.currency_preconditions('CNY', deterministic, reply) == ['host_not_real_model', 'model_tariff_missing']  # never outside the test stack
    assert R.currency_preconditions('CNY', host(runtime_profile='deterministic-test'), reply, test_stack=True) == ['host_currency_differs']
    tariff = ROOT / 'deploy/configuration/model-tariffs/deepseek-flash.cny.v1.json'
    model = host(runtime_profile='deepseek-flash', cost_currency='CNY', model_tariff_file=str(tariff))
    assert R.currency_preconditions('CNY', model, reply, test_stack=True) == ['test_stack_host_not_deterministic']
    for key, value in (('model_tariff_file', str(tariff)), ('model_configuration_file', 'model.json'), ('maximum_request_cost', '0.45')):
        mixed = host(runtime_profile='deterministic-test', cost_currency='CNY', **{key: value})
        assert R.currency_preconditions('CNY', mixed, reply, test_stack=True) == ['test_stack_host_not_deterministic'], key


def loopback_routes_file(directory, enterprise='https://127.0.0.1:9443'):
    directory.mkdir(exist_ok=True)
    configs = {}
    for action, origin in (('nexloop.service.request', 'https://127.0.0.1:9444'), ('nexloop.enterprise.deliver', enterprise)):
        path = directory / (action + '.json')
        path.write_text(json.dumps({'origin': origin}))
        configs[action] = str(path)
    routes = directory / 'routes.json'
    routes.write_text(json.dumps({'schema': 'nexloop-effect-provider-routes/1', 'routes': configs}))
    return routes


def test_loopback_routes_accept_only_loopback_origins(tmp_path):
    assert R.loopback_routes(loopback_routes_file(tmp_path / 'a'))
    assert R.loopback_routes(loopback_routes_file(tmp_path / 'b', enterprise='https://localhost:9443'))
    assert R.loopback_routes(loopback_routes_file(tmp_path / 'c', enterprise='https://[::1]:9443'))
    assert not R.loopback_routes(loopback_routes_file(tmp_path / 'd', enterprise='https://enterprise.example.invalid'))
    assert not R.loopback_routes(tmp_path / 'absent.json')


def test_test_stack_tenants_are_the_synthetic_tenant_constants():
    from support.tenants import TENANT_A, TENANT_B, TENANT_OTHER
    assert R.TEST_STACK_TENANTS == {TENANT_A, TENANT_B, TENANT_OTHER} and R.SYNTHETIC_TENANT == TENANT_A


def test_test_stack_records_state_and_evidence_pack_are_never_real_evidence(tmp_path):
    import evidence_pack as E
    trial = X.load_plan(R.TRIAL)
    from nexloop_eios.interaction_drivers import SyntheticCollector, SyntheticDriver

    def fresh():  # one synthetic stack per trial
        driver = SyntheticDriver(tenant=R.SYNTHETIC_TENANT, other_tenant=R.SYNTHETIC_OTHER)
        return driver, SyntheticCollector(driver)
    marked = X.run(trial, *fresh(), tmp_path / 'marked', trial_id=TRIAL_ID, order=ORDER, test_stack=True)
    assert marked['test_stack'] is True and all(r['test_stack'] is True for r in X.read_records(tmp_path / 'marked'))
    plain = X.run(trial, *fresh(), tmp_path / 'plain', trial_id=TRIAL_ID, order=ORDER)
    assert 'test_stack' not in plain and not any('test_stack' in r for r in X.read_records(tmp_path / 'plain'))
    E.build(tmp_path / 'pack-marked', {}, tmp_path / 'marked')
    index = json.loads((tmp_path / 'pack-marked' / 'index.json').read_text())
    assert index['test_stack'] is True and index['real_evidence'] is False
    assert json.loads((tmp_path / 'pack-marked' / 'interactions' / 'summary.json').read_text())['test_stack'] is True
    E.build(tmp_path / 'pack-plain', {}, tmp_path / 'plain')
    assert 'real_evidence' not in json.loads((tmp_path / 'pack-plain' / 'index.json').read_text())
    assert E.check(tmp_path / 'pack-marked') == []
    # A paused test stack trial cannot be resumed as another stack (and the reverse).
    paused = X.run(trial, *fresh(), tmp_path / 'paused', trial_id=TRIAL_ID, order=ORDER, calibration_pause=True, test_stack=True)
    assert paused['status'] == 'paused'
    with pytest.raises(X.PlanInvalid):
        X.run(trial, *fresh(), tmp_path / 'paused', trial_id=TRIAL_ID, order=ORDER, resume='owner-confirmation:x')


PLAN_V3 = ROOT / 'deploy/configuration/nx037-interaction-plan.v3.json'
TRIAL_V3 = ROOT / 'deploy/configuration/nx037-trial.v3.json'


def test_plan_v3_loads_with_the_mix_counted_by_kind_and_v2_still_loads():
    trial = X.load_plan(TRIAL_V3)
    v2 = X.load_plan(R.TRIAL)
    assert trial['cost_caps'] == v2['cost_caps'] and trial['currency'] == v2['currency'] and trial['calibration'] == v2['calibration']
    assert json.loads(TRIAL_V3.read_text())['stop'] == json.loads(R.TRIAL.read_text())['stop']
    assert json.loads(TRIAL_V3.read_text())['pause'] == json.loads(R.TRIAL.read_text())['pause']
    plan = R.load_interaction_plan(PLAN_V3, trial)
    assert len(plan['order']) == 36 and {k: [i['kind'] for i in plan['order']].count(k) for k in X.KINDS} == trial['mix'] == {
        'ordinary_reply': 16, 'real_delivery': 6, 'restricted_after_refusal': 5, 'human_takeover': 5, 'unknown_reconciliation': 4}
    assert [i['kind'] for i in plan['order'][:3]] == ['ordinary_reply', 'real_delivery', 'unknown_reconciliation']  # calibration
    assert plan['consumers'] == sorted({i['consumer'] for i in plan['order']}) and len(plan['consumers']) == 6
    assert R.load_interaction_plan(R.PLAN, v2)['consumers'] is None  # v2: kinds only


def test_plan_v3_refuses_a_wrong_mix_bad_items_and_a_refusal_account_playing_a_delivery(tmp_path):
    trial = X.load_plan(TRIAL_V3)
    base = json.loads(PLAN_V3.read_text())

    def load(change):
        value = json.loads(json.dumps(base))
        change(value)
        path = tmp_path / 'plan.json'
        path.write_text(json.dumps(value))
        return R.load_interaction_plan(path, trial)
    with pytest.raises(X.PlanInvalid):
        load(lambda v: v['order'][0].update(kind='real_delivery'))  # 15/7: the mix no longer holds
    with pytest.raises(X.PlanInvalid):
        load(lambda v: v['order'][0].update(variant='Not A Variant'))
    with pytest.raises(X.PlanInvalid):
        load(lambda v: v['order'][0].pop('consumer'))
    with pytest.raises(R.Refused):
        load(lambda v: v['order'].__setitem__(0, 'ordinary_reply'))  # v3 items only

    def refusal_delivers(v):
        refusal = next(i['consumer'] for i in v['order'] if i['kind'] == 'restricted_after_refusal')
        next(i for i in v['order'] if i['kind'] == 'real_delivery')['consumer'] = refusal
    with pytest.raises(R.Refused, match='account groups'):
        load(refusal_delivers)
    with pytest.raises(R.Refused):
        load(lambda v: v.update(consumers=['consumer:<CUSTOMER_1>']))  # a separate list must be the order's set


def test_a_v3_synthetic_run_records_variants_and_a_long_conversations_runs_count_per_run():
    import tempfile
    trial = X.load_plan(TRIAL_V3)
    order = json.loads(PLAN_V3.read_text())['order']
    from nexloop_eios.interaction_drivers import SyntheticCollector, SyntheticDriver
    driver = SyntheticDriver(tenant=R.SYNTHETIC_TENANT, other_tenant=R.SYNTHETIC_OTHER)
    with tempfile.TemporaryDirectory() as out:
        state = X.run(trial, driver, SyntheticCollector(driver), out, trial_id=TRIAL_ID, order=order)
        records = X.read_records(out)
    assert state['status'] == 'completed' and state['recorded'] == 36 and [r['variant'] for r in records] == [i['variant'] for i in order]
    # Several Runs: costs summed for the interaction, the per-Run cap judged on the largest Run.
    merged = X.merge_evidence([{'model': {'calls': 1, 'ms': 10}, 'tool': {'calls': 1, 'ms': 5}, 'costs': {'model': {'amount': '1.5', 'currencies': ['CNY']}},
                                'intents': {'fulfilled': 1}, 'intent_ids': ['a'], 'refusals': {}},
                               {'model': {'calls': 2, 'ms': 20}, 'tool': {'calls': 0, 'ms': 0}, 'costs': {'model': {'amount': '1.0', 'currencies': ['CNY']}},
                                'intents': {'fulfilled': 1}, 'intent_ids': ['b'], 'refusals': {'NXC05': 1}}])
    assert merged['model'] == {'calls': 3, 'ms': 30} and merged['costs'] == {'model': {'amount': '2.5', 'currencies': ['CNY']}}
    assert merged['intents'] == {'fulfilled': 2} and merged['intent_ids'] == ['a', 'b'] and merged['refusals'] == {'NXC05': 1}
    within = {'facts': {}, 'outcome': 'ok', 'cost': {'total': '2.5', 'max_run': '1.5'}}
    assert X.evaluate(within, [], trial, cumulative=X.Decimal('2.5'), monthly=X.Decimal('2.5')) == (None, [])
    over = {'facts': {}, 'outcome': 'ok', 'cost': {'total': '2.5', 'max_run': '2.1'}}
    assert X.evaluate(over, [], trial, cumulative=X.Decimal('2.5'), monthly=X.Decimal('2.5')) == ('stop', ['per_run_cost_exceeded'])


def test_a_failed_run_s_tool_reaches_the_interaction_record(tmp_path):
    """10/18 stage (Run 1ab1d785): the record names which tool failed (the Run evidence's failure, 0204), codes only."""
    from nexloop_eios.interaction_drivers import SyntheticCollector, SyntheticDriver

    class Failing(SyntheticCollector):
        def collect(self, run_id):
            return {**super().collect(run_id), 'failure': {'code': 'runtime_failed', 'failure_code': 'runtime_tool_error',
                                                           'failure_detail': 'invalid_effect_request', 'failure_tool': 'nexloop.enterprise.deliver'}}
    driver = SyntheticDriver(tenant=R.SYNTHETIC_TENANT, other_tenant=R.SYNTHETIC_OTHER)
    X.run(X.load_plan(R.TRIAL), driver, Failing(driver), tmp_path / 't', trial_id=TRIAL_ID, order=ORDER)
    record = X.read_records(tmp_path / 't')[0]
    assert record['failures'] == [{'code': 'runtime_failed', 'failure_code': 'runtime_tool_error', 'failure_detail': 'invalid_effect_request',
                                   'failure_tool': 'nexloop.enterprise.deliver'}]
    import evidence_pack as E
    E.build(tmp_path / 'pack', {}, tmp_path / 't')
    assert E.check(tmp_path / 'pack') == [] and json.loads((tmp_path / 'pack' / 'index.json').read_text())['items']['interactions']['dropped_values'] == 0
