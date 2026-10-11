"""NX-052: write cost of a tenant foreign key, measured on a disposable bootstrapped cluster only.

For one table and its `<table>_tenant_fk` constraint, each round measures autocommit single-row inserts twice: with the
constraint dropped, then re-added (NOT VALID + VALIDATE). Reported per variant: client-side median and p95 statement time;
one EXPLAIN ANALYZE sample of the foreign key trigger; and a lock probe in which another session holds the tenant row
`FOR UPDATE` (as 0050 does) while an insert runs. Criteria (NX-052 batch 5 gate, also run on cold tables as a method
check): median +10 % at most, p95 +5 % at most. Never point this at a shared or production database: it drops and re-adds
a constraint and writes synthetic rows, which it deletes afterwards.
"""
import argparse
import json
import statistics
import threading
import time
import uuid

import psycopg

TABLES = {
    # table: (insert sql with %s for tenant and a unique key, cleanup sql). A test-mode connector row with throwaway key
    # bytes generated in the database; no load script writes the login rate policy (test_load_matrix).
    'control.nexloop_commercial_connectors': (
        "insert into control.nexloop_commercial_connectors(connector_id,tenant_id,world,data_mode,accepted_types,currencies,key_id,key_material)"
        " values(%s,%s,'test','test','{payment.succeeded}','{CNY}','fk-cost',extensions.gen_random_bytes(32))",
        "delete from control.nexloop_commercial_connectors where connector_id like 'fk-cost-%%'"),
}
MEDIAN_LIMIT, P95_LIMIT = 1.10, 1.05


def _constraint(table):
    return table.split('.', 1)[1] + '_tenant_fk'


def _set_fk(db, table, present):
    name = _constraint(table)
    db.execute(f'alter table {table} drop constraint if exists {name}')
    if present:
        db.execute(f'alter table {table} add constraint {name} foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid')
        db.execute(f'alter table {table} validate constraint {name}')


def _inserts(db, table, tenant, rows):
    sql = TABLES[table][0]
    times = []
    for _ in range(rows):
        start = time.perf_counter_ns()
        db.execute(sql, ('fk-cost-' + uuid.uuid4().hex[:20], tenant))
        times.append((time.perf_counter_ns() - start) / 1000)
    db.execute(TABLES[table][1])
    return times


def _quantiles(times):
    ordered = sorted(times)
    return {'median_us': statistics.median(ordered), 'p95_us': ordered[max(0, int(len(ordered) * 0.95) - 1)]}


def _trigger_sample(db, table, tenant):
    # EXPLAIN ANALYZE executes the insert; the transaction is rolled back so the sample row never persists.
    with db.transaction(force_rollback=True):
        plan = db.execute('explain (analyze, format json) ' + TABLES[table][0], ('fk-cost-explain', tenant)).fetchone()[0]
    return [{'calls': t['Calls'], 'time_ms': t['Time']} for t in plan[0].get('Triggers', []) if t['Trigger Name'].startswith('RI_ConstraintTrigger')]


def _lock_probe(dsn, db, table, tenant, hold_seconds):
    holder = psycopg.connect(dsn)
    ready = threading.Event()

    def hold():
        with holder.transaction():
            holder.execute('select 1 from control.nexloop_tenants where tenant_id=%s for update', (tenant,))
            ready.set()
            time.sleep(hold_seconds)

    thread = threading.Thread(target=hold)
    thread.start()
    ready.wait()
    start = time.perf_counter()
    db.execute(TABLES[table][0], ('fk-cost-lock', tenant))
    waited = time.perf_counter() - start
    thread.join()
    holder.close()
    db.execute(TABLES[table][1])
    return waited


def measure(dsn, table, tenant, rows=2000, rounds=3, hold_seconds=0.3):
    """The tenant row must exist; the constraint is left present and validated."""
    report = {'table': table, 'rows': rows, 'rounds': rounds, 'rounds_detail': []}
    with psycopg.connect(dsn, autocommit=True) as db:
        for _ in range(rounds):
            detail = {}
            for present in (False, True):
                _set_fk(db, table, present)
                detail['with_fk' if present else 'without_fk'] = _quantiles(_inserts(db, table, tenant, rows))
            report['rounds_detail'].append(detail)
        report['fk_trigger_sample'] = _trigger_sample(db, table, tenant)
        _set_fk(db, table, False)
        no_fk_wait = _lock_probe(dsn, db, table, tenant, hold_seconds)
        _set_fk(db, table, True)
        fk_wait = _lock_probe(dsn, db, table, tenant, hold_seconds)
        report['lock_probe'] = {'tenant_row_for_update_held_s': hold_seconds, 'insert_wait_without_fk_s': round(no_fk_wait, 4),
                                'insert_wait_with_fk_s': round(fk_wait, 4), 'blocked_by_for_update': fk_wait >= hold_seconds * 0.8}
    for variant in ('without_fk', 'with_fk'):
        report[variant] = {k: statistics.median(r[variant][k] for r in report['rounds_detail']) for k in ('median_us', 'p95_us')}
    ratio = {k: report['with_fk'][k] / report['without_fk'][k] for k in ('median_us', 'p95_us')}
    report['ratio'] = {k: round(v, 4) for k, v in ratio.items()}
    report['within_criteria'] = ratio['median_us'] <= MEDIAN_LIMIT and ratio['p95_us'] <= P95_LIMIT
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dsn', required=True, help='admin DSN of a disposable bootstrapped cluster')
    parser.add_argument('--table', default='control.nexloop_commercial_connectors', choices=sorted(TABLES))
    parser.add_argument('--tenant', required=True, help='an existing synthetic tenant id')
    parser.add_argument('--rows', type=int, default=2000)
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(measure(args.dsn, args.table, args.tenant, args.rows, args.rounds), indent=2))


if __name__ == '__main__':
    main()
