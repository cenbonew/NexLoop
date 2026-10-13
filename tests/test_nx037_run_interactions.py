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
