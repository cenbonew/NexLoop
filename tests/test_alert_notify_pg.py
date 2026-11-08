"""Out-of-workbench alert notification on PostgreSQL (migration alert_notify_export): authz.nexloop_alerts_firing_export() gives the
read-only nexloop_metrics login the firing alerts - dedupe_key, rule_id, severity, first_fired_at, fire_count - and the evaluator's
last evaluation per tenant and world, nothing else (no value, threshold or selector field); a resolved alert is not listed; any other
login is refused. The notifier's own reader (scripts/deploy/alert_notify.read_alerts) is used as deployed. NX-030 fixture."""
import importlib.util
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from test_observability_pg import TENANT, observed  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('alert_notify', ROOT / 'scripts/deploy/alert_notify.py')
N = importlib.util.module_from_spec(spec);spec.loader.exec_module(N)


def alert(admin, key, severity, firing, value=7.5):
    admin.execute("""insert into control.nexloop_alert_state(tenant_id,world,dedupe_key,rule_id,severity,selector,breached_since,firing,first_fired_at,
        last_fired_at,fire_count,last_value,last_evaluated_at,rules_version) values(%s,'real',%s,%s,%s,'synthetic-selector',now(),%s,'2026-11-08T09:00:00Z',now(),3,%s,now(),32)""",
        (TENANT, key, key.split(':')[0], severity, firing, value))


def test_the_metrics_login_reads_only_what_fires_and_only_those_fields(observed):  # noqa: F811
    o = observed;admin, pg = o['admin'], o['pg']
    admin.execute("delete from control.nexloop_alert_state where tenant_id=%s", (TENANT,))
    alert(admin, 'timer_stale_file_backup:app_host:file-backup', 'warning', True)
    alert(admin, 'feed_dead_letter:claim-match', 'critical', True)
    alert(admin, 'evaluator_quiet:x', 'warning', False)
    scopes = [s for s in N.read_alerts(make_conninfo(pg, user='nexloop_metrics')) if s['tenant_id'] == TENANT]
    assert len(scopes) == 1 and scopes[0]['world'] == 'real' and scopes[0]['last_evaluated_at'].endswith('Z')
    firing = scopes[0]['firing']
    assert [a['dedupe_key'] for a in firing] == ['feed_dead_letter:claim-match', 'timer_stale_file_backup:app_host:file-backup']
    assert all(set(a) == {'dedupe_key', 'rule_id', 'severity', 'first_fired_at', 'fire_count'} for a in firing)
    assert firing[0] == {'dedupe_key': 'feed_dead_letter:claim-match', 'rule_id': 'feed_dead_letter', 'severity': 'critical',
                         'first_fired_at': '2026-11-08T09:00:00Z', 'fire_count': 3}
    assert 'synthetic-selector' not in str(scopes) and '7.5' not in str(scopes)


@pytest.mark.parametrize('user', ['nexloop_api', 'nexloop_domain_worker'])
def test_any_other_login_is_refused(observed, user):  # noqa: F811
    with psycopg.connect(make_conninfo(observed['pg'], user=user)) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute('select authz.nexloop_alerts_firing_export()')
    with pytest.raises(N.Refused):
        N.read_alerts(make_conninfo(observed['pg'], user=user))
