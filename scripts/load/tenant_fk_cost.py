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

HEX = lambda: uuid.uuid4().hex + uuid.uuid4().hex  # noqa: E731  64 hex characters
SHORT = lambda: 'fk-cost-' + uuid.uuid4().hex[:20]  # noqa: E731
WEIGHTS = '{"core_term_containment":0.25,"lexical_similarity":0.25,"rule_whitelist":0.25,"vector_cluster":0.25}'
CONVERSATION = 'f' * 64
TABLES = {
    # insert: %s for the unique key, then the tenant. setup/teardown run once with the tenant. No load script writes the login
    # rate policy (test_load_matrix), so the cold sample is a test-mode connector row with throwaway key bytes.
    'control.nexloop_commercial_connectors': dict(key=SHORT,
        insert="insert into control.nexloop_commercial_connectors(connector_id,tenant_id,world,data_mode,accepted_types,currencies,key_id,key_material)"
               " values(%s,%s,'test','test','{payment.succeeded}','{CNY}','fk-cost',extensions.gen_random_bytes(32))",
        cleanup="delete from control.nexloop_commercial_connectors where connector_id like 'fk-cost-%%'"),
    # 3a: a cold definition table.
    'ontology.nexloop_merge_configurations': dict(key=SHORT,
        insert="insert into ontology.nexloop_merge_configurations(config_version,tenant_id,weights,merge_threshold,dedupe_threshold)"
               " values(%s,%s,'" + WEIGHTS + "'::jsonb,0.8,0.9)",
        cleanup="delete from ontology.nexloop_merge_configurations where config_version like 'fk-cost-%%'"),
    # 3b: one extraction run per extracted input; it already references its conversation (tenant, world, conversation_id).
    'ontology.nexloop_extraction_runs': dict(key=HEX,
        setup=["insert into runtime.nexloop_conversations(tenant_id,world,conversation_id,consumer_id,principal_id,idempotency_key)"
               " values(%s,'real','" + CONVERSATION + "','" + 'e' * 64 + "','fk-cost-principal','fk-cost') on conflict do nothing"],
        insert="insert into ontology.nexloop_extraction_runs(input_digest,tenant_id,world,conversation_id,consumer_id,principal_id,extractor_version,"
               "prompt_version,provider,model_id,timezone,message_refs,rejected) values(%s,%s,'real','" + CONVERSATION + "','" + 'e' * 64 + "',"
               "'fk-cost-principal','fk-cost','fk-cost','fk-cost','fk-cost','UTC','[]','[]')",
        cleanup="delete from ontology.nexloop_extraction_runs where principal_id='fk-cost-principal'",
        teardown=["delete from runtime.nexloop_conversations where principal_id='fk-cost-principal' and tenant_id=%s"]),
    # 3c: the conversation root (one row per conversation) and the message inbox (one row per accepted message).
    'runtime.nexloop_conversations': dict(key=HEX,
        insert="insert into runtime.nexloop_conversations(conversation_id,tenant_id,world,consumer_id,principal_id,idempotency_key)"
               " values(%s,%s,'real','" + 'e' * 64 + "','fk-cost-principal',gen_random_uuid()::text)",
        cleanup="delete from runtime.nexloop_conversations where principal_id='fk-cost-principal'"),
    'runtime.nexloop_message_inbox': dict(key=HEX,
        insert="insert into runtime.nexloop_message_inbox(message_id,tenant_id,world,event_id) values(%s,%s,'real','fk-cost-'||gen_random_uuid())",
        cleanup="delete from runtime.nexloop_message_inbox where event_id like 'fk-cost-%%'"),
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
    spec = TABLES[table]
    times = []
    for _ in range(rows):
        key = spec['key']()
        start = time.perf_counter_ns()
        db.execute(spec['insert'], (key, tenant))
        times.append((time.perf_counter_ns() - start) / 1000)
    db.execute(spec['cleanup'])
    return times


def _quantiles(times):
    ordered = sorted(times)
    return {'median_us': statistics.median(ordered), 'p95_us': ordered[max(0, int(len(ordered) * 0.95) - 1)]}


def _trigger_sample(db, table, tenant):
    # EXPLAIN ANALYZE executes the insert; the transaction is rolled back so the sample row never persists.
    with db.transaction(force_rollback=True):
        plan = db.execute('explain (analyze, format json) ' + TABLES[table]['insert'], (TABLES[table]['key'](), tenant)).fetchone()[0]
    return [{'calls': t['Calls'], 'time_ms': t['Time']} for t in plan[0].get('Triggers', []) if t['Trigger Name'].startswith('RI_ConstraintTrigger')]


def _lock_probe(dsn, db, table, tenant, hold_seconds, lock='update'):
    holder = psycopg.connect(dsn)
    ready = threading.Event()

    def hold():
        with holder.transaction():
            holder.execute('select 1 from control.nexloop_tenants where tenant_id=%s for ' + {'update': 'update', 'no_key_update': 'no key update'}[lock], (tenant,))
            ready.set()
            time.sleep(hold_seconds)

    thread = threading.Thread(target=hold)
    thread.start()
    ready.wait()
    start = time.perf_counter()
    db.execute(TABLES[table]['insert'], (TABLES[table]['key'](), tenant))
    waited = time.perf_counter() - start
    thread.join()
    holder.close()
    db.execute(TABLES[table]['cleanup'])
    return waited


def measure(dsn, table, tenant, rows=2000, rounds=3, hold_seconds=0.3):
    """The tenant row must exist; the constraint is left present and validated."""
    report = {'table': table, 'rows': rows, 'rounds': rounds, 'rounds_detail': []}
    spec = TABLES[table]
    with psycopg.connect(dsn, autocommit=True) as db:
        for statement in spec.get('setup', ()):
            db.execute(statement, (tenant,))
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
        # The proposed 0050 change: FOR NO KEY UPDATE does not conflict with the foreign key check's FOR KEY SHARE.
        no_key_wait = _lock_probe(dsn, db, table, tenant, hold_seconds, lock='no_key_update')
        for statement in spec.get('teardown', ()):
            db.execute(statement, (tenant,))
        report['lock_probe'] = {'tenant_row_lock_held_s': hold_seconds, 'insert_wait_without_fk_s': round(no_fk_wait, 4),
                                'insert_wait_with_fk_s': round(fk_wait, 4), 'blocked_by_for_update': fk_wait >= hold_seconds * 0.8,
                                'insert_wait_with_fk_under_no_key_update_s': round(no_key_wait, 4),
                                'blocked_by_for_no_key_update': no_key_wait >= hold_seconds * 0.8}
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
