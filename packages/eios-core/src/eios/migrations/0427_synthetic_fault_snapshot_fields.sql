-- ADR-045 follow-up (dispatcher 2026-11-08, for L9's run-state page): the metrics snapshot block synthetic_fault gains expires_at and
-- members. Both describe the live switch (not closed, not expired, remaining > 0: runtime.nexloop_synthetic_fault_live, 0423) and are
-- JSON null when no switch is live - never a stale or invented value. members is the count of the live switch's Consumers
-- (control.nexloop_synthetic_fault_members after any erasure), never an id. The snapshot is renamed once and wrapped (the 0423
-- pattern); active, remaining and injected_last_hour are unchanged. Temporary number; the dispatcher renumbers.
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_fault_fields;
revoke all on function runtime.nexloop_metrics_snapshot_before_fault_fields(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_fault_fields(p_tenant,p_world);prior text:=current_setting('eios.tenant_id',true);
 s control.nexloop_synthetic_fault_switches;n integer;
begin
 if jsonb_typeof(v->'synthetic_fault') is distinct from 'object' then return v;end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select (runtime.nexloop_synthetic_fault_live(p_tenant,p_world)).* into s;
 if s.switch_id is not null then
  select count(*) into n from control.nexloop_synthetic_fault_members m where m.tenant_id=p_tenant and m.world=p_world and m.switch_id=s.switch_id;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v||jsonb_build_object('synthetic_fault',(v->'synthetic_fault')||jsonb_build_object(
  'expires_at',case when s.switch_id is null then 'null'::jsonb else to_jsonb(to_char(s.expires_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"')) end,
  'members',case when s.switch_id is null then 'null'::jsonb else to_jsonb(n) end));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
