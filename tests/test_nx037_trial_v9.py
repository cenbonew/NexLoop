"""NX-043 X5 wiring (L6 review 2026-10-21: alert rules reach stage only through the newest trial's alert_rules): the trial configuration
v9 is v8 with alert rules v11 (experiment_auto_stopped); v8 stays as published. The tenant init and the interaction
driver read the newest trial configuration, so `real_tenant_init configure` delivers alert rules v10; --trial-config still names an
earlier version explicitly."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/nx037'))
import real_tenant_init as I  # noqa: E402
import run_interactions as R  # noqa: E402
from nexloop_eios.controlled_interactions import latest_trial, load_plan  # noqa: E402
from nexloop_eios.observability import load_rules  # noqa: E402

CONFIG = ROOT / 'deploy/configuration'
V8, V9 = CONFIG / 'nx037-trial.v8.json', CONFIG / 'nx037-trial.v9.json'
ARGS = dict(tenant='11111111-1111-4111-8111-111111111111', owner_principal='synthetic-owner', value_catalog=V9, channel_ref='synthetic-channel',
            period_start='2026-10-21T00:00:00+08:00', period_end='2026-10-23T00:00:00+08:00')


def test_v9_is_v8_with_alert_rules_v11_and_v8_is_unchanged():
    v8, v9 = json.loads(V8.read_text()), json.loads(V9.read_text())
    assert v8['alert_rules'] == 'alert-rules.v10.json' and v9['alert_rules'] == 'alert-rules.v11.json'
    assert v9['schema'] == 'nexloop.nx037-trial.v9'
    same = lambda d: {k: v for k, v in d.items() if k not in ('schema', 'decisions', 'alert_rules')}  # noqa: E731
    assert same(v9) == same(v8)
    assert load_plan(V9)['cost_caps'] == load_plan(V8)['cost_caps']


def test_init_and_driver_follow_the_newest_trial_configuration():
    assert latest_trial(CONFIG) == V9 and I.TRIAL == V9 and R.TRIAL == V9
    p = I.parameters(**ARGS)
    rules = p['alert_rules']
    assert rules['version'] == 11 and load_rules(CONFIG / 'alert-rules.v11.json') == rules
    ids = {r['rule_id'] for r in rules['rules']}
    assert 'experiment_auto_stopped' in ids  # v11's new rule
    assert {'clock_skew', 'schema_release_regressing', 'outreach_approvals_waiting'} <= ids  # earlier ones stay
    assert next(s for s in I.steps(p) if s['step'] == 'alert_rules') == {'step': 'alert_rules', 'kind': 'trusted_configuration', 'version': 11}
    assert I.parameters(**ARGS, trial=V8)['alert_rules']['version'] == 10


def test_the_dry_run_plan_names_alert_rules_v11(capsys):
    argv = ['plan', '--tenant', ARGS['tenant'], '--owner-principal', 'synthetic-owner', '--value-catalog', str(V9), '--channel-ref', 'synthetic-channel',
            '--period-start', ARGS['period_start'], '--period-end', ARGS['period_end']]
    code = I.main(argv)
    out = capsys.readouterr().out
    assert code == 0 and '"alert_rules"' in out and '"version": 11' in out.replace('"version":11', '"version": 11')
    code = I.main(argv + ['--trial-config', str(V8)])
    assert code == 0 and '"version": 10' in capsys.readouterr().out.replace('"version":10', '"version": 10')
