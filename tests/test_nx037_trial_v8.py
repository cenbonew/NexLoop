"""Release 27 with batch 7 (L6 audit 2026-10-21: the newest trial v7 named alert rules v9, so v10's clock skew alert never reached
stage): the trial configuration v8 is v7 with alert rules v10 (clock_skew); v7 stays as published. The tenant init and the interaction
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
V7, V8 = CONFIG / 'nx037-trial.v7.json', CONFIG / 'nx037-trial.v8.json'
ARGS = dict(tenant='11111111-1111-4111-8111-111111111111', owner_principal='synthetic-owner', value_catalog=V8, channel_ref='synthetic-channel',
            period_start='2026-10-21T00:00:00+08:00', period_end='2026-10-23T00:00:00+08:00')


def test_v8_is_v7_with_alert_rules_v10_and_v7_is_unchanged():
    v7, v8 = json.loads(V7.read_text()), json.loads(V8.read_text())
    assert v7['alert_rules'] == 'alert-rules.v9.json' and v8['alert_rules'] == 'alert-rules.v10.json'
    assert v8['schema'] == 'nexloop.nx037-trial.v8'
    same = lambda d: {k: v for k, v in d.items() if k not in ('schema', 'decisions', 'alert_rules')}  # noqa: E731
    assert same(v8) == same(v7)
    assert load_plan(V8)['cost_caps'] == load_plan(V7)['cost_caps']


def test_init_and_driver_follow_the_newest_trial_configuration():
    assert latest_trial(CONFIG) == V8 and I.TRIAL == V8 and R.TRIAL == V8
    p = I.parameters(**ARGS)
    rules = p['alert_rules']
    assert rules['version'] == 10 and load_rules(CONFIG / 'alert-rules.v10.json') == rules
    ids = {r['rule_id'] for r in rules['rules']}
    assert 'clock_skew' in ids  # v10's new rule
    assert {'schema_release_regressing', 'evolution_release_regressing', 'outreach_approvals_waiting'} <= ids  # earlier ones stay
    assert next(s for s in I.steps(p) if s['step'] == 'alert_rules') == {'step': 'alert_rules', 'kind': 'trusted_configuration', 'version': 10}
    assert I.parameters(**ARGS, trial=V7)['alert_rules']['version'] == 9


def test_the_dry_run_plan_names_alert_rules_v10(capsys):
    argv = ['plan', '--tenant', ARGS['tenant'], '--owner-principal', 'synthetic-owner', '--value-catalog', str(V8), '--channel-ref', 'synthetic-channel',
            '--period-start', ARGS['period_start'], '--period-end', ARGS['period_end']]
    code = I.main(argv)
    out = capsys.readouterr().out
    assert code == 0 and '"alert_rules"' in out and '"version": 10' in out.replace('"version":10', '"version": 10')
    code = I.main(argv + ['--trial-config', str(V7)])
    assert code == 0 and '"version": 9' in capsys.readouterr().out.replace('"version":9', '"version": 9')
