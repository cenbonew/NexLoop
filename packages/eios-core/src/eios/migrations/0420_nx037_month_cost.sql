-- NX-037 monthly cap (temporary number 0420; D-g option 2, owner 2026-10-13). The controlled interaction executor enforces the
-- tenant's monthly model cost cap; it now starts from what the tenant already spent this month instead of 0. Read-only, for
-- the metrics role, one number: the tenant's model cost entries (0120) in one currency since the start of the month (UTC),
-- major units only, never converted between currencies.
create function authz.nexloop_nx037_month_model_cost(p_tenant text,p_world text,p_currency text) returns numeric
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare total numeric;
begin
 if p_currency is null or p_currency !~ '^[A-Z]{3}$' or p_world is distinct from 'real' then raise exception 'month cost unavailable' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(sum(e.amount),0) into total from runtime.nexloop_cost_entries e
  where e.tenant_id=p_tenant and e.world=p_world and e.cost_kind='model' and e.currency=p_currency and e.amount_unit='major'
   and e.occurred_at>=date_trunc('month',clock_timestamp() at time zone 'UTC') at time zone 'UTC';
 return total;
end $$;
alter function authz.nexloop_nx037_month_model_cost(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_nx037_month_model_cost(text,text,text) from public;
grant execute on function authz.nexloop_nx037_month_model_cost(text,text,text) to nexloop_metrics;
