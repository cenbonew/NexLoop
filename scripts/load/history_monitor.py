"""NX-031 AT-060 ladder (100 / 200 / 500 / 1000 customers): read-only progress samples while a load template replies to its
history. Every `interval` seconds one JSON line goes to <template>/history-monitor.jsonl:

  {"at", "elapsed_s", "open": {unrouted, run_tasks, replies_due}, "replied", "unreplied": {by_design, outcome_unknown, missing},
   "jobs": {"<status>:<code>": n},
   "guard": {calls, errors, timeouts, p50_ms_max, p95_ms_max, processes}, "host": {active_runs, waiting, admission_timeouts},
   "relay_handoffs", "dead_letters"}

Numbers and codes only: no message text, no IDs, no credentials. The guard and Host figures are the process samples recorded
(recorded_at) since the previous line (each guard process writes its own window; p50/p95 are the worst process). It only reads.
"""
import json
import time
from datetime import UTC, datetime
from pathlib import Path


def _rows(db, sql, args=()):
    try:
        return db.execute(sql, args).fetchall()
    except Exception:  # an absent table in an older catalog is a missing figure, never a failed build
        return []


def unreplied(db, tenant):
    """Replies still due, split (counts only): by_design = the consumer is under an active contact restriction and has no reply
    effect (the restriction refused it); outcome_unknown = a reply effect exists for the consumer but its delivery result is not
    known yet (accepted, dispatching or unknown: queried and reconciled, never resent); missing = everything else."""
    rows = _rows(db, """select case
          when exists(select 1 from runtime.nexloop_effect_intents i where i.tenant_id=f.tenant_id and i.consumer_id=f.payload->>'consumer_id'
                      and i.state in ('accepted','dispatching','unknown')) then 'outcome_unknown'
          when exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=f.tenant_id and r.consumer_id=f.payload->>'consumer_id' and r.active)
           and not exists(select 1 from runtime.nexloop_effect_intents i where i.tenant_id=f.tenant_id and i.consumer_id=f.payload->>'consumer_id') then 'by_design'
          else 'missing' end,count(*)
        from runtime.nexloop_work_feed f where f.tenant_id=%s and f.feed='reply-due' and f.status='pending' group by 1""", (tenant,))
    out = {'by_design': 0, 'outcome_unknown': 0, 'missing': 0}
    out.update(dict(rows))
    return out


def sample(db, tenant, since):
    """One read-only sample of the template's history progress; `since` bounds the process samples (the previous line)."""
    open_ = dict(_rows(db, """select 'unrouted',count(*) from runtime.nexloop_message_outbox where tenant_id=%s and status='pending'
        union all select 'run_tasks',count(*) from runtime.jobs where queue='operations' and status in ('pending','retry_wait','running')
        union all select 'replies_due',count(*) from runtime.nexloop_work_feed where tenant_id=%s and feed='reply-due' and status='pending'""",
                       (tenant, tenant)))
    replied = (_rows(db, """select count(*) from runtime.nexloop_conversation_messages where tenant_id=%s and record->>'direction'='outbound'
        and record->>'trigger_message_id' is not null""", (tenant,)) or [(None,)])[0][0]
    jobs = {f'{s}:{c}': n for s, c, n in _rows(db, """select status,coalesce(result->>'code',''),count(*) from runtime.jobs
        where queue='operations' group by 1,2 order by 1,2""")}
    guard = {'calls': 0, 'errors': 0, 'timeouts': 0, 'p50_ms_max': None, 'p95_ms_max': None, 'processes': 0}
    for (value,) in _rows(db, "select sample from runtime.nexloop_process_samples where kind='guard' and recorded_at>=%s", (since,)):
        guard['processes'] += 1
        for key in ('calls', 'errors', 'timeouts'):
            guard[key] += int(value.get(key) or 0)
        for key, out in (('p50_ms', 'p50_ms_max'), ('p95_ms', 'p95_ms_max')):
            if value.get(key) is not None:
                guard[out] = value[key] if guard[out] is None else max(guard[out], value[key])
    host = {'active_runs': None, 'waiting': None, 'admission_timeouts': 0}
    for (value,) in _rows(db, "select sample from runtime.nexloop_process_samples where kind='host' and recorded_at>=%s order by recorded_at", (since,)):
        host['active_runs'], host['waiting'] = value.get('active_runs'), value.get('waiting')
        host['admission_timeouts'] += int(value.get('admission_timeouts') or 0)
    handoffs = (_rows(db, 'select count(*) from runtime.nexloop_relay_handoffs where tenant_id=%s', (tenant,)) or [(None,)])[0][0]
    dead = (_rows(db, "select count(*) from runtime.jobs where status='dead_lettered'") or [(None,)])[0][0]
    return {'open': {k: open_.get(k) for k in ('unrouted', 'run_tasks', 'replies_due')}, 'replied': replied, 'unreplied': unreplied(db, tenant), 'jobs': jobs,
            'guard': guard, 'host': host, 'relay_handoffs': handoffs, 'dead_letters': dead}


class HistoryMonitor:
    """Appends one sample line every `interval` seconds when tick() is called (the history loop calls it every few seconds)."""

    def __init__(self, path, tenant, *, interval=30.0, clock=time.monotonic, now=lambda: datetime.now(UTC)):
        self.path, self.tenant, self.interval, self.clock, self.now = Path(path), tenant, interval, clock, now
        self.started = self.clock()
        self.last = None
        self.since = self.now()

    def tick(self, db, *, force=False):
        at = self.clock()
        if not force and self.last is not None and at - self.last < self.interval:
            return None
        stamp = self.now()
        line = {'at': stamp.isoformat(), 'elapsed_s': round(at - self.started, 1), **sample(db, self.tenant, self.since)}
        with self.path.open('a') as out:
            out.write(json.dumps(line, separators=(',', ':'), sort_keys=True) + '\n')
        self.last, self.since = at, stamp
        return line
