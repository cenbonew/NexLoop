"""NX-030 pure checks: the shipped alert rules (D3) and the text export renderer (D1); no database."""
import json
from pathlib import Path

from nexloop_eios.observability import load_rules, main, render_prometheus

RULES = Path(__file__).resolve().parents[1] / 'deploy/configuration/alert-rules.v1.json'


def test_shipped_rules_cover_the_design_thresholds():
    rules = {r['rule_id']: r for r in load_rules(RULES)['rules']}
    assert rules['unknown_oldest']['threshold'] == 900 and rules['unknown_oldest_escalated']['threshold'] == 7200
    assert rules['guard_timeout_rate']['threshold'] == 0.01 and rules['connections_high']['threshold'] == 50
    assert rules['backup_age']['threshold'] == 26 * 3600 and rules['pool_waiting']['for_seconds'] == 300
    assert {r['severity'] for r in rules.values()} == {'warning', 'critical'}


def test_cli_check_and_render_only_numbers(capsys):
    assert main(['rules', '--manifest', str(RULES), '--check']) == 0
    assert json.loads(capsys.readouterr().out)['valid'] is True
    text = render_prometheus([{'tenant_id': 't', 'world': 'real', 'refusals': {'last_5m': {'NXB01': 2}}, 'guard': {'status': 'unavailable'},
                               'cost': {'budgets': {'model': {'limit': '10.00', 'consumed': '2.50', 'unit': 'CNY', 'ratio': 0.25}}}}])
    assert 'nexloop_refusals_last_5m{key="NXB01",tenant="t",world="real"} 2' in text
    assert 'nexloop_cost_budgets_consumed{key="model",tenant="t",world="real"} 2.5' in text
    assert 'CNY' not in text and 'unavailable' not in text
