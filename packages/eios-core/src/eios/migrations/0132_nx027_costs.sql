-- NX-027 (temporary number 0132): cost entries and budget settlement (M19; AT-043), docs/implementation/NX-027-design.md §6.
--
-- 1. D4: every model request result carries the Run budget's currency (the Host already refuses a Run whose trusted
--    provider profile currency differs from the budget currency, so the recorded cost is in that currency).
-- 2. runtime.nexloop_cost_entries (append-only, one entry per source, never added across currencies):
--    model    = each recorded model result with a cost (provider_reported, major currency units);
--    channel  = one unit per effect intent the provider accepted; an amount only when the versioned settings carry a
--               unit rate for the Action (D7: configured_rate), otherwise units only ('unpriced');
--    discount = each incentive budget reservation (budget_reservation, minor units).
--    service / labour entries need a human-owner Action (NX-028 workbench) and are not written here.
-- 3. D5 settlement: when the Run's task reaches a terminal status, its 'run:<id>' model reservation is settled once by
--    an appended release row 'run:<id>:settle' (amount = actual - reserved <= 0, at the reservation's own recorded_at so
--    the period does not move). No release when any request of the Run has no recorded result or an unknown one, or
--    when nothing was reserved; actual above the reservation is recorded, never charged twice. The budget table stays
--    append-only and NXB01 keeps blocking new reservations.
-- 4. Cost entries project to metric observations of a 'cost_entry' metric source (same chain columns as 0131).
-- Published migrations are not edited.

-- 1. Model request currency ------------------------------------------------------------------------------------------
alter table runtime.nexloop_model_requests add column currency text check(currency ~ '^[A-Z]{3}$');
create function runtime.nexloop_model_request_currency() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare v_currency text;
begin
 if tg_op='UPDATE' and old.result_status is null and new.result_status is not null then
  select j.normalized_input->'run_command'->'budget'->>'currency' into v_currency from authz.nexloop_runtime_run_bindings b
   join runtime.jobs j on j.tenant_id=b.tenant_id and j.job_id=b.task_id where b.run_id=new.run_id;
  if v_currency is null or v_currency!~'^[A-Z]{3}$' then raise exception 'model result without a Run budget currency' using errcode='22023';end if;
  new.currency:=v_currency;
 elsif new.currency is distinct from old.currency then raise exception 'model request currency is set with its result' using errcode='22023';
 end if;
 return new;
end $$;
alter function runtime.nexloop_model_request_currency() owner to nexloop_owner;
revoke all on function runtime.nexloop_model_request_currency() from public;
create trigger nx027_model_request_currency before update on runtime.nexloop_model_requests
 for each row execute function runtime.nexloop_model_request_currency();

-- 2. Cost entries ----------------------------------------------------------------------------------------------------
create table runtime.nexloop_cost_entries (
 tenant_id text not null,world text not null,entry_id text not null check(entry_id ~ '^[a-z]+:[A-Za-z0-9._:-]{1,200}$'),
 data_mode text not null check(data_mode in ('real','test','simulation')),check((world='real')=(data_mode='real')),
 cost_kind text not null check(cost_kind in ('model','channel','discount','service','labour')),
 units numeric not null default 1 check(units>0),
 amount numeric(24,8) check(amount>=0),currency text check(currency ~ '^[A-Z]{3}$'),
 amount_unit text check(amount_unit in ('major','minor')),
 check((amount is null)=(currency is null) and (amount is null)=(amount_unit is null)),
 basis text not null check(basis in ('provider_reported','configured_rate','unpriced','budget_reservation','operator_entered')),
 check((basis='unpriced')=(amount is null)),
 source_ref text not null check(char_length(source_ref) between 1 and 300),run_id uuid,consumer_ref text check(char_length(consumer_ref) between 1 and 200),
 corrects_entry_id text,occurred_at timestamptz not null,recorded_at timestamptz not null default clock_timestamp(),recorded_by text not null,
 primary key(tenant_id,world,entry_id)
);
create index nexloop_cost_entries_kind on runtime.nexloop_cost_entries(tenant_id,world,cost_kind,occurred_at);
create index nexloop_cost_entries_run on runtime.nexloop_cost_entries(tenant_id,world,run_id) where run_id is not null;
create table runtime.nexloop_budget_settlements (
 tenant_id text not null,world text not null,consumption_id text not null,
 reserved numeric not null,actual numeric,released numeric not null default 0 check(released>=0),unit text not null,
 outcome text not null check(outcome in ('released','fully_used','exceeded','result_pending_or_unknown')),
 settled_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world,consumption_id)
);
do $tables$
declare t text;
begin
 foreach t in array array['runtime.nexloop_cost_entries','runtime.nexloop_budget_settlements'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
  execute format('create trigger nx027_append_only before update or delete on %s for each row execute function control.nexloop_nx022_append_only()',t);
 end loop;
end $tables$;

create function runtime.nexloop_cost_data_mode(p_world text) returns text language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when p_world='real' then 'real' when p_world in ('simulation','shadow') then 'simulation' else 'test' end
$$;
alter function runtime.nexloop_cost_data_mode(text) owner to nexloop_owner;

-- Observations of 'cost_entry' metric sources: a 'count' source counts units (priced or not); an 'amount' source sums
-- amounts in the definition's own currency and scale (definition unit 'major' or 'minor'); unpriced entries have no
-- amount, and another currency or scale is an excluded observation, never converted.
alter table control.nexloop_metric_observations drop constraint nexloop_metric_observations_excluded_reason_check;
alter table control.nexloop_metric_observations add constraint nexloop_metric_observations_excluded_reason_check
 check(excluded_reason in ('other_currency','other_unit'));
create function runtime.nexloop_cost_project(e runtime.nexloop_cost_entries) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s control.nexloop_metric_sources;m control.nexloop_metric_definitions;n integer:=0;v_excluded text;v_value numeric;
begin
 for s in select * from control.nexloop_metric_sources x where x.tenant_id=e.tenant_id and x.world=e.world and x.source_kind='cost_entry'
   and e.cost_kind=any(x.cost_kinds) order by x.metric_id,x.metric_version loop
  select * into m from control.nexloop_metric_definitions where tenant_id=e.tenant_id and world=e.world and metric_id=s.metric_id and version=s.metric_version;
  if s.value='count' then v_excluded:=null;v_value:=e.units;
  elsif e.amount is null then continue;  -- an unpriced entry has units, never an amount
  else
   v_excluded:=case when m.currency is distinct from e.currency then 'other_currency' when m.unit is distinct from e.amount_unit then 'other_unit' end;
   v_value:=case when v_excluded is null then e.amount else 0 end;
  end if;
  if control.nexloop_metric_project(e.tenant_id,e.world,m,'cost:'||e.entry_id,'ce.'||encode(sha256(convert_to(e.entry_id,'UTF8')),'hex'),true,
    v_value,v_excluded,e.occurred_at,e.data_mode,e.consumer_ref,jsonb_build_array('cost:'||e.entry_id,e.source_ref)) is not null then
   n:=n+1;end if;
 end loop;
 return n;
end $$;
alter function runtime.nexloop_cost_project(runtime.nexloop_cost_entries) owner to nexloop_owner;
revoke all on function runtime.nexloop_cost_project(runtime.nexloop_cost_entries) from public;

create function runtime.nexloop_cost_record(p runtime.nexloop_cost_entries) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);e runtime.nexloop_cost_entries;
begin
 perform set_config('eios.tenant_id',p.tenant_id,true);
 insert into runtime.nexloop_cost_entries(tenant_id,world,entry_id,data_mode,cost_kind,units,amount,currency,amount_unit,basis,source_ref,run_id,
   consumer_ref,corrects_entry_id,occurred_at,recorded_by)
  values(p.tenant_id,p.world,p.entry_id,p.data_mode,p.cost_kind,coalesce(p.units,1),p.amount,p.currency,p.amount_unit,p.basis,p.source_ref,p.run_id,
   p.consumer_ref,p.corrects_entry_id,p.occurred_at,p.recorded_by)
  on conflict do nothing returning * into e;
 if e.entry_id is not null then perform runtime.nexloop_cost_project(e);end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return e.entry_id is not null;
end $$;
alter function runtime.nexloop_cost_record(runtime.nexloop_cost_entries) owner to nexloop_owner;
revoke all on function runtime.nexloop_cost_record(runtime.nexloop_cost_entries) from public;

-- model: each recorded model result with a cost.
create function runtime.nexloop_cost_on_model_result() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare e runtime.nexloop_cost_entries;
begin
 if old.result_status is null and new.result_status is not null and new.cost is not null then
  e.tenant_id:=new.tenant_id;e.world:=new.world;e.entry_id:='model:'||new.run_id||':'||new.call_sequence;e.data_mode:=runtime.nexloop_cost_data_mode(new.world);
  e.cost_kind:='model';e.units:=1;e.amount:=new.cost;e.currency:=new.currency;e.amount_unit:='major';e.basis:='provider_reported';
  e.source_ref:='model-request:'||new.run_id||':'||new.call_sequence;e.run_id:=new.run_id;e.occurred_at:=new.completed_at;e.recorded_by:='nexloop:model-result';
  perform runtime.nexloop_cost_record(e);
 end if;
 return null;
end $$;
alter function runtime.nexloop_cost_on_model_result() owner to nexloop_owner;
revoke all on function runtime.nexloop_cost_on_model_result() from public;
create trigger nx027_cost_model after update on runtime.nexloop_model_requests for each row execute function runtime.nexloop_cost_on_model_result();

-- channel: one unit per effect intent the provider accepted (D7: amount only with a configured unit rate).
create function runtime.nexloop_cost_channel_rate(p_action text,p_version integer) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select r from jsonb_array_elements(runtime.nexloop_commercial_settings_current()->'channel_unit_rates') r
  where r->>'action'=p_action and (r->>'version')::integer=p_version and r->>'currency' ~ '^[A-Z]{3}$'
   and r->>'amount_per_unit' ~ '^[0-9]{1,12}(\.[0-9]{1,8})?$' limit 1
$$;
alter function runtime.nexloop_cost_channel_rate(text,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_cost_channel_rate(text,integer) from public;
create function runtime.nexloop_cost_on_effect_attempt() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);i runtime.nexloop_effect_intents;rate jsonb;e runtime.nexloop_cost_entries;
begin
 if new.state not in ('provider_accepted','observed_fulfilled','fulfilled') or (tg_op='UPDATE' and old.state in ('provider_accepted','observed_fulfilled','fulfilled')) then
  return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select * into i from runtime.nexloop_effect_intents where intent_id=new.intent_id and tenant_id=new.tenant_id and world=new.world;
 rate:=runtime.nexloop_cost_channel_rate(i.action_name,i.action_version);
 e.tenant_id:=new.tenant_id;e.world:=new.world;e.entry_id:='channel:'||new.intent_id;e.data_mode:=runtime.nexloop_cost_data_mode(new.world);
 e.cost_kind:='channel';e.units:=1;e.source_ref:='effect-intent:'||new.intent_id;e.run_id:=new.origin_run_id;e.consumer_ref:=i.consumer_id;
 e.occurred_at:=new.updated_at;e.recorded_by:='nexloop:effect-ledger';
 if rate is null then e.basis:='unpriced';
 else e.basis:='configured_rate';e.amount:=(rate->>'amount_per_unit')::numeric;e.currency:=rate->>'currency';e.amount_unit:='major';end if;
 perform runtime.nexloop_cost_record(e);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_cost_on_effect_attempt() owner to nexloop_owner;
revoke all on function runtime.nexloop_cost_on_effect_attempt() from public;
create trigger nx027_cost_channel after insert or update of state on runtime.nexloop_effect_attempts
 for each row execute function runtime.nexloop_cost_on_effect_attempt();

-- discount: each incentive budget reservation (whole minor units of the budget currency, 0068).
create function control.nexloop_cost_on_incentive() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare e runtime.nexloop_cost_entries;
begin
 if new.budget_kind='incentive' and new.amount>0 then
  e.tenant_id:=new.tenant_id;e.world:=new.world;e.entry_id:='discount:'||new.consumption_id;e.data_mode:=runtime.nexloop_cost_data_mode(new.world);
  e.cost_kind:='discount';e.units:=1;e.amount:=new.amount;e.currency:=new.unit;e.amount_unit:='minor';e.basis:='budget_reservation';
  e.source_ref:='incentive:'||new.consumption_id||'@'||new.source_ref;e.occurred_at:=new.recorded_at;e.recorded_by:=new.principal_id;
  perform runtime.nexloop_cost_record(e);
 end if;
 return null;
end $$;
alter function control.nexloop_cost_on_incentive() owner to nexloop_owner;
revoke all on function control.nexloop_cost_on_incentive() from public;
create trigger nx027_cost_incentive after insert on control.nexloop_budget_consumption for each row execute function control.nexloop_cost_on_incentive();

-- 3. D5 settlement of a Run's model reservation ----------------------------------------------------------------------
alter table control.nexloop_budget_consumption drop constraint nexloop_budget_consumption_amount_check;
alter table control.nexloop_budget_consumption add constraint nexloop_budget_consumption_amount_check
 check(amount<>'Infinity'::numeric and amount<>'-Infinity'::numeric and (amount>0 or (amount<0 and consumption_id ~ ':settle$')));

create function runtime.nexloop_budget_settle_run(p_tenant text,p_world text,p_run text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);res control.nexloop_budget_consumption;v_actual numeric;v_open integer;v_release numeric;v_outcome text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into res from control.nexloop_budget_consumption where tenant_id=p_tenant and world=p_world and budget_kind='model' and consumption_id='run:'||p_run;
 if not found or exists(select 1 from runtime.nexloop_budget_settlements where tenant_id=p_tenant and world=p_world and consumption_id=res.consumption_id) then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);return null;end if;
 -- Conservative: a request without a recorded result, or with an unknown one, keeps the whole reservation.
 select count(*) filter (where result_status is null or result_status='unknown'),coalesce(sum(cost),0) into v_open,v_actual
  from runtime.nexloop_model_requests where tenant_id=p_tenant and world=p_world and run_id=p_run::uuid;
 if v_open>0 then v_outcome:='result_pending_or_unknown';v_release:=0;
 elsif v_actual>res.amount then v_outcome:='exceeded';v_release:=0;
 elsif v_actual=res.amount then v_outcome:='fully_used';v_release:=0;
 else v_outcome:='released';v_release:=res.amount-v_actual;end if;
 if v_release>0 then
  insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id,recorded_at)
   values(p_tenant,p_world,'model',res.consumption_id||':settle',-v_release,res.unit,'settle:'||res.source_ref,'nexloop:budget-settlement',res.recorded_at);
 end if;
 insert into runtime.nexloop_budget_settlements(tenant_id,world,consumption_id,reserved,actual,released,unit,outcome)
  values(p_tenant,p_world,res.consumption_id,res.amount,case when v_open>0 then null else v_actual end,v_release,res.unit,v_outcome);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('consumption_id',res.consumption_id,'outcome',v_outcome,'released',v_release::text);
end $$;
alter function runtime.nexloop_budget_settle_run(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_budget_settle_run(text,text,text) from public;

create function runtime.nexloop_budget_on_job_terminal() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare v_run text:=new.normalized_input->'run_command'->>'run_id';
begin
 if new.status in ('succeeded','failed','dead_lettered') and old.status is distinct from new.status and v_run~'^[0-9a-f-]{36}$' then
  perform runtime.nexloop_budget_settle_run(new.tenant_id,new.world,v_run);
 end if;
 return null;
end $$;
alter function runtime.nexloop_budget_on_job_terminal() owner to nexloop_owner;
revoke all on function runtime.nexloop_budget_on_job_terminal() from public;
create trigger nx027_budget_settlement after update of status on runtime.jobs for each row execute function runtime.nexloop_budget_on_job_terminal();

-- 4. Cost read port (eios:action:nexloop.cost.read:1): owner query port before NX-028 --------------------------------
create function authz.nexloop_cost_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-cost-read-v1','eios:action:nexloop.cost.read:1',
  array['nexloop_domain_worker','nexloop_api']);c jsonb:=p_payload::jsonb;v jsonb;
begin
 perform set_config('eios.tenant_id',t,true);
 if c->>'verb'='summary' then
  -- Per kind, currency and scale; unpriced units separately. Never one total across currencies.
  select coalesce(jsonb_agg(jsonb_build_object('cost_kind',k.cost_kind,'data_mode',k.data_mode,'currency',k.currency,'amount_unit',k.amount_unit,
    'amount',k.amount::text,'entries',k.n,'units',k.units::text) order by k.cost_kind,k.currency nulls last),'[]'::jsonb) into v
   from (select cost_kind,data_mode,currency,amount_unit,sum(amount) amount,count(*) n,sum(units) units from runtime.nexloop_cost_entries
     where tenant_id=t and world=p_world group by cost_kind,data_mode,currency,amount_unit) k;
  return jsonb_build_object('world',p_world,'costs',v);
 elsif c->>'verb'='entries' then
  if c ? 'run_id' and coalesce(c->>'run_id','')!~'^[0-9a-f-]{36}$' then raise exception 'cost read invalid' using errcode='22023';end if;
  select coalesce(jsonb_agg(jsonb_build_object('entry_id',e.entry_id,'cost_kind',e.cost_kind,'units',e.units::text,'amount',e.amount::text,'currency',e.currency,
    'amount_unit',e.amount_unit,'basis',e.basis,'source_ref',e.source_ref,'run_id',e.run_id,'consumer_ref',e.consumer_ref,
    'occurred_at',runtime.nexloop_commercial_ts(e.occurred_at)) order by e.occurred_at,e.entry_id),'[]'::jsonb) into v
   from runtime.nexloop_cost_entries e where e.tenant_id=t and e.world=p_world and (not (c ? 'run_id') or e.run_id=(c->>'run_id')::uuid)
    and (not (c ? 'cost_kind') or e.cost_kind=c->>'cost_kind');
  return jsonb_build_object('entries',v);
 elsif c->>'verb'='settlements' then
  select coalesce(jsonb_agg(jsonb_build_object('consumption_id',s.consumption_id,'reserved',s.reserved::text,'actual',s.actual::text,'released',s.released::text,
    'unit',s.unit,'outcome',s.outcome) order by s.settled_at,s.consumption_id),'[]'::jsonb) into v
   from runtime.nexloop_budget_settlements s where s.tenant_id=t and s.world=p_world;
  return jsonb_build_object('settlements',v);
 end if;
 raise exception 'cost read invalid' using errcode='22023';
end $$;
alter function authz.nexloop_cost_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_cost_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_cost_read(text,text,text,text,text) to nexloop_domain_worker,nexloop_api;
