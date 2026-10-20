"""Release 25 with batches 3 and 4 (owner 2026-10-21, all as recommended): the trial configuration v6 is v5 with alert rules v8 (v7's
evolution_release_regressing, v8's outreach_unsubscribe_rate_high and outreach_approvals_waiting); v5 stays as published. The tenant
init and the interaction driver read the newest trial configuration, so `real_tenant_init configure` delivers alert rules v8;
--trial-config still names an earlier version explicitly."""
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
V5, V6 = CONFIG / 'nx037-trial.v5.json', CONFIG / 'nx037-trial.v6.json'
ARGS = dict(tenant='11111111-1111-4111-8111-111111111111', owner_principal='synthetic-owner', value_catalog=V6, channel_ref='synthetic-channel',
            period_start='2026-10-21T00:00:00+08:00', period_end='2026-10-23T00:00:00+08:00')
NEW_RULES = {'evolution_release_regressing', 'outreach_unsubscribe_rate_high', 'outreach_approvals_waiting'}


def test_v6_is_v5_with_alert_rules_v8_and_v5_is_unchanged():
    v5, v6 = json.loads(V5.read_text()), json.loads(V6.read_text())
    assert v5['alert_rules'] == 'alert-rules.v6.json' and v6['alert_rules'] == 'alert-rules.v8.json'
    assert v6['schema'] == 'nexloop.nx037-trial.v6'
    same = lambda d: {k: v for k, v in d.items() if k not in ('schema', 'decisions', 'alert_rules')}  # noqa: E731
    assert same(v6) == same(v5)
    assert load_plan(V6)['cost_caps'] == load_plan(V5)['cost_caps']


def test_init_and_driver_follow_the_newest_trial_configuration():
    assert latest_trial(CONFIG) == V6 and I.TRIAL == V6 and R.TRIAL == V6
    p = I.parameters(**ARGS)
    rules = p['alert_rules']
    assert rules['version'] == 8 and load_rules(CONFIG / 'alert-rules.v8.json') == rules
    ids = {r['rule_id'] for r in rules['rules']}
    assert NEW_RULES <= ids and {'data_health_extraction_dead_letters', 'data_health_erasure_stalled'} <= ids  # v6's rules stay
    assert next(s for s in I.steps(p) if s['step'] == 'alert_rules') == {'step': 'alert_rules', 'kind': 'trusted_configuration', 'version': 8}
    # An earlier version is still named explicitly.
    assert I.parameters(**ARGS, trial=V5)['alert_rules']['version'] == 6


def test_the_dry_run_plan_names_alert_rules_v8(capsys):
    argv = ['plan', '--tenant', ARGS['tenant'], '--owner-principal', 'synthetic-owner', '--value-catalog', str(V6), '--channel-ref', 'synthetic-channel',
            '--period-start', ARGS['period_start'], '--period-end', ARGS['period_end']]
    code = I.main(argv)
    out = capsys.readouterr().out
    assert code == 0 and '"alert_rules"' in out and '"version": 8' in out.replace('"version":8', '"version": 8')
    code = I.main(argv + ['--trial-config', str(V5)])
    assert code == 0 and '"version": 6' in capsys.readouterr().out.replace('"version":6', '"version": 6')
