-- NX-037 run entry: the real dispatch switches of one tenant as the run entry compares them with the
-- owner's interaction plan before a real-mode trial: the tenant layer (enabled, data_mode) and the resource ids of the open
-- Action switches. Read-only, for the metrics role; no reasons, principals or intents. The deployment layer is the
-- dispatching process's own environment and is compared there.
create function runtime.nexloop_nx037_open_switches(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select jsonb_build_object(
  'tenant',coalesce((select jsonb_build_object('enabled',s.enabled,'data_mode',s.data_mode) from control.nexloop_real_dispatch_switches s
     where s.tenant_id=p_tenant and s.world=p_world and s.scope_kind='tenant'),jsonb_build_object('enabled',false,'data_mode',null)),
  'actions',(select coalesce(jsonb_agg(s.scope_ref order by s.scope_ref),'[]'::jsonb) from control.nexloop_real_dispatch_switches s
     where s.tenant_id=p_tenant and s.world=p_world and s.scope_kind='action_type' and s.enabled)) into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_nx037_open_switches(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_nx037_open_switches(text,text) from public;
create function authz.nexloop_nx037_open_switches(p_tenant text,p_world text) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$ select runtime.nexloop_nx037_open_switches(p_tenant,p_world) $$;
alter function authz.nexloop_nx037_open_switches(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_nx037_open_switches(text,text) from public;
grant execute on function authz.nexloop_nx037_open_switches(text,text) to nexloop_metrics;
