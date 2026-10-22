-- EIOS gap B5 (NX-061): outcome classes, so that a failure rate counts real failures only (upstream RunOutcomeClass, rewritten:
-- nexloop_eios.run_outcome_taxonomy holds the same table, a test keeps both equal).
-- 1. control.nexloop_outcome_code_classes: the class of every dispatch refusal code (0131 and later). Append-only, as the codes.
-- 2. The snapshot is wrapped once more (the 0277/0318/0320 pattern) and gains `outcomes`, counted as events of the last hour:
--    * every dispatch refusal (Run and effect path) by its code's class; a code without a class is 'unresolved' (a failure);
--    * every effect intent created in the hour by its state: fulfilled/confirmed = succeeded, unknown = outcome_unknown (to be
--      reconciled: neither), failed = defect, but only without a refusal of its own: 0152 fails a refused one-reply intent, and
--      that refusal is already counted under its class (a contact restriction is not a failure);
--    * the runtime queues' dead letters of the hour (the base snapshot's queues.*.dead_lettered_last_hour) = defect.
--    failure_rate = (transient + authorization + defect + unresolved) / decided; failure_rate_counting_refusals is what a rate that
--    took every non-success as a failure would show, for comparison. Counts and ratios only.
create table control.nexloop_outcome_code_classes (
 code text primary key references control.nexloop_dispatch_refusal_codes(code),
 outcome_class text not null check(outcome_class in ('succeeded','no_op','superseded','conflict_protected','outcome_unknown','transient',
  'authorization','defect'))
);
alter table control.nexloop_outcome_code_classes owner to nexloop_owner;
create trigger nx061_append_only before update or delete on control.nexloop_outcome_code_classes for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_outcome_code_classes from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
insert into control.nexloop_outcome_code_classes values
 ('NXC01','conflict_protected'),('NXC02','superseded'),('NXC03','superseded'),('NXC04','superseded'),('NXC05','conflict_protected'),
 ('NXC06','conflict_protected'),('NXC07','conflict_protected'),('NXB01','conflict_protected'),('NXB02','transient'),('NXB03','authorization'),
 ('NXM01','conflict_protected'),('NXR01','conflict_protected'),('NXE01','authorization');

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_outcomes;
revoke all on function runtime.nexloop_metrics_snapshot_before_outcomes(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s jsonb:=runtime.nexloop_metrics_snapshot_before_outcomes(p_tenant,p_world);now_at timestamptz:=clock_timestamp();
 classes jsonb;failures numeric;decided numeric;protected numeric;
begin
 with refusals as (
   select coalesce(c.outcome_class,'unresolved') k,count(*) n from runtime.nexloop_dispatch_refusals r
    left join control.nexloop_outcome_code_classes c on c.code=r.code
    where r.tenant_id=p_tenant and r.world=p_world and r.recorded_at>now_at-interval '1 hour' group by 1),
  intents as (
   select case i.state when 'fulfilled' then 'succeeded' when 'confirmed' then 'succeeded' when 'unknown' then 'outcome_unknown'
     when 'failed' then 'defect' end k,count(*) n from runtime.nexloop_effect_intents i
    where i.tenant_id=p_tenant and i.world=p_world and i.created_at>now_at-interval '1 hour' and i.state in ('fulfilled','confirmed','unknown','failed')
     and not (i.state='failed' and exists(select 1 from runtime.nexloop_dispatch_refusals r where r.tenant_id=i.tenant_id and r.world=i.world
      and r.intent_id=i.intent_id))
    group by 1),
  dead as (select 'defect' k,coalesce(sum((q.value->>'dead_lettered_last_hour')::numeric),0) n from jsonb_each(coalesce(s->'queues','{}'::jsonb)) q
    where jsonb_typeof(q.value)='object' and jsonb_typeof(q.value->'dead_lettered_last_hour')='number'),
  allk as (select k,sum(n) n from (select * from refusals union all select * from intents union all select * from dead) x group by k)
 select coalesce(jsonb_object_agg(k,n),'{}'::jsonb) into classes from allk where n>0;
 failures:=coalesce((classes->>'transient')::numeric,0)+coalesce((classes->>'authorization')::numeric,0)+coalesce((classes->>'defect')::numeric,0)
  +coalesce((classes->>'unresolved')::numeric,0);
 protected:=coalesce((classes->>'conflict_protected')::numeric,0)+coalesce((classes->>'superseded')::numeric,0);
 decided:=failures+protected+coalesce((classes->>'succeeded')::numeric,0)+coalesce((classes->>'no_op')::numeric,0)+coalesce((classes->>'outcome_unknown')::numeric,0);
 return s||jsonb_build_object('outcomes',jsonb_build_object('window_seconds',3600,'classes',classes,'decided',decided,'failures',failures,
  'failure_rate',case when decided=0 then 0 else round(failures/decided,4) end,
  'failure_rate_counting_refusals',case when decided=0 then 0 else round((failures+protected)/decided,4) end,
  'unresolved',coalesce((classes->>'unresolved')::numeric,0)));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
