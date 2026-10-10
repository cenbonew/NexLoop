-- NX-027 (temporary number 0131): metric observations from verified commercial records (M19, M03),
-- docs/implementation/NX-027-design.md §5.
--
-- 1. A metric source (deployment configuration through nexloop_configurator) binds one approved MetricDefinition
--    version to its source: CommercialRecord kinds and the statuses that count, the value (amount in the
--    definition's currency, or a count) and, for a ratio, the cohort kinds. Declarations are append-only.
-- 2. When the record keeper records a CommercialRecord state, the same transaction projects it deterministically:
--    one observation per (metric, record revision) with occurred_at = the record's business time, superseding the
--    record's previous observation (correction chain: supersedes_observation_id; an uncounted status retracts it).
--    refund_rule is applied here: net_of_refunds = amount minus refunds of the record (backdated to the record's own
--    business time), gross = amount, not_applicable = refund records never count. A record in another currency is
--    an excluded observation (never added, never converted).
-- 3. authz.nexloop_compute_key_result (renamed v0068 body kept) counts only the latest observation of each chain,
--    never retracted or excluded ones, and for a ratio with a cohort freezes its denominator once, at the first
--    computation at or after the window start: the distinct Consumers with a paying record (paid, succeeded or
--    partially refunded) of the cohort kinds before the window start. Members known only later are listed as late, never added (PRD: the denominator is
--    not re-chosen per report).
-- Published migrations are not edited.

-- 1. Observation chain columns (the table stays append-only) -------------------------------------------------------
alter table control.nexloop_metric_observations
 add column supersedes_observation_id text check(supersedes_observation_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
 add column retracted boolean not null default false,
 add column excluded_reason text check(excluded_reason in ('other_currency')),
 add column subject_ref text check(subject_ref ~ '^[a-f0-9]{64}$');
create index nexloop_metric_observations_source on control.nexloop_metric_observations(tenant_id,world,metric_id,metric_version,source_ref);
create index nexloop_metric_observations_supersedes on control.nexloop_metric_observations(tenant_id,world,metric_id,metric_version,supersedes_observation_id)
 where supersedes_observation_id is not null;

-- 2. Metric sources and frozen cohorts -------------------------------------------------------------------------------
create table control.nexloop_metric_sources (
 tenant_id text not null,world text not null,metric_id text not null,metric_version integer not null,
 source_kind text not null check(source_kind in ('commercial_record','cost_entry')),
 record_kinds text[] not null default '{}',statuses text[] not null default '{}',cost_kinds text[] not null default '{}',
 value text not null check(value in ('amount','count')),cohort_kinds text[],
 declared_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,metric_id,metric_version),
 check(source_kind<>'commercial_record' or (cardinality(record_kinds)>=1 and record_kinds<@array['order','payment','renewal','refund']
  and cardinality(statuses)>=1 and statuses<@array['pending','paid','succeeded','failed','cancelled','refunded_partial','refunded'])),
 check(source_kind<>'cost_entry' or (cardinality(cost_kinds)>=1 and cost_kinds<@array['model','channel','discount','service','labour'] and value='amount')),
 check(cohort_kinds is null or (cardinality(cohort_kinds)>=1 and cohort_kinds<@array['order','payment','renewal'])),
 foreign key(tenant_id,world,metric_id,metric_version) references control.nexloop_metric_definitions(tenant_id,world,metric_id,version));
create table control.nexloop_metric_cohort_heads (
 tenant_id text not null,world text not null,goal_id text not null,goal_version integer not null,kr_key text not null,
 frozen_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,goal_id,goal_version,kr_key),
 foreign key(tenant_id,world,goal_id,goal_version,kr_key) references control.nexloop_key_results(tenant_id,world,goal_id,goal_version,kr_key));
create table control.nexloop_metric_cohorts (
 tenant_id text not null,world text not null,goal_id text not null,goal_version integer not null,kr_key text not null,
 member_ref text not null check(member_ref ~ '^[a-f0-9]{64}$'),
 primary key(tenant_id,world,goal_id,goal_version,kr_key,member_ref),
 foreign key(tenant_id,world,goal_id,goal_version,kr_key) references control.nexloop_metric_cohort_heads(tenant_id,world,goal_id,goal_version,kr_key));
do $tables$
declare t text;
begin
 foreach t in array array['control.nexloop_metric_sources','control.nexloop_metric_cohort_heads','control.nexloop_metric_cohorts'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
  execute format('create trigger nx027_append_only before update or delete on %s for each row execute function control.nexloop_nx022_append_only()',t);
 end loop;
end $tables$;

create function control.nexloop_metric_source_declare(p_tenant text,p_world text,p_metric text,p_version integer,p_source_kind text,
  p_record_kinds text[],p_statuses text[],p_cost_kinds text[],p_value text,p_cohort_kinds text[]) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m control.nexloop_metric_definitions;cur control.nexloop_metric_sources;
begin
 if session_user<>'nexloop_configurator' then raise exception 'metric source configuration unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select * into m from control.nexloop_metric_definitions where tenant_id=p_tenant and world=p_world and metric_id=p_metric and version=p_version and status='approved';
 if not found then raise exception 'approved MetricDefinition required' using errcode='42501';end if;
 -- The value must fit the approved definition: an amount is a sum in the definition's currency; a count is a count or,
 -- with a cohort, the numerator of a ratio.
 if (p_value='amount' and (m.aggregation<>'sum' or m.currency is null))
  or (p_value='count' and m.aggregation not in ('count','ratio_of_sums'))
  or ((m.aggregation='ratio_of_sums')<>(p_cohort_kinds is not null))
  or (p_source_kind='cost_entry' and p_cohort_kinds is not null) then
  raise exception 'metric source does not fit its definition' using errcode='22023';end if;
 select * into cur from control.nexloop_metric_sources where tenant_id=p_tenant and world=p_world and metric_id=p_metric and metric_version=p_version;
 if found then
  if cur.source_kind is distinct from p_source_kind or cur.record_kinds is distinct from coalesce(p_record_kinds,'{}') or cur.statuses is distinct from coalesce(p_statuses,'{}')
   or cur.cost_kinds is distinct from coalesce(p_cost_kinds,'{}') or cur.value is distinct from p_value or cur.cohort_kinds is distinct from p_cohort_kinds then
   raise exception 'metric source is declared once per definition version' using errcode='23505';end if;
  return jsonb_build_object('metric_id',p_metric,'metric_version',p_version,'replayed',true);
 end if;
 insert into control.nexloop_metric_sources(tenant_id,world,metric_id,metric_version,source_kind,record_kinds,statuses,cost_kinds,value,cohort_kinds)
  values(p_tenant,p_world,p_metric,p_version,p_source_kind,coalesce(p_record_kinds,'{}'),coalesce(p_statuses,'{}'),coalesce(p_cost_kinds,'{}'),p_value,p_cohort_kinds);
 return jsonb_build_object('metric_id',p_metric,'metric_version',p_version,'replayed',false);
end $$;
alter function control.nexloop_metric_source_declare(text,text,text,integer,text,text[],text[],text[],text,text[]) owner to nexloop_owner;
revoke all on function control.nexloop_metric_source_declare(text,text,text,integer,text,text[],text[],text[],text,text[]) from public;
grant execute on function control.nexloop_metric_source_declare(text,text,text,integer,text,text[],text[],text[],text,text[]) to nexloop_configurator;

-- 3. Projection of one observation per (metric source, source revision) ----------------------------------------------
-- Appends the desired observation of p_source_ref unless the chain's latest observation already says the same.
create function control.nexloop_metric_project(p_tenant text,p_world text,m control.nexloop_metric_definitions,p_source_ref text,p_observation text,
  p_counted boolean,p_value numeric,p_excluded text,p_occurred timestamptz,p_data_mode text,p_subject text,p_evidence jsonb) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare latest control.nexloop_metric_observations;
begin
 select * into latest from control.nexloop_metric_observations o where o.tenant_id=p_tenant and o.world=p_world and o.metric_id=m.metric_id
  and o.metric_version=m.version and o.source_ref=p_source_ref
  and not exists(select 1 from control.nexloop_metric_observations x where x.tenant_id=o.tenant_id and x.world=o.world and x.metric_id=o.metric_id
   and x.metric_version=o.metric_version and x.supersedes_observation_id=o.observation_id)
  order by o.recorded_at desc limit 1;
 if not p_counted then
  if latest.observation_id is null or latest.retracted then return null;end if;
 elsif latest.observation_id is not null and not latest.retracted and latest.numerator=p_value and latest.occurred_at=p_occurred
  and latest.excluded_reason is not distinct from p_excluded and latest.subject_ref is not distinct from p_subject then return null;
 end if;
 if exists(select 1 from control.nexloop_metric_observations where tenant_id=p_tenant and world=p_world and metric_id=m.metric_id and metric_version=m.version
   and observation_id=p_observation) then return null;end if;
 insert into control.nexloop_metric_observations(tenant_id,world,metric_id,metric_version,observation_id,numerator,denominator,occurred_at,matures_at,
   data_mode,source_ref,evidence_refs,recorded_by,supersedes_observation_id,retracted,excluded_reason,subject_ref)
  values(p_tenant,p_world,m.metric_id,m.version,p_observation,case when p_counted then p_value else 0 end,null,p_occurred,
   p_occurred+make_interval(secs=>m.maturity_seconds),p_data_mode,p_source_ref,p_evidence,'nexloop:commercial-projection',
   latest.observation_id,not p_counted,case when p_counted then p_excluded end,p_subject);
 return p_observation;
end $$;
alter function control.nexloop_metric_project(text,text,control.nexloop_metric_definitions,text,text,boolean,numeric,text,timestamptz,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_metric_project(text,text,control.nexloop_metric_definitions,text,text,boolean,numeric,text,timestamptz,text,text,jsonb) from public;

create function runtime.nexloop_commercial_project(p_tenant text,p_world text,p_key text,p_props jsonb) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);s control.nexloop_metric_sources;m control.nexloop_metric_definitions;r runtime.nexloop_commercial_records;
 v_revision integer;v_value numeric;v_counted boolean;v_excluded text;n integer:=0;v_id text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from runtime.nexloop_commercial_records where tenant_id=p_tenant and world=p_world and record_key=p_key;
 select nexloop_revision into v_revision from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='CommercialRecord' and object_id=r.object_id;
 for s in select * from control.nexloop_metric_sources x where x.tenant_id=p_tenant and x.world=p_world and x.source_kind='commercial_record'
   and (p_props->>'kind')=any(x.record_kinds) order by x.metric_id,x.metric_version loop
  select * into m from control.nexloop_metric_definitions where tenant_id=p_tenant and world=p_world and metric_id=s.metric_id and version=s.metric_version;
  v_counted:=(p_props->>'status')=any(s.statuses) and not (m.refund_rule='not_applicable' and p_props->>'kind'='refund');
  v_excluded:=null;
  if s.value='count' then v_value:=1;
  elsif m.currency is distinct from p_props->>'currency' then v_value:=0;v_excluded:='other_currency';
  elsif m.refund_rule='net_of_refunds' and p_props->>'kind'<>'refund' then v_value:=(p_props->>'amount_minor')::numeric-(p_props->>'refunded_minor')::numeric;
  else v_value:=(p_props->>'amount_minor')::numeric;end if;
  v_id:=control.nexloop_metric_project(p_tenant,p_world,m,'commercial:'||r.object_id,'cr.'||p_key||'.r'||v_revision,v_counted,v_value,v_excluded,
   (p_props->>'occurred_at')::timestamptz,p_props->>'data_mode',p_props->>'consumer_ref',
   jsonb_build_array('commercial:'||r.object_id||'@'||v_revision,p_props->>'last_event_ref'));
  if v_id is not null then n:=n+1;end if;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return n;
end $$;
alter function runtime.nexloop_commercial_project(text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_commercial_project(text,text,text,jsonb) from public;

-- The record keeper's downstream hook now projects observations (0130 body was empty).
create or replace function runtime.nexloop_commercial_on_recorded(p_tenant text,p_world text,p_key text,p_props jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin return jsonb_build_object('observations',runtime.nexloop_commercial_project(p_tenant,p_world,p_key,p_props));end $$;

-- 4. Key result: latest of each chain, frozen cohort for ratios ----------------------------------------------------
alter function authz.nexloop_compute_key_result(text,text,text,integer,text,timestamptz) rename to nexloop_compute_key_result_v0068;
revoke all on function authz.nexloop_compute_key_result_v0068(text,text,text,integer,text,timestamptz) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler;

create function authz.nexloop_compute_key_result(p_digest text,p_world text,p_goal text,p_version integer,p_kr text,p_as_of timestamptz)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;kr control.nexloop_key_results%rowtype;m control.nexloop_metric_definitions%rowtype;s control.nexloop_metric_sources;
 v_as_of timestamptz;v_num numeric;v_den numeric;v_count bigint;v_immature bigint;v_excluded bigint;v_value numeric;v_corrections bigint;
 v_retracted bigint;v_other bigint;h control.nexloop_metric_cohort_heads;v_cohort jsonb;v_late bigint;
begin
 ident:=control.nexloop_nx022_identity(p_digest,p_world,false);v_tenant:=ident->'binding'->>'tenant_id';
 v_as_of:=least(coalesce(p_as_of,clock_timestamp()),clock_timestamp());
 select * into kr from control.nexloop_key_results where tenant_id=v_tenant and world=p_world and goal_id=p_goal and goal_version=p_version and kr_key=p_kr;
 if not found then raise exception 'key result unavailable' using errcode='42501';end if;
 select * into m from control.nexloop_metric_definitions where tenant_id=v_tenant and world=p_world and metric_id=kr.metric_id and version=kr.metric_version;
 select * into s from control.nexloop_metric_sources where tenant_id=v_tenant and world=p_world and metric_id=m.metric_id and metric_version=m.version;
 with obs as (
  select o.*,exists(select 1 from control.nexloop_metric_observations x where x.tenant_id=o.tenant_id and x.world=o.world and x.metric_id=o.metric_id
    and x.metric_version=o.metric_version and x.supersedes_observation_id=o.observation_id) superseded,
   (p_world<>'real' or o.data_mode='real') eligible
  from control.nexloop_metric_observations o where o.tenant_id=v_tenant and o.world=p_world and o.metric_id=m.metric_id and o.metric_version=m.version
   and o.occurred_at>=kr.window_start and o.occurred_at<kr.window_end and o.occurred_at<=v_as_of)
 select coalesce(sum(numerator) filter (where not superseded and eligible and not retracted and excluded_reason is null and matures_at<=v_as_of),0),
        coalesce(sum(denominator) filter (where not superseded and eligible and not retracted and excluded_reason is null and matures_at<=v_as_of),0),
        count(*) filter (where not superseded and eligible and not retracted and excluded_reason is null and matures_at<=v_as_of),
        count(*) filter (where not superseded and eligible and not retracted and excluded_reason is null and matures_at>v_as_of),
        count(*) filter (where not superseded and not eligible),
        count(*) filter (where superseded),
        count(*) filter (where not superseded and retracted),
        count(*) filter (where not superseded and excluded_reason='other_currency')
   into v_num,v_den,v_count,v_immature,v_excluded,v_corrections,v_retracted,v_other from obs;
 if m.aggregation='ratio_of_sums' and s.cohort_kinds is not null then
  select * into h from control.nexloop_metric_cohort_heads where tenant_id=v_tenant and world=p_world and goal_id=p_goal and goal_version=p_version and kr_key=p_kr;
  if h.kr_key is null and v_as_of>=kr.window_start then
   -- Freeze once: Consumers with a counted record of the cohort kinds before the window start, known now.
   insert into control.nexloop_metric_cohort_heads(tenant_id,world,goal_id,goal_version,kr_key) values(v_tenant,p_world,p_goal,p_version,p_kr)
    returning * into h;
   insert into control.nexloop_metric_cohorts(tenant_id,world,goal_id,goal_version,kr_key,member_ref)
    select distinct v_tenant,p_world,p_goal,p_version,p_kr,o.properties->>'consumer_ref' from ontology.objects o
     where o.tenant_id=v_tenant and o.world=p_world and o.type_name='CommercialRecord' and o.properties->>'consumer_ref' is not null
      and (o.properties->>'kind')=any(s.cohort_kinds) and (o.properties->>'status') in ('paid','succeeded','refunded_partial')
      and (o.properties->>'occurred_at')::timestamptz<kr.window_start and (p_world<>'real' or o.properties->>'data_mode'='real');
  end if;
  if h.kr_key is not null then
   select count(*) into v_den from control.nexloop_metric_cohorts c where c.tenant_id=v_tenant and c.world=p_world and c.goal_id=p_goal and c.goal_version=p_version and c.kr_key=p_kr;
   select count(distinct o.subject_ref) into v_num from control.nexloop_metric_observations o
    where o.tenant_id=v_tenant and o.world=p_world and o.metric_id=m.metric_id and o.metric_version=m.version
     and o.occurred_at>=kr.window_start and o.occurred_at<kr.window_end and o.occurred_at<=v_as_of and o.matures_at<=v_as_of
     and not o.retracted and o.excluded_reason is null and (p_world<>'real' or o.data_mode='real')
     and not exists(select 1 from control.nexloop_metric_observations x where x.tenant_id=o.tenant_id and x.world=o.world and x.metric_id=o.metric_id
      and x.metric_version=o.metric_version and x.supersedes_observation_id=o.observation_id)
     and exists(select 1 from control.nexloop_metric_cohorts c where c.tenant_id=v_tenant and c.world=p_world and c.goal_id=p_goal and c.goal_version=p_version
      and c.kr_key=p_kr and c.member_ref=o.subject_ref);
   select count(distinct o.properties->>'consumer_ref') into v_late from ontology.objects o
    where o.tenant_id=v_tenant and o.world=p_world and o.type_name='CommercialRecord' and o.properties->>'consumer_ref' is not null
     and (o.properties->>'kind')=any(s.cohort_kinds) and (o.properties->>'status') in ('paid','succeeded','refunded_partial')
     and (o.properties->>'occurred_at')::timestamptz<kr.window_start and (p_world<>'real' or o.properties->>'data_mode'='real')
     and not exists(select 1 from control.nexloop_metric_cohorts c where c.tenant_id=v_tenant and c.world=p_world and c.goal_id=p_goal and c.goal_version=p_version
      and c.kr_key=p_kr and c.member_ref=o.properties->>'consumer_ref');
   v_cohort:=jsonb_build_object('size',v_den,'frozen_at',h.frozen_at,'late_members',v_late,'members_with_outcome',v_num);
  else
   v_num:=null;v_den:=null;v_cohort:=jsonb_build_object('size',null,'frozen_at',null,'late_members',null,'members_with_outcome',null);
  end if;
 end if;
 v_value:=case m.aggregation when 'count' then v_count when 'sum' then v_num when 'ratio_of_sums' then v_num/nullif(v_den,0) end;
 return jsonb_build_object('goal_id',p_goal,'goal_version',p_version,'kr_key',p_kr,'metric_id',m.metric_id,'metric_version',m.version,
  'aggregation',m.aggregation,'unit',m.unit,'currency',m.currency,'refund_rule',m.refund_rule,'as_of',v_as_of,
  'window_start',kr.window_start,'window_end',kr.window_end,'value',v_value::text,'target',kr.target::text,'direction',kr.direction,
  'met',case when v_value is null then null when kr.direction='at_least' then v_value>=kr.target else v_value<=kr.target end,
  'matured_observations',v_count,'immature_observations',v_immature,'excluded_non_real_observations',v_excluded,
  'corrections_applied',v_corrections,'retracted_observations',v_retracted,'excluded_other_currency',v_other,
  'refund_rule_applied',m.refund_rule,'source',case when s.metric_id is null then null else s.source_kind end,'cohort',v_cohort);
end $$;
alter function authz.nexloop_compute_key_result(text,text,text,integer,text,timestamptz) owner to nexloop_owner;
revoke all on function authz.nexloop_compute_key_result(text,text,text,integer,text,timestamptz) from public;
grant execute on function authz.nexloop_compute_key_result(text,text,text,integer,text,timestamptz) to nexloop_api,nexloop_domain_worker,nexloop_scheduler;
