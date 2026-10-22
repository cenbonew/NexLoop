-- X7 third round S8 attribution (read only; counts, Action names, time buckets and principal kinds - no person, no payload value).
-- psql -v tenant=<tenant uuid> -v from='<S4 time, UTC>' -v to='<S8 time, UTC>' -f x7_real_attribution.sql
begin transaction read only;
set local timezone = 'UTC';
select set_config('eios.tenant_id', :'tenant', true);

-- 1. Alert events in the window: which rules fired or resolved, per hour.
select 'alert_events' as source, rule_id, kind, severity, date_trunc('hour', recorded_at) as hour, count(*)
from control.nexloop_alert_events
where tenant_id = :'tenant' and world = 'real' and recorded_at >= :'from'::timestamptz and recorded_at < :'to'::timestamptz
group by 2, 3, 4, 5 order by 5, 2, 3;

-- 2. Retention sweeps touched in the window: the class, when, and the names of the counts it recorded.
select 'retention_sweeps' as source, item_class, date_trunc('hour', last_started_at) as started_hour, date_trunc('hour', last_completed_at) as completed_hour,
       (select string_agg(k, ',' order by k) from jsonb_object_keys(last_counts) k) as count_keys
from runtime.nexloop_retention_sweeps
where tenant_id = :'tenant' and world = 'real'
  and (last_started_at >= :'from'::timestamptz or last_completed_at >= :'from'::timestamptz)
order by 2;

-- 3. Action claims active in the window: the Action, its state, the principal's kind, per hour (finalized time, else the lease end).
select 'action_claims' as source, c.action_name, c.claim->>'state' as state,
       case when exists(select 1 from control.nexloop_browser_memberships m where m.tenant_id = c.tenant_id and m.principal_id = c.principal_id)
            then 'workbench_member' else 'service_or_run' end as principal_kind,
       date_trunc('hour', coalesce((c.claim->'terminal_outcome'->>'finalized_at')::timestamptz, (c.claim->>'lease_expires_at')::timestamptz)) as hour,
       count(*), count(distinct c.principal_id) as principals
from runtime.nexloop_action_claims c
where c.tenant_id = :'tenant' and c.world = 'real'
  and coalesce((c.claim->'terminal_outcome'->>'finalized_at')::timestamptz, (c.claim->>'lease_expires_at')::timestamptz) >= :'from'::timestamptz
  and coalesce((c.claim->'terminal_outcome'->>'finalized_at')::timestamptz, (c.claim->>'lease_expires_at')::timestamptz) < :'to'::timestamptz + interval '1 hour'
group by 2, 3, 4, 5 order by 5, 2;

-- 4. Claims of the window that the new X7 digest still compares (anything here fails real_side_unchanged and needs its own reason).
select 'claims_still_compared' as source, c.action_name, count(*)
from runtime.nexloop_action_claims c
where c.tenant_id = :'tenant' and c.world = 'real'
  and coalesce((c.claim->'terminal_outcome'->>'finalized_at')::timestamptz, (c.claim->>'lease_expires_at')::timestamptz) >= :'from'::timestamptz
  and not (c.action_name = any(array['nexloop.strategy.publish','nexloop.strategy.retire','nexloop.experiment.register','nexloop.experiment.start',
        'nexloop.experiment.stop','nexloop.experiment.report','nexloop.shadow_copy.set','nexloop.sim_world.create','nexloop.sim_world.delete',
        'nexloop.sim_world.publish_catalog','nexloop.sim_world.generate','nexloop.sim_clock.control','nexloop.consumer_twin.publish',
        'nexloop.consumer_twin.retire','nexloop.consumer_twin.calibrate','nexloop.consumer_twin.erase_text'])
       and exists(select 1 from control.nexloop_browser_memberships m where m.tenant_id = c.tenant_id and m.principal_id = c.principal_id))
group by 2 order by 2;
-- 5. Orphan check: world-real claims still active. lease_state 'expired' = nobody renews them any more (an orphaned lease, a defect of
--    its own); 'live' = still held by a running worker. Age buckets only.
select 'active_claims' as source, c.action_name,
       case when (c.claim->>'lease_expires_at')::timestamptz > clock_timestamp() then 'live' else 'expired' end as lease_state,
       case when (c.claim->>'lease_expires_at')::timestamptz > clock_timestamp() then 'not_expired'
            when (c.claim->>'lease_expires_at')::timestamptz > clock_timestamp() - interval '10 minutes' then 'under_10_min'
            when (c.claim->>'lease_expires_at')::timestamptz > clock_timestamp() - interval '1 day' then 'under_1_day' else 'older' end as expired_for,
       case when exists(select 1 from control.nexloop_browser_memberships m where m.tenant_id = c.tenant_id and m.principal_id = c.principal_id)
            then 'workbench_member' else 'service_or_run' end as principal_kind,
       count(*), max((c.claim->>'claim_revision')::int) as max_revision
from runtime.nexloop_action_claims c
where c.tenant_id = :'tenant' and c.world = 'real' and c.claim->>'state' = 'active'
group by 2, 3, 4, 5 order by 2, 3;
rollback;
