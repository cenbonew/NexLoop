"""Clock skew between APP_HOST and the database as a continuous metric (0277) and alert rules v10 (rule clock_skew, |skew| > 2 s).

The alert evaluator samples the skew (database clock minus its own clock at the midpoint of the shortest round trip) before every
evaluation; the snapshot's clock block holds the 5-minute max per service; the rule fires above 2000 ms. The evaluator's clock is
moved by patching nexloop_eios.observability.CLOCK. Alerts are evaluated for the real world only (0131).
Real PostgreSQL through the NX-030 fixture (real evaluator credential on the domain worker role, the real configurator)."""
import json
from pathlib import Path
import time

import pytest

from nexloop_eios import observability
from nexloop_eios.observability import apply_rules, clock_sample, load_rules, validate_rules
from test_observability_pg import RULES, TENANT, observed  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

V9, V10 = RULES.parent / 'alert-rules.v9.json', RULES.parent / 'alert-rules.v10.json'


def test_rules_v10_are_v9_plus_the_clock_skew_rule():
    v9, v10 = json.loads(V9.read_text()), load_rules(V10)
    assert v10['version'] == 10 and v10['rules'][:-1] == v9['rules']
    assert v10['rules'][-1] == {**v10['rules'][-1], 'rule_id': 'clock_skew', 'metric': 'clock.by_service.*.max.skew_abs_ms', 'condition': 'gt',
                                'threshold': 2000, 'for_seconds': 0, 'severity': 'critical'}
    validate_rules(v10)


def test_the_sample_is_numbers_only_and_symmetric():
    assert clock_sample(-3.0004, 0.0021) == {'skew_ms': -3000, 'skew_abs_ms': 3000, 'round_trip_ms': 2}
    assert clock_sample(0.5, 0.001)['skew_abs_ms'] == 500


def firing(admin):
    return admin.execute("select firing,last_value,world from control.nexloop_alert_state where rule_id='clock_skew'").fetchall()


def test_the_evaluator_samples_the_skew_and_the_rule_fires_above_two_seconds(observed, monkeypatch):  # noqa: F811
    o = observed
    admin = o['admin']
    assert apply_rules(load_rules(V10), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    # In sync (the real clock): a sample is written, the block is there, nothing fires.
    assert o['evaluate']()['rules_version'] == 10
    block = admin.execute("select runtime.nexloop_metrics_snapshot(%s,'real')->'clock'", (TENANT,)).fetchone()[0]
    assert block['status'] == 'ok' and block['by_service']['alert-evaluator']['max']['skew_abs_ms'] < 500, block
    assert [f for f, _, _ in firing(admin)] in ([], [False])
    # 0.5 s ahead: below the threshold - no alert (the negative case).
    monkeypatch.setattr(observability, 'CLOCK', lambda: time.time() + 0.5)
    o['evaluate']()
    assert [f for f, _, _ in firing(admin)] in ([], [False]), firing(admin)
    # 3 s ahead: the rule fires, critical, with the measured value; in the real world only.
    monkeypatch.setattr(observability, 'CLOCK', lambda: time.time() + 3)
    o['evaluate']()
    rows = firing(admin)
    assert len(rows) == 1 and rows[0][0] is True and 2900 <= rows[0][1] <= 3100 and rows[0][2] == 'real', rows
    assert admin.execute("select count(*) from control.nexloop_alert_state where world<>'real'").fetchone()[0] == 0
    # The evaluation is real world only (0131: any other world answers alerts_real_world_only): no alert state outside real.
    assert admin.execute("select prosrc like '%%alerts_real_world_only%%' from pg_proc where proname='nexloop_alert_evaluate'").fetchone()[0]


def test_a_failing_skew_sample_never_breaks_the_evaluation(observed, monkeypatch):  # noqa: F811
    o = observed
    monkeypatch.setattr(observability, 'CLOCK', lambda: (_ for _ in ()).throw(OSError('clock unavailable')))
    assert apply_rules(load_rules(V10), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    assert o['evaluate']()['rules_version'] == 10  # best effort, as the pool sample


def test_samples_of_kind_clock_accept_only_their_numbers(observed):  # noqa: F811
    import psycopg
    pool = observed['domain']
    observability.record_sample(pool, 'clock', 'alert-evaluator', {'skew_ms': 1, 'skew_abs_ms': 1, 'round_trip_ms': 1})
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='process sample invalid'):
        observability.record_sample(pool, 'clock', 'alert-evaluator', {'skew_ms': 1, 'host': 'x'})
    assert Path(V10).exists()
