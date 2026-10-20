-- NX-041 S6 leftover (L4 integration note): S6 gives each simulation world its model budget when the world is registered (trigger
-- nx041_sim_world_model_budget); worlds registered before S6 have none. As S5 backfilled the clocks and S7 the counts, this gives
-- every active simulation world without one the same row: CNY, the monthly cap as the limit, its lifetime as the period, revision 1.
-- The tenant's monthly simulation cap (control.nexloop_sim_model_spend) applies to them as to every other world. A world being
-- deleted gets none (its rows are leaving). Repeatable: a world with a row is left as it is. No existing function changes. The
-- migration number lives only in the file name and the catalog.
create function control.nexloop_sim_world_model_budget_backfill() returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);t text;n integer;v_total integer:=0;
begin
 -- Tenant by tenant (the registry is under row security: each tenant's worlds are visible with its own setting).
 for t in select tenant_id from control.nexloop_tenants order by tenant_id loop
  perform set_config('eios.tenant_id',t,true);
  insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
   select w.tenant_id,w.world,'model','CNY',runtime.nexloop_sim_model_monthly_cap(),w.created_at,w.expires_at,1
   from control.nexloop_sim_worlds w where w.tenant_id=t and w.status='active'
  on conflict(tenant_id,world,budget_kind) do nothing;
  get diagnostics n=row_count;v_total:=v_total+n;
 end loop;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return v_total;
end $$;
alter function control.nexloop_sim_world_model_budget_backfill() owner to nexloop_owner;
revoke all on function control.nexloop_sim_world_model_budget_backfill() from public;
select control.nexloop_sim_world_model_budget_backfill();
