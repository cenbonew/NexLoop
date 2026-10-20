"""Release 26 with batches 5 and 6 (owner 2026-10-21: release 26 moves to alert rules v9): the trial configuration v7 is v6 with alert
rules v9 (NX-040 F4's schema_release_regressing); v6 stays as published. The tenant init and the interaction driver read the newest
trial configuration, so `real_tenant_init configure` delivers alert rules v9; --trial-config still names an earlier version explicitly."""
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
V6, V7 = CONFIG / 'nx037-trial.v6.json', CONFIG / 'nx037-trial.v7.json'
ARGS = dict(tenant='11111111-1111-4111-8111-111111111111', owner_principal='synthetic-owner', value_catalog=V7, channel_ref='synthetic-channel',
            period_start='2026-10-21T00:00:00+08:00', period_end='2026-10-23T00:00:00+08:00')


def test_v7_is_v6_with_alert_rules_v9_and_v6_is_unchanged():
    v6, v7 = json.loads(V6.read_text()), json.loads(V7.read_text())
    assert v6['alert_rules'] == 'alert-rules.v8.json' and v7['alert_rules'] == 'alert-rules.v9.json'
    assert v7['schema'] == 'nexloop.nx037-trial.v7'
    same = lambda d: {k: v for k, v in d.items() if k not in ('schema', 'decisions', 'alert_rules')}  # noqa: E731
    assert same(v7) == same(v6)
    assert load_plan(V7)['cost_caps'] == load_plan(V6)['cost_caps']


def test_init_and_driver_follow_the_newest_trial_configuration():
    assert latest_trial(CONFIG) == V7 and I.TRIAL == V7 and R.TRIAL == V7
    p = I.parameters(**ARGS)
    rules = p['alert_rules']
    assert rules['version'] == 9 and load_rules(CONFIG / 'alert-rules.v9.json') == rules
    ids = {r['rule_id'] for r in rules['rules']}
    assert 'schema_release_regressing' in ids  # v9's new rule
    assert {'evolution_release_regressing', 'outreach_unsubscribe_rate_high', 'outreach_approvals_waiting'} <= ids  # v8's stay
    assert next(s for s in I.steps(p) if s['step'] == 'alert_rules') == {'step': 'alert_rules', 'kind': 'trusted_configuration', 'version': 9}
    assert I.parameters(**ARGS, trial=V6)['alert_rules']['version'] == 8


def test_the_dry_run_plan_names_alert_rules_v9(capsys):
    argv = ['plan', '--tenant', ARGS['tenant'], '--owner-principal', 'synthetic-owner', '--value-catalog', str(V7), '--channel-ref', 'synthetic-channel',
            '--period-start', ARGS['period_start'], '--period-end', ARGS['period_end']]
    code = I.main(argv)
    out = capsys.readouterr().out
    assert code == 0 and '"alert_rules"' in out and '"version": 9' in out.replace('"version":9', '"version": 9')
    code = I.main(argv + ['--trial-config', str(V6)])
    assert code == 0 and '"version": 8' in capsys.readouterr().out.replace('"version":8', '"version": 8')
