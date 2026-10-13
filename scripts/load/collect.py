"""NX-031 slice 1: what a cell writes into its results directory (docs/implementation/NX-031-results-contract.md §2).

Read-only queries on the cell's own disposable cluster (superuser of that throwaway cluster, never a persistent database).
Every row is codes, counts, latencies, timestamps or opaque IDs; no message text, names or keys (D8). `runs.jsonl` carries no
run_id (contract §2).
"""
import json

TENANT, WORLD = '00000000-0000-4000-8000-00000000000a', 'real'  # tests/support/tenants.TENANT_A (NX-052)


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


# A message Run: its route (message → run → task), the task's first claim (job_events 'running'), its terminal summary.
_RUNS = """with r as (
  select m.message_id,m.run_id,m.task_id,m.command->>'consumer_ref' consumer_ref,(cm.record->>'accepted_at')::timestamptz accepted_at,cv.consumer_id,
         j.created_at task_created,j.status task_status,j.result,j.updated_at task_updated,
         (select min(e.created_at) from runtime.job_events e where e.job_id=m.task_id and e.status='running') claimed_at
  from runtime.nexloop_message_routes m
  join runtime.nexloop_conversation_messages cm on cm.tenant_id=m.tenant_id and cm.world=m.world and cm.message_id=m.message_id
  join runtime.nexloop_conversations cv on cv.tenant_id=cm.tenant_id and cv.world=cm.world and cv.conversation_id=cm.conversation_id
  left join runtime.jobs j on j.job_id=m.task_id
  where m.tenant_id=%s and m.world=%s)"""


def runs(db, start, end):
    """D6 per message Run whose trigger was accepted in the window (contract §2; no run_id): end to end (trigger accepted → its
    reply, fallback reply or escalation), model time (the Host's model_timing, else the sum of the Run's model requests), tool time and calls (the Dispatcher's
    tool_timing), queue wait (task created → first claimed), Run duration (Host accepted: first activation → task ended), failure (the
    allowlisted authorization failure_code of a failed Run, else the task code). admission_wait_ms stays null: the Host's admission wait has no
    per-Run record (NX-030 M09 is not delivered)."""
    rows = db.execute(_RUNS + """
      select r.accepted_at,r.result,r.task_created,r.claimed_at,
        (select min((o.record->>'accepted_at')::timestamptz) from runtime.nexloop_conversation_messages o
          where o.tenant_id=%s and o.world=%s and o.record->>'direction'='outbound' and o.record->>'trigger_message_id'=r.message_id) replied_at,
        exists(select 1 from authz.nexloop_message_fallback_issuances f where f.message_id=r.message_id) fallback,
        (select e.escalated_at from control.nexloop_reply_escalations e where e.tenant_id=%s and e.world=%s and e.message_id=r.message_id) escalated_at,
        exists(select 1 from control.nexloop_takeover_settlements s where s.message_id=r.message_id) takeover,
        exists(select 1 from control.nexloop_contact_restrictions c where c.tenant_id=%s and c.world=%s and c.consumer_id=r.consumer_id and c.active) restricted,
        (select sum(extract(epoch from q.completed_at-q.requested_at)*1000) from runtime.nexloop_model_requests q where q.run_id=r.run_id and q.completed_at is not null) model_ms,
        (select min(a.created_at) from authz.nexloop_runtime_activations a where a.run_id=r.run_id::uuid) host_accepted,
        case when r.task_status in ('succeeded','failed') then r.task_updated end run_ended
      from r where r.accepted_at>=%s and r.accepted_at<%s order by r.accepted_at""",
      (TENANT, WORLD, TENANT, WORLD, TENANT, WORLD, TENANT, WORLD, start, end)).fetchall()
    out = []
    for accepted, result, created, claimed, replied, fallback, escalated, takeover, restricted, model_ms, host_accepted, run_ended in rows:
        timing = (result or {}).get('tool_timing') if isinstance(result, dict) else None
        # AT-060 model time (dispatcher 2026-10-11, A2): the Host's model_timing in the task result (every Run kind); the v6 model
        # request records only when it is absent.
        host_model = (result or {}).get('model_timing') if isinstance(result, dict) else None
        if isinstance(host_model, dict) and isinstance(host_model.get('total_ms'), (int, float)):
            model_ms = host_model['total_ms']
        done = replied or escalated
        outcome = ('fallback' if fallback else 'replied') if replied else 'escalated' if escalated else 'takeover' if takeover else 'restricted' if restricted else 'in_progress'
        out.append({'e2e_ms': round((done - accepted).total_seconds() * 1000, 1) if done else None,
                    'model_ms': round(float(model_ms), 1) if model_ms is not None else None,
                    'tool_ms': timing.get('total_ms') if timing else None, 'tool_calls': timing.get('calls') if timing else None,
                    'queue_wait_ms': round((claimed - created).total_seconds() * 1000, 1) if claimed and created else None,
                    # Run duration (dispatcher 2026-10-11, ruling 2): the Host accepted the Run (the runtime worker's first activation of it,
                    # registered right before the Host start; earlier than any Pi task) → its task ended. Model and tool time fall inside
                    # this span, so the platform time (this minus model and tool) includes the authorization waits.
                    'run_ms': round((run_ended - host_accepted).total_seconds() * 1000, 1) if run_ended and host_accepted else None,
                    'admission_wait_ms': None, 'outcome': outcome,
                    # NX-031: an unanswered guard and a denial counted apart (null for a Run that did not fail).
                    'failure': (result.get('failure_code') or result.get('code')) if isinstance(result, dict) and result.get('runtime_outcome') == 'failed' else None})
    return out


def dispatch(db, start, end):
    """Idle dispatch (owner 2026-10-13, 4A, ROADMAP §7; NX-031 design §4.6): per message Run whose trigger was accepted in the
    window, in ms: relay (message accepted -> its Run task created), queue (task created -> first claimed, runtime.job_events
    running), dispatch_ms = relay + queue (the gated caliber), start (claimed -> the Agent Host's first execution authorization,
    authz.nexloop_runtime_execution_markers.first_authorized_at) and run_start_ms (task created -> first authorization: the
    former caliber, an observation). A segment whose end has not happened is null; model time is never part of any."""
    rows = db.execute(_RUNS + """ select r.accepted_at,r.task_created,r.claimed_at,
          (select x.first_authorized_at from authz.nexloop_runtime_execution_markers x where x.run_id=r.run_id::uuid)
        from r where r.accepted_at>=%s and r.accepted_at<%s""", (TENANT, WORLD, start, end)).fetchall()
    ms = lambda a, b: round((b - a).total_seconds() * 1000, 1) if a and b else None
    return [{'relay_ms': ms(accepted, created), 'queue_ms': ms(created, claimed), 'dispatch_ms': ms(accepted, claimed),
             'start_ms': ms(claimed, authorized), 'run_start_ms': ms(created, authorized)}
            for accepted, created, claimed, authorized in rows if created]


def checks(db, start, end, cross_access):
    """Design §4.4: identity and outcome checks over the cell's window (IDs compared, never text)."""
    one = lambda sql, *a: db.execute(sql, a).fetchone()
    # The SQL helpers below (e.g. runtime.nexloop_inbound_answered) are security definer with row security: they see the tenant's
    # rows only under its eios.tenant_id (without it every inbound message would read as unanswered).
    db.execute("select set_config('eios.tenant_id',%s,false)", (TENANT,))
    # 1. Run binding: each message Run in the window was issued for the consumer of its trigger message's conversation.
    run_binding = one(_RUNS + """ select count(*),count(*) filter (where r.consumer_ref is distinct from 'consumer:'||r.consumer_id) from r
        where r.accepted_at>=%s and r.accepted_at<%s""", TENANT, WORLD, start, end)
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
    # 4. Model request context (AT-060, design §4.4 item 4): every ID a model request's context sources cite is the consumer of
    #    that request's own context (control.nexloop_effect_contexts via the request's context_id, for message, fallback and
    #    role Runs alike), one of that consumer's conversations or messages, or not a Consumer / Conversation / Message at all
    #    (IDs compared, never text). A request whose context has no consumer counts as a violation (fail closed). `checked` is the
    #    number of model requests; `ids_compared` the cited IDs actually compared (0 means nothing was compared).
    model = one("""with q as (select q.run_id,q.call_sequence,q.context_id,e.consumer_id from runtime.nexloop_model_requests q
          left join control.nexloop_effect_contexts e on e.context_id=q.context_id and e.tenant_id=q.tenant_id and e.world=q.world
          where q.tenant_id=%s and q.world=%s and q.requested_at>=%s and q.requested_at<%s),
        cited as (select q.run_id,q.call_sequence,q.consumer_id,(regexp_matches(s.ref,'[0-9a-f]{64}','g'))[1] id from q join runtime.nexloop_context_sources s
          on s.tenant_id=%s and s.world=%s and s.context_id=q.context_id)
        select (select count(*) from q),(select count(*) from cited),
          (select count(*) from q where q.consumer_id is null)+(select count(*) from cited c where c.consumer_id is not null and (
          exists(select 1 from ontology.objects o where o.tenant_id=%s and o.world=%s and o.type_name='Consumer' and o.object_id=c.id and o.object_id<>c.consumer_id)
          or exists(select 1 from runtime.nexloop_conversations cv where cv.tenant_id=%s and cv.world=%s and cv.conversation_id=c.id and cv.consumer_id<>c.consumer_id)
          or exists(select 1 from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations cv on cv.tenant_id=m.tenant_id and cv.world=m.world
                    and cv.conversation_id=m.conversation_id where m.tenant_id=%s and m.world=%s and m.message_id=c.id and cv.consumer_id<>c.consumer_id)))""",
        TENANT, WORLD, start, end, TENANT, WORLD, TENANT, WORLD, TENANT, WORLD, TENANT, WORLD)
    # 6. One reply the customer can see per inbound message, over the whole tenant (a duplicate is never acceptable), split by
    #    where the trigger message comes from: the template's history (accepted before the window) or the cell's window.
    duplicate = one('''select count(*),count(*) filter (where n>1),count(*) filter (where n>1 and in_window),count(*) filter (where n>1 and not in_window)
        from (select o.trigger_message_id,count(*) n,bool_or((t.record->>'accepted_at')::timestamptz>=%s) in_window from runtime.nexloop_outbound_messages o
          left join runtime.nexloop_conversation_messages t on t.tenant_id=o.tenant_id and t.world=o.world and t.message_id=o.trigger_message_id
          where o.tenant_id=%s and o.world=%s and o.delivery_state in ('provider_accepted','delivered') group by 1) x''', start, TENANT, WORLD)
    # 7. Every inbound message accepted in the window has an outcome: answered, settled by a takeover, or escalated.
    unresolved = one('''select count(*),count(*) filter (where not (
            runtime.nexloop_inbound_answered(m.tenant_id,m.world,m.message_id)
            or exists(select 1 from control.nexloop_takeover_settlements s where s.message_id=m.message_id)
            or exists(select 1 from control.nexloop_reply_escalations e where e.tenant_id=m.tenant_id and e.world=m.world and e.message_id=m.message_id)))
        from runtime.nexloop_conversation_messages m where m.tenant_id=%s and m.world=%s and not (m.record ? 'direction')
        and (m.record->>'accepted_at')::timestamptz>=%s and (m.record->>'accepted_at')::timestamptz<%s''', TENANT, WORLD, start, end)
    return {'run_binding': {'checked': run_binding[0], 'violations': run_binding[1]}, 'outbound_ownership': {'checked': outbound[0], 'violations': outbound[1]},
            'claim_ownership': {'checked': claims[0], 'violations': claims[1]}, 'model_context': {'checked': model[0], 'ids_compared': model[1], 'violations': model[2]},
            'cross_access_probe': cross_access, 'duplicate_effects': {'checked': duplicate[0], 'violations': duplicate[1],
                                                                   'window_violations': duplicate[2], 'history_violations': duplicate[3]},
            'unresolved_work': {'checked': unresolved[0], 'unresolved': unresolved[1]}}


def _has(db, table, column):
    schema, name = table.split('.')
    return db.execute('select 1 from information_schema.columns where table_schema=%s and table_name=%s and column_name=%s',
                      (schema, name, column)).fetchone() is not None


def residue(db, before):
    """Due work feed items of the load tenant created before `before` (the cell's start: what the template clone brought), by
    feed; plan-reevaluate excluded as in drained(). Counts only."""
    return dict(db.execute("select feed,count(*) from runtime.nexloop_work_feed where tenant_id=%s and world=%s and status='pending' "
                           "and feed<>'plan-reevaluate' and available_at<=clock_timestamp() and created_at<%s group by 1 order by 1",
                           (TENANT, WORLD, before)).fetchall())


def drained(db):
    """Pending work of the load tenant: queues (pending/retry_wait/running), due work feeds and inbound messages the relay has not
    routed. Only the load tenant counts: the template also holds the base fixture's tenant, whose items no cell service serves
    (counting them kept every cell from draining)."""
    jobs = db.execute("select count(*) from runtime.jobs where tenant_id=%s and status in ('pending','retry_wait','running')", (TENANT,)).fetchone()[0]
    # plan-reevaluate is excluded: the load plans are data only and the cells run no plan reevaluator (dispatcher 2026-10-11, option B).
    # Items scheduled for later (commitment due stages, reply-due before its time) are not pending work yet.
    feeds = db.execute("select count(*) from runtime.nexloop_work_feed where tenant_id=%s and world=%s and status='pending' and feed<>'plan-reevaluate' "
                       "and available_at<=clock_timestamp()", (TENANT, WORLD)).fetchone()[0]
    outbox = db.execute("select count(*) from runtime.nexloop_message_outbox where tenant_id=%s and world=%s and status='pending'", (TENANT, WORLD)).fetchone()[0]
    return jobs + feeds + outbox


def jobs_summary(db):
    """Diagnostics for cell.json: durable queue items by queue, status and result code (codes only, no payloads)."""
    return [{'queue': q, 'status': s, 'code': c, 'count': n} for q, s, c, n in db.execute('''select queue,status,coalesce(result->>'code',last_error_code,''),count(*)
        from runtime.jobs group by 1,2,3 order by 1,2,3''').fetchall()] if _has(db, 'runtime.jobs', 'last_error_code') else \
        [{'queue': q, 'status': s, 'code': c, 'count': n} for q, s, c, n in db.execute('''select queue,status,coalesce(result->>'code',''),count(*)
        from runtime.jobs group by 1,2,3 order by 1,2,3''').fetchall()]


def run_durations(db):
    """Diagnostics for cell.json: message Run tasks, claimed → finished (succeeded or failed), seconds; p50 / p95 / max / count."""
    rows = [r[0] for r in db.execute("""select extract(epoch from j.updated_at-(select min(e.created_at) from runtime.job_events e where e.job_id=j.job_id and e.status='running'))
        from runtime.jobs j where j.queue='operations' and j.status in ('succeeded','failed')""").fetchall() if r[0] is not None] \
        if _has(db, 'runtime.jobs', 'queue') else []
    if not rows:
        return {'count': 0}
    rows.sort()
    return {'count': len(rows), 'p50_s': round(float(rows[len(rows) // 2]), 2), 'p95_s': round(float(rows[max(0, int(len(rows) * 0.95) - 1)]), 2), 'max_s': round(float(rows[-1]), 2)}
