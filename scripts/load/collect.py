"""NX-031 slice 1: what a cell writes into its results directory (docs/implementation/NX-031-results-contract.md §2).

Read-only queries on the cell's own disposable cluster (superuser of that throwaway cluster, never a persistent database).
Every row is codes, counts, latencies, timestamps or opaque IDs; no message text, names or keys (D8). `runs.jsonl` carries no
run_id (contract §2).
"""
import json

TENANT, WORLD = 'synthetic-a', 'real'


def jsonl(path, rows):
    with open(path, 'a') as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str, separators=(',', ':')) + '\n')


def snapshot(db):
    return db.execute('select runtime.nexloop_metrics_snapshot(%s,%s)', (TENANT, WORLD)).fetchone()[0]


def process_samples(db, start, end):
    return [{'kind': k, 'service': s, 'instance': i, 'window_start': w, 'window_seconds': n, 'sample': sample}
            for k, s, i, w, n, sample in db.execute('''select kind,service,instance,window_start,window_seconds,sample from runtime.nexloop_process_samples
                where recorded_at>=%s and recorded_at<%s order by recorded_at''', (start, end)).fetchall()]


def refusals(db, start, end):
    return [{'code': c, 'at': a} for c, a in db.execute('''select code,recorded_at from runtime.nexloop_dispatch_refusals
        where tenant_id=%s and world=%s and recorded_at>=%s and recorded_at<%s order by recorded_at''', (TENANT, WORLD, start, end)).fetchall()]


def alerts(db, start, end):
    return [{'rule_id': r, 'severity': s, 'kind': k, 'at': a} for r, s, k, a in db.execute('''select rule_id,severity,kind,recorded_at
        from control.nexloop_alert_events where tenant_id=%s and world=%s and recorded_at>=%s and recorded_at<%s order by recorded_at''',
        (TENANT, WORLD, start, end)).fetchall()]


def runs(db, start, end):
    """Per message Run decomposition (D6). Message Runs are not started by this slice's stack (see stack.py), so there is
    nothing to decompose; an empty file lets the report mark the decomposition missing instead of inventing it."""
    return []


def dispatch(db, start, end):
    """accepted_at → the message Run's task claimed; empty while the message Run chain is not started (stack.py)."""
    return []


def checks(db, start, end, cross_access):
    """Design §4.4: identity and outcome checks over the cell's window (IDs compared, never text)."""
    one = lambda sql, *a: db.execute(sql, a).fetchone()
    # 1. Run binding: each message Run's context pack is for the consumer of its trigger message.
    run_binding = one('''select count(*),count(*) filter (where cv.consumer_id is distinct from p.consumer_id) from runtime.nexloop_context_packs p
        join authz.nexloop_message_fallback_issuances f on f.run_id=p.run_id join runtime.nexloop_conversation_messages m on m.message_id=f.message_id
        join runtime.nexloop_conversations cv on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
        where p.created_at>=%s and p.created_at<%s''', start, end) if _has(db, 'runtime.nexloop_context_packs', 'consumer_id') else (0, 0)
    # 2. Outbound ownership: every outbound record (Agent, fallback, staff) in the same conversation as its trigger.
    outbound = one('''select count(*),count(*) filter (where t.conversation_id is distinct from o.conversation_id) from runtime.nexloop_conversation_messages o
        left join runtime.nexloop_conversation_messages t on t.tenant_id=o.tenant_id and t.world=o.world and t.message_id=o.record->>'trigger_message_id'
        where o.tenant_id=%s and o.world=%s and o.record->>'direction'='outbound' and (o.record->>'accepted_at')::timestamptz>=%s and (o.record->>'accepted_at')::timestamptz<%s''',
        TENANT, WORLD, start, end)
    # 3. Claim ownership: each Claim's consumer is the consumer of its conversation.
    claims = one('''select count(*),count(*) filter (where cv.consumer_id is distinct from c.consumer_id) from ontology.nexloop_claims c
        join runtime.nexloop_conversations cv on cv.tenant_id=c.tenant_id and cv.world=c.world and cv.conversation_id=c.conversation_id
        where c.tenant_id=%s and c.world=%s''', TENANT, WORLD)
    # 5 comes from the driver; 4 and 6 need the message Run chain's records (model requests, effects) in the window.
    # 4. Model request context ⊆ the Run's consumer: compared against the context manifest once message Runs exist (not checked
    #    here, so `checked` stays 0 rather than counting requests as verified).
    model = (0, 0)
    duplicate = one('''select count(*),count(*) filter (where n>1) from (select trigger_message_id,count(*) n from runtime.nexloop_outbound_messages
        where tenant_id=%s and world=%s and delivery_state in ('provider_accepted','delivered') group by 1) x''', TENANT, WORLD)
    # 7. Every inbound message accepted in the window has an outcome: answered, settled by a takeover, or escalated.
    unresolved = one('''select count(*),count(*) filter (where not (
            runtime.nexloop_inbound_answered(m.tenant_id,m.world,m.message_id)
            or exists(select 1 from control.nexloop_takeover_settlements s where s.message_id=m.message_id)
            or exists(select 1 from control.nexloop_reply_escalations e where e.tenant_id=m.tenant_id and e.world=m.world and e.message_id=m.message_id)))
        from runtime.nexloop_conversation_messages m where m.tenant_id=%s and m.world=%s and not (m.record ? 'direction')
        and (m.record->>'accepted_at')::timestamptz>=%s and (m.record->>'accepted_at')::timestamptz<%s''', TENANT, WORLD, start, end)
    return {'run_binding': {'checked': run_binding[0], 'violations': run_binding[1]}, 'outbound_ownership': {'checked': outbound[0], 'violations': outbound[1]},
            'claim_ownership': {'checked': claims[0], 'violations': claims[1]}, 'model_context': {'checked': model[0], 'violations': model[1]},
            'cross_access_probe': cross_access, 'duplicate_effects': {'checked': duplicate[0], 'violations': duplicate[1]},
            'unresolved_work': {'checked': unresolved[0], 'unresolved': unresolved[1]}}


def _has(db, table, column):
    schema, name = table.split('.')
    return db.execute('select 1 from information_schema.columns where table_schema=%s and table_name=%s and column_name=%s',
                      (schema, name, column)).fetchone() is not None


def drained(db):
    """Pending work left: queues (pending/retry_wait/running) and work feeds (pending)."""
    jobs = db.execute("select count(*) from runtime.jobs where status in ('pending','retry_wait','running')").fetchone()[0]
    feeds = db.execute("select count(*) from runtime.nexloop_work_feed where status='pending'").fetchone()[0]
    return jobs + feeds


def jobs_summary(db):
    """Diagnostics for cell.json: durable queue items by queue, status and result code (codes only, no payloads)."""
    return [{'queue': q, 'status': s, 'code': c, 'count': n} for q, s, c, n in db.execute('''select queue,status,coalesce(result->>'code',last_error_code,''),count(*)
        from runtime.jobs group by 1,2,3 order by 1,2,3''').fetchall()] if _has(db, 'runtime.jobs', 'last_error_code') else \
        [{'queue': q, 'status': s, 'code': c, 'count': n} for q, s, c, n in db.execute('''select queue,status,coalesce(result->>'code',''),count(*)
        from runtime.jobs group by 1,2,3 order by 1,2,3''').fetchall()]
