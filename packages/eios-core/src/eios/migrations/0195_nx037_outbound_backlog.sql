-- NX-037 real-mode start guard (scripts/nx037/trial_guards.py): is the outbound recorder keeping up? For the read-only metrics
-- role, numbers only: the tenant's Agent replies already accepted or delivered by the channel that are still not written into
-- their conversation (materialized_at null), and how many of them have waited more than 60 s since their delivery state
-- changed. Nothing is written. Owner function under row security for the tenant (eios.tenant_id for its own transaction).
create function runtime.nexloop_nx037_outbound_backlog(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select jsonb_build_object('pending',count(*),
   'stalled',count(*) filter (where o.delivery_changed_at<clock_timestamp()-interval '60 seconds'),
   'oldest_age_seconds',coalesce(floor(extract(epoch from clock_timestamp()-min(o.delivery_changed_at))),0))
  into v from runtime.nexloop_outbound_messages o
  where o.tenant_id=p_tenant and o.world=p_world and o.delivery_state in ('provider_accepted','delivered') and o.materialized_at is null;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_nx037_outbound_backlog(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_nx037_outbound_backlog(text,text) from public;
create function authz.nexloop_nx037_outbound_backlog(p_tenant text,p_world text) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$ select runtime.nexloop_nx037_outbound_backlog(p_tenant,p_world) $$;
alter function authz.nexloop_nx037_outbound_backlog(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_nx037_outbound_backlog(text,text) from public;
grant execute on function authz.nexloop_nx037_outbound_backlog(text,text) to nexloop_metrics;
