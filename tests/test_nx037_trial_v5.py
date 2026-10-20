"""Release 25 (owner 2026-10-20): the trial configuration v5 is v4 with alert rules v6 (data health); v4 stays as published. The
tenant init and the interaction driver read the newest trial configuration, so `real_tenant_init configure` delivers alert rules v6;
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
V4, V5 = CONFIG / 'nx037-trial.v4.json', CONFIG / 'nx037-trial.v5.json'


def test_v5_is_v4_with_alert_rules_v6_and_v4_is_unchanged():
    v4, v5 = json.loads(V4.read_text()), json.loads(V5.read_text())
    assert v4['alert_rules'] == 'alert-rules.v5.json' and v5['alert_rules'] == 'alert-rules.v6.json'
    assert v5['schema'] == 'nexloop.nx037-trial.v5'
    same = lambda d: {k: v for k, v in d.items() if k not in ('schema', 'decisions', 'alert_rules')}
    assert same(v5) == same(v4)
    assert load_plan(V5)['cost_caps'] == load_plan(V4)['cost_caps']


def test_init_and_driver_follow_the_newest_trial_configuration():
    assert latest_trial(CONFIG) == V5 and I.TRIAL == V5 and R.TRIAL == V5
    p = I.parameters(tenant='11111111-1111-4111-8111-111111111111', owner_principal='synthetic-owner', value_catalog=V5, channel_ref='synthetic-channel',
                     period_start='2026-10-21T00:00:00+08:00', period_end='2026-10-23T00:00:00+08:00')
    rules = p['alert_rules']
    assert rules['version'] == 6 and load_rules(CONFIG / 'alert-rules.v6.json') == rules
    assert {'data_health_extraction_dead_letters', 'data_health_erasure_stalled'} <= {r['rule_id'] for r in rules['rules']}
    assert next(s for s in I.steps(p) if s['step'] == 'alert_rules') == {'step': 'alert_rules', 'kind': 'trusted_configuration', 'version': 6}
    # An earlier version is still named explicitly.
    old = I.parameters(tenant='11111111-1111-4111-8111-111111111111', owner_principal='synthetic-owner', value_catalog=V5, channel_ref='synthetic-channel',
                       period_start='2026-10-21T00:00:00+08:00', period_end='2026-10-23T00:00:00+08:00', trial=V4)
    assert old['alert_rules']['version'] == 5


def test_the_dry_run_plan_names_alert_rules_v6(capsys):
    code = I.main(['plan', '--tenant', '11111111-1111-4111-8111-111111111111', '--owner-principal', 'synthetic-owner', '--value-catalog', str(V5),
                   '--channel-ref', 'synthetic-channel', '--period-start', '2026-10-21T00:00:00+08:00', '--period-end', '2026-10-23T00:00:00+08:00'])
    out = capsys.readouterr().out
    assert code == 0 and '"alert_rules"' in out and '"version": 6' in out.replace('"version":6', '"version": 6')
    code = I.main(['plan', '--tenant', '11111111-1111-4111-8111-111111111111', '--owner-principal', 'synthetic-owner', '--value-catalog', str(V5),
                   '--channel-ref', 'synthetic-channel', '--period-start', '2026-10-21T00:00:00+08:00', '--period-end', '2026-10-23T00:00:00+08:00',
                   '--trial-config', str(V4)])
    assert code == 0 and '"version": 5' in capsys.readouterr().out.replace('"version":5', '"version": 5')
