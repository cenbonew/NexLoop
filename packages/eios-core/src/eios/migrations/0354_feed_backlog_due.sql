-- Work feed backlog age counts only work that is due and waiting (stage alert feed_backlog_age:commitment-monitor, firing since 10-16).
-- The migration number is temporary; it lives only in the file name and the catalog.
--
-- feeds.<feed>.oldest_pending_seconds (0131, kept by 0141) was now - min(changed_at) over every pending item, and 0149's
-- oldest_due_seconds was now - min(changed_at) over the due ones. A feed whose items are scheduled for later (the commitment monitor
-- reschedules its item in place for the next stage: available_at moves on, changed_at becomes the time of the last pass, the row stays
-- pending) therefore looked like a backlog from 10 minutes after each pass, until the next stage was due, although nothing was due.
-- Both values now count only items that are due (available_at <= now) and not held by a live lease (being worked on is not waiting;
-- an expired lease counts again), and the wait starts at available_at, the moment the item became claimable: the same measure
-- the job queues use (0131/0141 queues.*.oldest_pending_seconds, from available_at for due items). null when no item waits.
-- pending, due and scheduled keep their meaning (0149). runtime.nexloop_metrics_snapshot is renamed once and wrapped, as 0149 did.
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_feed_backlog_due;
revoke all on function runtime.nexloop_metrics_snapshot_before_feed_backlog_due(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_feed_backlog_due(p_tenant,p_world);prior text:=current_setting('eios.tenant_id',true);
 now_at timestamptz:=clock_timestamp();waiting jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_object_agg(feed,oldest),'{}'::jsonb) into waiting
  from (select feed,floor(extract(epoch from now_at-min(available_at)))::bigint oldest
        from runtime.nexloop_work_feed where tenant_id=p_tenant and world=p_world and status='pending' and available_at<=now_at
         and (lease_until is null or lease_until<=now_at) group by feed) x;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 if jsonb_typeof(v->'feeds') is distinct from 'object' then return v;end if;
 return jsonb_set(v,'{feeds}',coalesce((select jsonb_object_agg(f.key,f.value||jsonb_build_object('oldest_pending_seconds',waiting->f.key,
   'oldest_due_seconds',waiting->f.key)) from jsonb_each(v->'feeds') f),'{}'::jsonb));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
