"""Dispatcher 2026-10-13 (scripts/load/collect.py residue): the template's residual due feed items are those of the load tenant,
pending, due now and created before the cell started; items created during the cell, scheduled for later, dead-lettered,
plan-reevaluate or of another tenant are not. Synthetic rows, rolled back."""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nexloop_eios.bootstrap import bootstrap

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/load'))
import collect  # noqa: E402


class Rollback(Exception):
    pass


def test_residue_counts_only_due_template_items_by_feed(admin):
    bootstrap(admin)
    tenant, world = collect.TENANT, collect.WORLD
    cell_start = datetime.now(UTC)
    old, new = cell_start - timedelta(hours=1), cell_start + timedelta(seconds=1)
    rows = [('recall-instance', 'a', old, 'pending', old), ('recall-instance', 'b', old, 'pending', old), ('claim-match', 'c', old, 'pending', old),
            ('recall-instance', 'new', new, 'pending', new),                      # created during the cell
            ('commitment-monitor', 'later', old, 'pending', cell_start + timedelta(hours=1)),  # scheduled
            ('claim-match', 'dead', old, 'dead_lettered', old), ('plan-reevaluate', 'plan', old, 'pending', old)]
    try:
        with admin.transaction():
            admin.execute('set local session_replication_role=replica')
            for feed, key, created, status, available in rows:
                admin.execute('insert into runtime.nexloop_work_feed(tenant_id,world,feed,item_key,payload,created_at,available_at,status) values(%s,%s,%s,%s,%s,%s,%s,%s)',
                              (tenant, world, feed, key, '{}', created, available, status))
            admin.execute("insert into runtime.nexloop_work_feed(tenant_id,world,feed,item_key,payload,created_at,available_at) values('other-tenant',%s,'recall-instance','x','{}',%s,%s)",
                          (world, old, old))
            result = collect.residue(admin, cell_start)
            raise Rollback()
    except Rollback:
        pass
    assert result == {'claim-match': 1, 'recall-instance': 2}
