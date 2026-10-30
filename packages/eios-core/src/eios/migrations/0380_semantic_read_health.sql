-- Semantic read health (I29 / L6 S60, dispatcher 2026-10-31; temporary number, renumbered at merge). R60's positive S6 probe
-- came back with miss_reason no_readable_types: the relay's service principal could read no definition type, every semantic
-- read was empty and nothing said so. The metrics snapshot (world real) gains semantic_reads for alert rules v24:
--   accesses_1h            semantic access records (kind access) in the last hour
--   no_readable_types_1h   of those, the ones whose caller could read no definition type at all (miss_reason no_readable_types)
-- Counts only; no term hash, ref or Run id leaves the function.
create function runtime.nexloop_semantic_read_health(p_tenant text,p_world text,p_now timestamptz) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select jsonb_build_object('accesses_1h',count(*),'no_readable_types_1h',count(*) filter (where a.miss_reason='no_readable_types')) into v
  from runtime.nexloop_semantic_accesses a
  where a.tenant_id=p_tenant and a.world=p_world and a.kind='access' and a.recorded_at>p_now-interval '1 hour';
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_semantic_read_health(text,text,timestamptz) owner to nexloop_owner;
revoke all on function runtime.nexloop_semantic_read_health(text,text,timestamptz) from public;

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_semantic_reads;
revoke all on function runtime.nexloop_metrics_snapshot_before_semantic_reads(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_semantic_reads(p_tenant,p_world);
begin
 if p_world is distinct from 'real' then return v;end if;
 return v||jsonb_build_object('semantic_reads',runtime.nexloop_semantic_read_health(p_tenant,p_world,clock_timestamp()));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
