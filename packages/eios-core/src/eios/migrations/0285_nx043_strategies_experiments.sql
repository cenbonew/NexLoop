-- NX-043 X1 (design §3, §4, §10 D1-D6, D9): strategy versions and experiment preregistration. The migration number lives only in the
-- file name and the catalog; nothing below repeats it.
-- 1. control.nexloop_strategy_versions: one row per published strategy version (contract nexloop-strategy-version/1). A strategy is a
--    set of low-risk levers only (D1): follow-up timing, a choice among published operator persona versions, and switches of
--    non-marketing outreach purposes. Price, incentives and marketing consent are no lever: any other lever is refused
--    (lever_not_allowed). A version is frozen (no update, delete or truncate); its status is appended events (published, retired).
-- 2. control.nexloop_experiments: one row per preregistered experiment (contract nexloop-experiment-registration/1, D3): hypothesis,
--    primary metric, guardrails, sample sizes, maturity window, budget and end time, all required and frozen at registration; the
--    defaults of D4 (guardrails) and D6 (maturity window, minimum sample) are written into the frozen registration when the owner
--    leaves a member out, and the owner may only tighten them. Its status is appended events (started, stopped with a reason): a
--    changed experiment is a new experiment.
-- 3. Governed owner Actions strategy.publish / retire, experiment.register / start / stop (D3: owner only, from real, audited,
--    idempotent per request). start checks the registration again: a selected twin must be selectable (NX-042: published, not
--    retired, calibrated - the part of D9 that still holds), every strategy still published, the world active, the end ahead, and the
--    online stage is refused until allocation and the stopping rules exist (X4, X5).
-- 4. An experiment never opens real dispatch (design §4): no field of a registration names a switch, a registration that mentions
--    real dispatch is refused, and no function here writes control.nexloop_real_dispatch_switches.

-- 1. Strategy versions -------------------------------------------------------------------------------------------------------------
create table control.nexloop_strategy_versions (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 strategy_id text not null check(strategy_id~'^[a-z][a-z0-9_.-]{0,63}$'),
 version integer not null check(version>=1),
 definition jsonb not null check(jsonb_typeof(definition)='object'),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),
 request_id text not null check(char_length(request_id) between 1 and 200),
 published_by text not null check(char_length(published_by) between 1 and 200),
 published_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,strategy_id,version),
 unique(tenant_id,request_id));
create table control.nexloop_strategy_events (
 event_id bigint generated always as identity primary key,
 tenant_id text not null,strategy_id text not null,version integer not null,
 status text not null check(status in ('published','retired')),
 reason text check(char_length(reason) between 1 and 500),
 recorded_by text not null check(char_length(recorded_by) between 1 and 200),
 request_id text not null check(char_length(request_id) between 1 and 200),
 recorded_at timestamptz not null default clock_timestamp(),
 unique(tenant_id,request_id),
 check((status='retired')=(reason is not null)),
 foreign key(tenant_id,strategy_id,version) references control.nexloop_strategy_versions(tenant_id,strategy_id,version));
create index nexloop_strategy_events_version on control.nexloop_strategy_events(tenant_id,strategy_id,version,event_id);

-- 2. Experiments -------------------------------------------------------------------------------------------------------------------
create table control.nexloop_experiments (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 experiment_id text not null check(experiment_id~'^[a-z][a-z0-9_.-]{0,63}$'),
 registration jsonb not null check(jsonb_typeof(registration)='object'),
 registration_digest text not null check(registration_digest~'^[0-9a-f]{64}$'),
 request_id text not null check(char_length(request_id) between 1 and 200),
 registered_by text not null check(char_length(registered_by) between 1 and 200),
 registered_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,experiment_id),
 unique(tenant_id,request_id));
create table control.nexloop_experiment_events (
 event_id bigint generated always as identity primary key,
 tenant_id text not null,experiment_id text not null,
 status text not null check(status in ('started','stopped')),
 reason text check(char_length(reason) between 1 and 500),
 recorded_by text not null check(char_length(recorded_by) between 1 and 200),
 request_id text not null check(char_length(request_id) between 1 and 200),
 recorded_at timestamptz not null default clock_timestamp(),
 unique(tenant_id,request_id),
 check((status='stopped')=(reason is not null)),
 foreign key(tenant_id,experiment_id) references control.nexloop_experiments(tenant_id,experiment_id));
create index nexloop_experiment_events_experiment on control.nexloop_experiment_events(tenant_id,experiment_id,event_id);

do $$declare t text;begin
 foreach t in array array['control.nexloop_strategy_versions','control.nexloop_strategy_events','control.nexloop_experiments','control.nexloop_experiment_events'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker,nexloop_twin_port,nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api',t);
 end loop;
end $$;
-- Frozen: a version, a registration and their events are written once (the triggers rely on session_replication_role=replica being a
-- superuser setting).
create function control.nexloop_experiment_frozen() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 raise exception '% is append-only (a strategy version or an experiment registration is frozen; change it by a new one)',tg_table_name using errcode='42501';
end $$;
alter function control.nexloop_experiment_frozen() owner to nexloop_owner;
revoke all on function control.nexloop_experiment_frozen() from public;
do $$declare t text;begin
 foreach t in array array['control.nexloop_strategy_versions','control.nexloop_strategy_events','control.nexloop_experiments','control.nexloop_experiment_events'] loop
  execute format('create trigger nx043_frozen before update or delete on %s for each row execute function control.nexloop_experiment_frozen()',t);
  execute format('create trigger nx043_no_truncate before truncate on %s for each statement execute function control.nexloop_experiment_frozen()',t);
 end loop;
end $$;

-- D4, D6: the defaults written into a registration that leaves them out; a registration may only tighten them.
create function runtime.nexloop_experiment_defaults() returns jsonb language sql immutable set search_path=pg_catalog,pg_temp as $$
 select '{"guardrails":{"refusal_or_unsubscribe_rate_delta":0.02,"reconciliation_anomalies":0,"stop_on_budget_exhausted":true},
  "maturity":{"window_days":14,"min_samples_per_group":30},"max_candidate_consumers":20,"max_model_cny":100}'::jsonb
$$;
alter function runtime.nexloop_experiment_defaults() owner to nexloop_owner;

-- The latest status of a strategy version and of an experiment (registered when it has no event).
create function runtime.nexloop_strategy_status(p_tenant text,p_strategy text,p_version integer) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v text;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select e.status into v from control.nexloop_strategy_events e where e.tenant_id=p_tenant and e.strategy_id=p_strategy and e.version=p_version
  order by e.event_id desc limit 1;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_strategy_status(text,text,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_strategy_status(text,text,integer) from public;

create function runtime.nexloop_experiment_status(p_tenant text,p_experiment text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v text;v_found boolean;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 v_found:=exists(select 1 from control.nexloop_experiments x where x.tenant_id=p_tenant and x.experiment_id=p_experiment);
 select e.status into v from control.nexloop_experiment_events e where e.tenant_id=p_tenant and e.experiment_id=p_experiment order by e.event_id desc limit 1;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return case when not v_found then null else coalesce(v,'registered') end;
end $$;
alter function runtime.nexloop_experiment_status(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_status(text,text) from public;

-- D1: whether a persona version is published now (its latest status event), and whether a purpose is a listed non-marketing one.
create function runtime.nexloop_strategy_persona_published(p_tenant text,p_persona text,p_version integer) returns boolean
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select coalesce((select e.status from control.nexloop_operator_persona_status_events e where e.tenant_id=p_tenant and e.persona_id=p_persona
  and e.version=p_version order by e.event_id desc limit 1)='published',false)
$$;
alter function runtime.nexloop_strategy_persona_published(text,text,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_strategy_persona_published(text,text,integer) from public;
create function runtime.nexloop_strategy_purpose_allowed(p_purpose text) returns boolean language sql stable set search_path=pg_catalog,pg_temp as $$
 select coalesce((select bool_or(p->>'purpose_id'=p_purpose and p->>'kind'<>'marketing' and p->>'frequency_class'<>'marketing'
  and not coalesce((p->>'requires_consent')::boolean,false))
  from control.nexloop_outreach_purpose_lists l,jsonb_array_elements(l.definition->'purposes') p
  where l.version=(select max(version) from control.nexloop_outreach_purpose_lists)),false)
$$;
alter function runtime.nexloop_strategy_purpose_allowed(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_strategy_purpose_allowed(text) from public;

-- The strategy definition's code: null when valid (the persona and purposes are checked against the tenant set in eios.tenant_id).
create function runtime.nexloop_strategy_problem(p_tenant text,d jsonb) returns text language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare l jsonb;
begin
 if jsonb_typeof(d) is distinct from 'object' or exists(select 1 from jsonb_object_keys(d) k where k not in ('schema','strategy_id','description','levers'))
  or d->>'schema' is distinct from 'nexloop-strategy-version/1' or coalesce(d->>'strategy_id','')!~'^[a-z][a-z0-9_.-]{0,63}$'
  or (d ? 'description' and (jsonb_typeof(d->'description')<>'string' or char_length(d->>'description') not between 1 and 500))
  or jsonb_typeof(d->'levers') is distinct from 'object' then return 'invalid';end if;
 if d ? 'description' and runtime.nexloop_twin_text_looks_real(d->>'description') then return 'looks_real';end if;
 l:=d->'levers';
 -- D1: only the three low-risk levers; price, incentives, marketing consent and anything else are not levers.
 if exists(select 1 from jsonb_object_keys(l) k where k not in ('follow_up_delay_hours','persona','outreach_purposes')) then return 'lever_not_allowed';end if;
 if (select count(*) from jsonb_object_keys(l))=0 then return 'invalid';end if;
 if l ? 'follow_up_delay_hours' and (jsonb_typeof(l->'follow_up_delay_hours')<>'number' or (l->>'follow_up_delay_hours')!~'^[0-9]{1,3}$'
   or (l->>'follow_up_delay_hours')::integer not between 1 and 336) then return 'invalid';end if;
 if l ? 'persona' then
  if jsonb_typeof(l->'persona')<>'object' or (select array_agg(k order by k) from jsonb_object_keys(l->'persona') k) is distinct from array['persona_id','version']
   or coalesce(l->'persona'->>'persona_id','')!~'^[a-z][a-z0-9_-]{0,63}$' or jsonb_typeof(l->'persona'->'version')<>'number'
   or (l->'persona'->>'version')!~'^[0-9]{1,6}$' then return 'invalid';end if;
  if not runtime.nexloop_strategy_persona_published(p_tenant,l->'persona'->>'persona_id',(l->'persona'->>'version')::integer) then
   return 'persona_not_published';end if;
 end if;
 if l ? 'outreach_purposes' then
  if jsonb_typeof(l->'outreach_purposes')<>'object' or (select count(*) from jsonb_object_keys(l->'outreach_purposes')) not between 1 and 16
   or exists(select 1 from jsonb_each(l->'outreach_purposes') p where p.key!~'^[a-z][a-z0-9_.]{0,63}$' or jsonb_typeof(p.value)<>'boolean') then
   return 'invalid';end if;
  -- D1: a marketing purpose (or one that needs consent) is a marketing lever.
  if exists(select 1 from jsonb_object_keys(l->'outreach_purposes') k where not runtime.nexloop_strategy_purpose_allowed(k)) then return 'purpose_not_allowed';end if;
 end if;
 return null;
end $$;
alter function runtime.nexloop_strategy_problem(text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_strategy_problem(text,jsonb) from public;

-- The registration's code (null when complete and valid) and its frozen form with the D4/D6 defaults written in.
create function runtime.nexloop_experiment_registration_problem(p_tenant text,r jsonb) returns text language plpgsql stable set search_path=pg_catalog,pg_temp as $$
declare v_def jsonb:=runtime.nexloop_experiment_defaults();v_ref jsonb;v_kind text;
begin
 if jsonb_typeof(r) is distinct from 'object' then return 'invalid';end if;
 -- Design §4: an experiment never opens real dispatch; a registration that names it is refused before anything else.
 if r::text ~* 'real.?dispatch' then return 'real_dispatch_not_allowed';end if;
 if exists(select 1 from jsonb_object_keys(r) k where k not in ('schema','experiment_id','stage','world','hypothesis','candidate','control','twin',
   'primary_metric','guardrails','sample_size','maturity','budget','ends_at','allocation_seed')) then return 'invalid';end if;
 -- D3: every preregistered field is required (control and twin explicitly, null when there is none).
 if exists(select 1 from unnest(array['schema','experiment_id','stage','world','hypothesis','candidate','control','twin','primary_metric','guardrails',
   'sample_size','maturity','budget','ends_at','allocation_seed']) k where not r ? k) then return 'registration_incomplete';end if;
 if r->>'schema' is distinct from 'nexloop-experiment-registration/1' or coalesce(r->>'experiment_id','')!~'^[a-z][a-z0-9_.-]{0,63}$'
  or coalesce(r->>'stage','') not in ('screening','shadow','online')
  or jsonb_typeof(r->'world')<>'string' or runtime.nexloop_world_kind(r->>'world') is null
  or jsonb_typeof(r->'hypothesis')<>'string' or char_length(btrim(r->>'hypothesis')) not between 1 and 500
  or jsonb_typeof(r->'allocation_seed')<>'number' or (r->>'allocation_seed')!~'^[0-9]{1,10}$' or (r->>'allocation_seed')::bigint>2147483647
  or jsonb_typeof(r->'ends_at')<>'string' or (r->>'ends_at')!~'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|[+-][0-9]{2}:[0-9]{2})$' then return 'invalid';end if;
 if runtime.nexloop_twin_text_looks_real(r->>'hypothesis') then return 'looks_real';end if;
 foreach v_ref in array array[r->'candidate',r->'control'] loop
  if v_ref is distinct from 'null'::jsonb and (jsonb_typeof(v_ref)<>'object' or (select array_agg(k order by k) from jsonb_object_keys(v_ref) k) is distinct from array['strategy_id','version']
   or coalesce(v_ref->>'strategy_id','')!~'^[a-z][a-z0-9_.-]{0,63}$' or jsonb_typeof(v_ref->'version')<>'number' or (v_ref->>'version')!~'^[0-9]{1,9}$') then return 'invalid';end if;
 end loop;
 if r->'candidate'='null'::jsonb then return 'registration_incomplete';end if;
 if r->'twin'<>'null'::jsonb and (jsonb_typeof(r->'twin')<>'object' or (select array_agg(k order by k) from jsonb_object_keys(r->'twin') k) is distinct from array['twin_id','version']
  or coalesce(r->'twin'->>'twin_id','')!~'^[a-z][a-z0-9_.-]{0,63}$' or jsonb_typeof(r->'twin'->'version')<>'number' or (r->'twin'->>'version')!~'^[0-9]{1,9}$') then return 'invalid';end if;
 if jsonb_typeof(r->'primary_metric')<>'object' or (select array_agg(k order by k) from jsonb_object_keys(r->'primary_metric') k) is distinct from array['direction','key']
  or coalesce(r->'primary_metric'->>'key','')!~'^[a-z][a-z0-9_.]{0,63}$' or coalesce(r->'primary_metric'->>'direction','') not in ('increase','decrease') then return 'invalid';end if;
 -- D4: the guardrails; a member left out takes the default, a given one may only be tighter.
 if jsonb_typeof(r->'guardrails')<>'object' or exists(select 1 from jsonb_object_keys(r->'guardrails') k where k not in
   ('refusal_or_unsubscribe_rate_delta','reconciliation_anomalies','stop_on_budget_exhausted')) then return 'invalid';end if;
 if (r->'guardrails' ? 'refusal_or_unsubscribe_rate_delta' and (jsonb_typeof(r->'guardrails'->'refusal_or_unsubscribe_rate_delta')<>'number'
    or (r->'guardrails'->>'refusal_or_unsubscribe_rate_delta')::numeric<=0
    or (r->'guardrails'->>'refusal_or_unsubscribe_rate_delta')::numeric>(v_def->'guardrails'->>'refusal_or_unsubscribe_rate_delta')::numeric))
  or (r->'guardrails' ? 'reconciliation_anomalies' and r->'guardrails'->'reconciliation_anomalies' is distinct from '0'::jsonb)
  or (r->'guardrails' ? 'stop_on_budget_exhausted' and r->'guardrails'->'stop_on_budget_exhausted' is distinct from 'true'::jsonb) then return 'guardrail_looser_than_default';end if;
 -- D6: the maturity window and the minimum sample per group; defaults when left out, never shorter or smaller.
 if jsonb_typeof(r->'maturity')<>'object' or exists(select 1 from jsonb_object_keys(r->'maturity') k where k not in ('window_days','min_samples_per_group'))
  or exists(select 1 from jsonb_each(r->'maturity') m where jsonb_typeof(m.value)<>'number' or (m.value#>>'{}')!~'^[0-9]{1,5}$') then return 'invalid';end if;
 if (r->'maturity' ? 'window_days' and ((r->'maturity'->>'window_days')::integer<(v_def->'maturity'->>'window_days')::integer or (r->'maturity'->>'window_days')::integer>90))
  or (r->'maturity' ? 'min_samples_per_group' and (r->'maturity'->>'min_samples_per_group')::integer<(v_def->'maturity'->>'min_samples_per_group')::integer) then
  return 'maturity_below_default';end if;
 -- D2: candidate group at most 20 consumers, the control group at least as large (at least half); D6: each at least the minimum sample.
 if jsonb_typeof(r->'sample_size')<>'object' or (select array_agg(k order by k) from jsonb_object_keys(r->'sample_size') k) is distinct from array['candidate','control']
  or exists(select 1 from jsonb_each(r->'sample_size') s where jsonb_typeof(s.value)<>'number' or (s.value#>>'{}')!~'^[0-9]{1,5}$') then return 'invalid';end if;
 if (r->'sample_size'->>'candidate')::integer not between 1 and (v_def->>'max_candidate_consumers')::integer then return 'candidate_group_too_large';end if;
 if (r->'sample_size'->>'control')::integer<(r->'sample_size'->>'candidate')::integer then return 'control_group_too_small';end if;
 -- D5: a model ceiling per experiment; D1: no incentive experiments in the first batch.
 if jsonb_typeof(r->'budget')<>'object' or (select array_agg(k order by k) from jsonb_object_keys(r->'budget') k) is distinct from array['incentive_cny','model_cny']
  or jsonb_typeof(r->'budget'->'model_cny')<>'number' or jsonb_typeof(r->'budget'->'incentive_cny')<>'number'
  or (r->'budget'->>'model_cny')::numeric<0 or (r->'budget'->>'model_cny')::numeric>(v_def->>'max_model_cny')::numeric then return 'invalid';end if;
 if (r->'budget'->>'incentive_cny')::numeric<>0 then return 'incentive_not_allowed';end if;
 begin
  if (r->>'ends_at')::timestamptz<=clock_timestamp() or (r->>'ends_at')::timestamptz>clock_timestamp()+interval '180 days' then return 'ends_at_out_of_range';end if;
 exception when others then return 'invalid';
 end;
 -- The stage decides the world: screening in a simulation world with a twin, shadow in a shadow world, online in real.
 v_kind:=runtime.nexloop_world_kind(r->>'world');
 if (r->>'stage'='screening' and v_kind<>'simulation') or (r->>'stage'='shadow' and v_kind<>'shadow') or (r->>'stage'='online' and r->>'world'<>'real') then
  return 'world_mismatch';end if;
 if r->>'stage'='screening' and r->'twin'='null'::jsonb then return 'twin_required';end if;
 if r->'twin'<>'null'::jsonb and not exists(select 1 from control.nexloop_consumer_twins t where t.tenant_id=p_tenant and t.twin_id=r->'twin'->>'twin_id'
   and t.version=(r->'twin'->>'version')::integer) then return 'twin_unknown';end if;
 foreach v_ref in array array[r->'candidate',r->'control'] loop
  if v_ref<>'null'::jsonb and runtime.nexloop_strategy_status(p_tenant,v_ref->>'strategy_id',(v_ref->>'version')::integer) is distinct from 'published' then
   return 'strategy_not_published';end if;
 end loop;
 if r->'candidate'=r->'control' then return 'invalid';end if;
 return null;
end $$;
alter function runtime.nexloop_experiment_registration_problem(text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_registration_problem(text,jsonb) from public;

create function runtime.nexloop_experiment_registration_frozen(r jsonb) returns jsonb language sql stable set search_path=pg_catalog,pg_temp as $$
 select r||jsonb_build_object('hypothesis',btrim(r->>'hypothesis'),
  'guardrails',(runtime.nexloop_experiment_defaults()->'guardrails')||(r->'guardrails'),
  'maturity',(runtime.nexloop_experiment_defaults()->'maturity')||(r->'maturity'),
  'ends_at',to_char((r->>'ends_at')::timestamptz at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'))
$$;
alter function runtime.nexloop_experiment_registration_frozen(jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_experiment_registration_frozen(jsonb) from public;

-- 3. The governed owner Actions ------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_publish_strategy(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare d jsonb:=body->'definition';s control.nexloop_strategy_versions;v_code text;v_version integer;v_digest text;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','definition'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200 then
  raise exception 'strategy: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'strategy: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 v_code:=runtime.nexloop_strategy_problem(p_tenant,d);
 if v_code is not null then raise exception 'strategy: %',v_code using errcode='22023';end if;
 v_digest:=encode(sha256(convert_to(d::text,'UTF8')),'hex');
 perform pg_advisory_xact_lock(hashtextextended('nexloop:strategy-request:'||p_tenant||':'||(body->>'request_id'),0));
 select * into s from control.nexloop_strategy_versions where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if s.definition_digest<>v_digest then raise exception 'strategy: request_reused' using errcode='22023';end if;
  return jsonb_build_object('strategy_id',s.strategy_id,'version',s.version,'definition_digest',s.definition_digest,
   'status',runtime.nexloop_strategy_status(p_tenant,s.strategy_id,s.version),'published',false);
 end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop:strategy:'||p_tenant||':'||(d->>'strategy_id'),0));
 select coalesce(max(version),0)+1 into v_version from control.nexloop_strategy_versions where tenant_id=p_tenant and strategy_id=d->>'strategy_id';
 insert into control.nexloop_strategy_versions(tenant_id,strategy_id,version,definition,definition_digest,request_id,published_by)
  values(p_tenant,d->>'strategy_id',v_version,d,v_digest,body->>'request_id',p_principal) returning * into s;
 insert into control.nexloop_strategy_events(tenant_id,strategy_id,version,status,recorded_by,request_id)
  values(p_tenant,s.strategy_id,s.version,'published',p_principal,body->>'request_id');
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('strategy:publish:'||s.strategy_id||':'||s.version||':'||md5(p_tenant),'nexloop.strategy.published',p_tenant,p_principal,'strategy',p_intent,
   jsonb_build_object('strategy_id',s.strategy_id,'version',s.version,'definition_digest',s.definition_digest),clock_timestamp());
 return jsonb_build_object('strategy_id',s.strategy_id,'version',s.version,'definition_digest',s.definition_digest,'status','published','published',true);
end $$;
alter function control.nexloop_governed_publish_strategy(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_publish_strategy(text,text,text,text,text,jsonb) from public;

create function control.nexloop_governed_retire_strategy(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare e control.nexloop_strategy_events;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','strategy_id','version','reason'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(body->>'strategy_id','')!~'^[a-z][a-z0-9_.-]{0,63}$' or jsonb_typeof(body->'version') is distinct from 'number'
  or (body->>'version')!~'^[0-9]{1,9}$' or (body->>'version')::integer<1
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(body->>'reason')) not between 1 and 500 then
  raise exception 'strategy: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'strategy: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform pg_advisory_xact_lock(hashtextextended('nexloop:strategy-request:'||p_tenant||':'||(body->>'request_id'),0));
 select * into e from control.nexloop_strategy_events where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if (e.strategy_id,e.version,e.status) is distinct from (body->>'strategy_id',(body->>'version')::integer,'retired') then
   raise exception 'strategy: request_reused' using errcode='22023';end if;
  return jsonb_build_object('strategy_id',e.strategy_id,'version',e.version,'status','retired','retired',false);
 end if;
 perform 1 from control.nexloop_strategy_versions where tenant_id=p_tenant and strategy_id=body->>'strategy_id' and version=(body->>'version')::integer for share;
 if not found then raise exception 'strategy: not_found' using errcode='22023';end if;
 if runtime.nexloop_strategy_status(p_tenant,body->>'strategy_id',(body->>'version')::integer)='retired' then
  raise exception 'strategy: already_retired' using errcode='22023';end if;
 insert into control.nexloop_strategy_events(tenant_id,strategy_id,version,status,reason,recorded_by,request_id)
  values(p_tenant,body->>'strategy_id',(body->>'version')::integer,'retired',btrim(body->>'reason'),p_principal,body->>'request_id');
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('strategy:retire:'||(body->>'strategy_id')||':'||(body->>'version')||':'||md5(p_tenant),'nexloop.strategy.retired',p_tenant,p_principal,'strategy',p_intent,
   jsonb_build_object('strategy_id',body->>'strategy_id','version',(body->>'version')::integer),clock_timestamp());
 return jsonb_build_object('strategy_id',body->>'strategy_id','version',(body->>'version')::integer,'status','retired','retired',true);
end $$;
alter function control.nexloop_governed_retire_strategy(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_retire_strategy(text,text,text,text,text,jsonb) from public;

create function control.nexloop_governed_register_experiment(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r jsonb:=body->'registration';x control.nexloop_experiments;v_code text;v_frozen jsonb;v_digest text;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','registration'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200 then
  raise exception 'experiment: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'experiment: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment-request:'||p_tenant||':'||(body->>'request_id'),0));
 select * into x from control.nexloop_experiments where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  -- A retry replays the frozen registration; the same request with another registration is refused.
  begin v_frozen:=runtime.nexloop_experiment_registration_frozen(r);exception when others then v_frozen:=null;end;
  if v_frozen is distinct from x.registration then raise exception 'experiment: request_reused' using errcode='22023';end if;
  return jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest,'registration',x.registration,
   'status',runtime.nexloop_experiment_status(p_tenant,x.experiment_id),'registered',false);
 end if;
 v_code:=runtime.nexloop_experiment_registration_problem(p_tenant,r);
 if v_code is not null then raise exception 'experiment: %',v_code using errcode='22023';end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment:'||p_tenant||':'||(r->>'experiment_id'),0));
 -- D3: registered once; a changed experiment is a new experiment.
 if exists(select 1 from control.nexloop_experiments where tenant_id=p_tenant and experiment_id=r->>'experiment_id') then
  raise exception 'experiment: experiment_exists' using errcode='22023';end if;
 v_frozen:=runtime.nexloop_experiment_registration_frozen(r);
 v_digest:=encode(sha256(convert_to(v_frozen::text,'UTF8')),'hex');
 insert into control.nexloop_experiments(tenant_id,experiment_id,registration,registration_digest,request_id,registered_by)
  values(p_tenant,r->>'experiment_id',v_frozen,v_digest,body->>'request_id',p_principal) returning * into x;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('experiment:register:'||x.experiment_id||':'||md5(p_tenant),'nexloop.experiment.registered',p_tenant,p_principal,'experiment',p_intent,
   jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest,'stage',v_frozen->>'stage'),clock_timestamp());
 return jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest,'registration',x.registration,
  'status','registered','registered',true);
end $$;
alter function control.nexloop_governed_register_experiment(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_register_experiment(text,text,text,text,text,jsonb) from public;

-- start and stop share the body shape: request, experiment, and for stop a reason.
create function control.nexloop_governed_start_experiment(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare x control.nexloop_experiments;e control.nexloop_experiment_events;r jsonb;v_status text;v_ref jsonb;v_persona jsonb;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','experiment_id'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(body->>'experiment_id','')!~'^[a-z][a-z0-9_.-]{0,63}$' then
  raise exception 'experiment: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'experiment: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment:'||p_tenant||':'||(body->>'experiment_id'),0));
 select * into e from control.nexloop_experiment_events where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if (e.experiment_id,e.status) is distinct from (body->>'experiment_id','started') then raise exception 'experiment: request_reused' using errcode='22023';end if;
  return jsonb_build_object('experiment_id',e.experiment_id,'status',runtime.nexloop_experiment_status(p_tenant,e.experiment_id),'started',false);
 end if;
 select * into x from control.nexloop_experiments where tenant_id=p_tenant and experiment_id=body->>'experiment_id';
 if not found then raise exception 'experiment: not_found' using errcode='22023';end if;
 v_status:=runtime.nexloop_experiment_status(p_tenant,x.experiment_id);
 if v_status<>'registered' then raise exception 'experiment: not_registered_state' using errcode='22023';end if;
 r:=x.registration;
 if (r->>'ends_at')::timestamptz<=clock_timestamp() then raise exception 'experiment: ended' using errcode='22023';end if;
 -- X1 has no allocation and no stopping rules yet: an online experiment cannot start until X4 and X5.
 if r->>'stage'='online' then raise exception 'experiment: online_not_available' using errcode='22023';end if;
 -- D9 (the part that still holds): a selected twin must be published, not retired and calibrated now.
 if r->'twin'<>'null'::jsonb and not runtime.nexloop_consumer_twin_selectable(p_tenant,r->'twin'->>'twin_id',(r->'twin'->>'version')::integer) then
  raise exception 'experiment: twin_not_calibrated' using errcode='22023';end if;
 foreach v_ref in array array[r->'candidate',r->'control'] loop
  if v_ref<>'null'::jsonb then
   if runtime.nexloop_strategy_status(p_tenant,v_ref->>'strategy_id',(v_ref->>'version')::integer) is distinct from 'published' then
    raise exception 'experiment: strategy_not_published' using errcode='22023';end if;
   v_persona:=(select s.definition->'levers'->'persona' from control.nexloop_strategy_versions s where s.tenant_id=p_tenant
    and s.strategy_id=v_ref->>'strategy_id' and s.version=(v_ref->>'version')::integer);
   if v_persona is not null and not runtime.nexloop_strategy_persona_published(p_tenant,v_persona->>'persona_id',(v_persona->>'version')::integer) then
    raise exception 'experiment: persona_not_published' using errcode='22023';end if;
  end if;
 end loop;
 if r->>'world'<>'real' and not exists(select 1 from control.nexloop_sim_worlds w where w.world=r->>'world' and w.tenant_id=p_tenant
   and w.status='active' and w.expires_at>clock_timestamp()) then
  raise exception 'experiment: world_not_active' using errcode='22023';end if;
 insert into control.nexloop_experiment_events(tenant_id,experiment_id,status,recorded_by,request_id)
  values(p_tenant,x.experiment_id,'started',p_principal,body->>'request_id');
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('experiment:start:'||x.experiment_id||':'||md5(p_tenant),'nexloop.experiment.started',p_tenant,p_principal,'experiment',p_intent,
   jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest),clock_timestamp());
 return jsonb_build_object('experiment_id',x.experiment_id,'registration_digest',x.registration_digest,'status','started','started',true);
end $$;
alter function control.nexloop_governed_start_experiment(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_start_experiment(text,text,text,text,text,jsonb) from public;

create function control.nexloop_governed_stop_experiment(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare e control.nexloop_experiment_events;v_status text;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','experiment_id','reason'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(body->>'experiment_id','')!~'^[a-z][a-z0-9_.-]{0,63}$'
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(body->>'reason')) not between 1 and 500 then
  raise exception 'experiment: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'experiment: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform pg_advisory_xact_lock(hashtextextended('nexloop:experiment:'||p_tenant||':'||(body->>'experiment_id'),0));
 select * into e from control.nexloop_experiment_events where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if (e.experiment_id,e.status) is distinct from (body->>'experiment_id','stopped') then raise exception 'experiment: request_reused' using errcode='22023';end if;
  return jsonb_build_object('experiment_id',e.experiment_id,'status','stopped','stopped',false);
 end if;
 v_status:=runtime.nexloop_experiment_status(p_tenant,body->>'experiment_id');
 if v_status is null then raise exception 'experiment: not_found' using errcode='22023';end if;
 if v_status='stopped' then raise exception 'experiment: already_stopped' using errcode='22023';end if;
 -- Stopping returns everyone to the control strategy (X4/X5 act on it); no publication, no data change, nothing is resent or withdrawn.
 insert into control.nexloop_experiment_events(tenant_id,experiment_id,status,reason,recorded_by,request_id)
  values(p_tenant,body->>'experiment_id','stopped',btrim(body->>'reason'),p_principal,body->>'request_id');
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('experiment:stop:'||(body->>'experiment_id')||':'||md5(p_tenant),'nexloop.experiment.stopped',p_tenant,p_principal,'experiment',p_intent,
   jsonb_build_object('experiment_id',body->>'experiment_id','from',v_status),clock_timestamp());
 return jsonb_build_object('experiment_id',body->>'experiment_id','status','stopped','stopped',true);
end $$;
alter function control.nexloop_governed_stop_experiment(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_stop_experiment(text,text,text,text,text,jsonb) from public;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('strategy.publish','publish_strategy','human','control.nexloop_governed_publish_strategy(text,text,text,text,text,jsonb)','NX-043'),
 ('strategy.retire','retire_strategy','human','control.nexloop_governed_retire_strategy(text,text,text,text,text,jsonb)','NX-043'),
 ('experiment.register','register_experiment','human','control.nexloop_governed_register_experiment(text,text,text,text,text,jsonb)','NX-043'),
 ('experiment.start','start_experiment','human','control.nexloop_governed_start_experiment(text,text,text,text,text,jsonb)','NX-043'),
 ('experiment.stop','stop_experiment','human','control.nexloop_governed_stop_experiment(text,text,text,text,text,jsonb)','NX-043');
