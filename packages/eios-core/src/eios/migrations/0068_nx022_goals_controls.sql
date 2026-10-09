-- NX-022 Goal/KR versions, approved MetricDefinitions, owner control revisions,
-- dispatch-time control checks and tenant budget ledger. Temporary number; published
-- 0001..0065 remain unchanged. Every table is tenant/world scoped, FORCE RLS, and
-- writable only by the SECURITY DEFINER functions below.

create table control.nexloop_control_heads (
 tenant_id text not null,world text not null,revision bigint not null check(revision>=0),
 primary key(tenant_id,world));

create table control.nexloop_control_events (
 tenant_id text not null,world text not null,revision bigint not null check(revision>=1),
 event_kind text not null check(event_kind in ('pause','resume','budget','goal_version','agent_goal','metric')),
 scope_kind text not null check(scope_kind in ('tenant','role','consumer','strategy','action_type','goal','budget','metric')),
 scope_ref text not null check(scope_ref ~ '^[A-Za-z0-9*][A-Za-z0-9._:/@*-]{0,254}$'),
 detail jsonb not null check(jsonb_typeof(detail)='object'),principal_id text not null,intent_id text not null,
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,revision),
 check((scope_kind='tenant')=(scope_ref='*')));
create index nexloop_control_events_scope on control.nexloop_control_events(tenant_id,world,scope_kind,scope_ref,revision);

create table control.nexloop_control_scopes (
 tenant_id text not null,world text not null,
 scope_kind text not null check(scope_kind in ('tenant','role','consumer','strategy','action_type')),
 scope_ref text not null check(scope_ref ~ '^[A-Za-z0-9*][A-Za-z0-9._:/@*-]{0,254}$'),
 paused boolean not null,revision bigint not null check(revision>=1),reason text not null check(length(reason) between 1 and 2000),
 updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,scope_kind,scope_ref),
 check((scope_kind='tenant')=(scope_ref='*')));

create table control.nexloop_metric_definitions (
 tenant_id text not null,world text not null,
 metric_id text not null check(metric_id ~ '^[a-z0-9][a-z0-9._-]{0,127}$'),version integer not null check(version>=1),
 name text not null check(length(name) between 1 and 200),
 aggregation text not null check(aggregation in ('count','sum','ratio_of_sums')),
 unit text not null check(unit ~ '^[A-Za-z0-9._%/-]{1,32}$'),currency text check(currency ~ '^[A-Z]{3}$'),
 maturity_seconds integer not null check(maturity_seconds between 0 and 31622400),
 refund_rule text not null check(refund_rule in ('net_of_refunds','gross','not_applicable')),
 cohort_rule text not null check(length(cohort_rule) between 1 and 2000),
 status text not null check(status='approved'),approved_by text not null,intent_id text not null,
 approved_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,metric_id,version));

create table control.nexloop_goals (
 tenant_id text not null,world text not null,
 goal_id text not null check(goal_id ~ '^[a-z0-9][a-z0-9._-]{0,127}$'),
 goal_kind text not null check(goal_kind in ('long_term','stage','agent')),
 owner_principal_id text not null,parent_goal_id text,current_version integer not null check(current_version>=1),
 created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,goal_id),
 check((goal_kind='long_term' and parent_goal_id is null) or goal_kind='stage' or (goal_kind='agent' and parent_goal_id is not null)),
 foreign key(tenant_id,world,parent_goal_id) references control.nexloop_goals(tenant_id,world,goal_id));

create table control.nexloop_goal_versions (
 tenant_id text not null,world text not null,goal_id text not null,version integer not null check(version>=1),
 objective text not null check(length(objective) between 1 and 4000),
 period_start timestamptz not null,period_end timestamptz not null,
 priority integer not null check(priority between 1 and 5),
 budget jsonb not null check(jsonb_typeof(budget)='object'),
 constraints jsonb not null check(jsonb_typeof(constraints)='array'),
 parent_goal_id text,parent_version integer,role_ref text check(role_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$'),
 status text not null check(status in ('published','superseded')),
 publisher_principal_id text not null,publisher_kind text not null check(publisher_kind in ('human','agent')),
 change_summary text not null check(length(change_summary) between 1 and 2000),
 impact jsonb not null check(jsonb_typeof(impact)='object'),control_revision bigint not null check(control_revision>=1),
 intent_id text not null,published_at timestamptz not null default clock_timestamp(),superseded_at timestamptz,
 primary key(tenant_id,world,goal_id,version),
 check(period_start<period_end),
 check((status='superseded')=(superseded_at is not null)),
 check((parent_goal_id is null)=(parent_version is null)),
 foreign key(tenant_id,world,goal_id) references control.nexloop_goals(tenant_id,world,goal_id),
 foreign key(tenant_id,world,parent_goal_id,parent_version) references control.nexloop_goal_versions(tenant_id,world,goal_id,version));
create unique index nexloop_goal_one_published on control.nexloop_goal_versions(tenant_id,world,goal_id) where status='published';
create index nexloop_goal_versions_parent on control.nexloop_goal_versions(tenant_id,world,parent_goal_id,parent_version);
alter table control.nexloop_goals add foreign key(tenant_id,world,goal_id,current_version)
 references control.nexloop_goal_versions(tenant_id,world,goal_id,version) deferrable initially deferred;

create table control.nexloop_key_results (
 tenant_id text not null,world text not null,goal_id text not null,goal_version integer not null,
 kr_key text not null check(kr_key ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
 metric_id text not null,metric_version integer not null,
 target numeric not null check(target not in ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)),
 direction text not null check(direction in ('at_least','at_most')),
 window_start timestamptz not null,window_end timestamptz not null,
 primary key(tenant_id,world,goal_id,goal_version,kr_key),
 check(window_start<window_end),
 foreign key(tenant_id,world,goal_id,goal_version) references control.nexloop_goal_versions(tenant_id,world,goal_id,version),
 foreign key(tenant_id,world,metric_id,metric_version) references control.nexloop_metric_definitions(tenant_id,world,metric_id,version));

create table control.nexloop_metric_observations (
 tenant_id text not null,world text not null,metric_id text not null,metric_version integer not null,
 observation_id text not null check(observation_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
 numerator numeric not null check(numerator not in ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)),
 denominator numeric check(denominator not in ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)),
 occurred_at timestamptz not null,matures_at timestamptz not null,
 data_mode text not null check(data_mode in ('real','test','simulation')),
 source_ref text not null check(length(source_ref) between 1 and 512),
 evidence_refs jsonb not null check(jsonb_typeof(evidence_refs)='array'),
 recorded_by text not null,recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,metric_id,metric_version,observation_id),
 -- A real world never stores simulation output; non-real worlds never claim real data.
 check((world='real' and data_mode in ('real','test')) or (world<>'real' and data_mode<>'real')),
 check(matures_at>=occurred_at),
 foreign key(tenant_id,world,metric_id,metric_version) references control.nexloop_metric_definitions(tenant_id,world,metric_id,version));

create table control.nexloop_budget_limits (
 tenant_id text not null,world text not null,budget_kind text not null check(budget_kind in ('model','incentive')),
 unit text not null check(unit ~ '^[A-Z]{3}$|^[a-z][a-z0-9_]{0,31}$'),
 limit_amount numeric not null check(limit_amount>=0 and limit_amount<>'Infinity'::numeric),
 period_start timestamptz not null,period_end timestamptz not null,revision bigint not null check(revision>=1),
 updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,budget_kind),check(period_start<period_end),
 check(budget_kind<>'incentive' or (unit ~ '^[A-Z]{3}$' and limit_amount=trunc(limit_amount))));

create table control.nexloop_budget_consumption (
 tenant_id text not null,world text not null,budget_kind text not null,
 consumption_id text not null check(consumption_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
 amount numeric not null check(amount>0 and amount<>'Infinity'::numeric),unit text not null,
 source_ref text not null check(length(source_ref) between 1 and 512),principal_id text not null,
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,budget_kind,consumption_id),
 foreign key(tenant_id,world,budget_kind) references control.nexloop_budget_limits(tenant_id,world,budget_kind));
create index nexloop_budget_consumption_time on control.nexloop_budget_consumption(tenant_id,world,budget_kind,recorded_at);

create table control.nexloop_run_goal_bindings (
 tenant_id text not null,world text not null,run_id uuid not null,goal_id text not null,goal_version integer not null,
 control_revision bigint not null check(control_revision>=0),principal_id text not null,
 bound_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,run_id,goal_id),
 foreign key(tenant_id,world,goal_id,goal_version) references control.nexloop_goal_versions(tenant_id,world,goal_id,version));

do $$
declare t text;
begin
 foreach t in array array['nexloop_control_heads','nexloop_control_events','nexloop_control_scopes','nexloop_metric_definitions',
  'nexloop_goals','nexloop_goal_versions','nexloop_key_results','nexloop_metric_observations','nexloop_budget_limits',
  'nexloop_budget_consumption','nexloop_run_goal_bindings'] loop
  execute format('alter table control.%I owner to nexloop_owner',t);
  execute format('alter table control.%I enable row level security',t);
  execute format('alter table control.%I force row level security',t);
  execute format('create policy nx022_tenant on control.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on control.%I from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime',t);
 end loop;
end $$;

create function control.nexloop_nx022_append_only() returns trigger language plpgsql set search_path=pg_catalog as $$
begin raise exception '% is append-only',tg_table_name using errcode='42501';end $$;
alter function control.nexloop_nx022_append_only() owner to nexloop_owner;
revoke all on function control.nexloop_nx022_append_only() from public;
create trigger nx022_append_only before update or delete on control.nexloop_control_events for each row execute function control.nexloop_nx022_append_only();
create trigger nx022_append_only before update or delete on control.nexloop_metric_definitions for each row execute function control.nexloop_nx022_append_only();
create trigger nx022_append_only before update or delete on control.nexloop_key_results for each row execute function control.nexloop_nx022_append_only();
create trigger nx022_append_only before update or delete on control.nexloop_metric_observations for each row execute function control.nexloop_nx022_append_only();
create trigger nx022_append_only before update or delete on control.nexloop_budget_consumption for each row execute function control.nexloop_nx022_append_only();
create trigger nx022_append_only before update or delete on control.nexloop_run_goal_bindings for each row execute function control.nexloop_nx022_append_only();
create trigger nx022_no_delete before delete on control.nexloop_control_heads for each row execute function control.nexloop_nx022_append_only();
create trigger nx022_no_delete before delete on control.nexloop_control_scopes for each row execute function control.nexloop_nx022_append_only();
create trigger nx022_no_delete before delete on control.nexloop_budget_limits for each row execute function control.nexloop_nx022_append_only();
create trigger nx022_no_delete before delete on control.nexloop_goals for each row execute function control.nexloop_nx022_append_only();

-- A published goal version is never edited in place: the only transition is
-- published -> superseded when a newer version of the same goal is published.
create function control.nexloop_nx022_goal_version_guard() returns trigger language plpgsql set search_path=pg_catalog as $$
begin
 if tg_op='DELETE' then raise exception 'goal versions are immutable' using errcode='42501';end if;
 if old.status<>'published' or new.status<>'superseded' or new.superseded_at is null
  or (to_jsonb(new)-'status'-'superseded_at') is distinct from (to_jsonb(old)-'status'-'superseded_at') then
  raise exception 'published goal version is immutable' using errcode='42501';end if;
 return new;
end $$;
alter function control.nexloop_nx022_goal_version_guard() owner to nexloop_owner;
revoke all on function control.nexloop_nx022_goal_version_guard() from public;
create trigger nx022_goal_version_guard before update or delete on control.nexloop_goal_versions for each row execute function control.nexloop_nx022_goal_version_guard();

create function control.nexloop_nx022_goal_guard() returns trigger language plpgsql set search_path=pg_catalog as $$
begin
 if (to_jsonb(new)-'current_version') is distinct from (to_jsonb(old)-'current_version') or new.current_version<>old.current_version+1 then
  raise exception 'goal identity immutable; only the version pointer advances' using errcode='42501';end if;
 return new;
end $$;
alter function control.nexloop_nx022_goal_guard() owner to nexloop_owner;
revoke all on function control.nexloop_nx022_goal_guard() from public;
create trigger nx022_goal_guard before update on control.nexloop_goals for each row execute function control.nexloop_nx022_goal_guard();

-- Server identity: tenant is never taken from the caller payload.
create function control.nexloop_nx022_identity(p_digest text,p_world text,p_service_only boolean) returns jsonb
language plpgsql set search_path=pg_catalog as $$
declare v jsonb;
begin
 v:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v is null or v->'binding'->>'tenant_id' is null or v->'binding'->>'subject_principal_id' is null then
  raise exception 'control identity unavailable' using errcode='42501';end if;
 if p_service_only and (v->'binding'->>'subject_kind' is distinct from 'service' or coalesce(v->'run_context','null'::jsonb)<>'null'::jsonb) then
  raise exception 'trusted service identity required' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v->'binding'->>'tenant_id',true);
 return v;
end $$;
alter function control.nexloop_nx022_identity(text,text,boolean) owner to nexloop_owner;
revoke all on function control.nexloop_nx022_identity(text,text,boolean) from public;

-- Every owner control change advances one tenant/world control revision and
-- leaves an append-only event. The head row lock serializes against dispatch.
create function control.nexloop_nx022_bump(p_tenant text,p_world text,p_kind text,p_scope_kind text,p_scope_ref text,p_detail jsonb,p_principal text,p_intent text)
 returns bigint language plpgsql set search_path=pg_catalog as $$
declare v bigint;
begin
 insert into control.nexloop_control_heads(tenant_id,world,revision) values(p_tenant,p_world,0) on conflict do nothing;
 update control.nexloop_control_heads set revision=revision+1 where tenant_id=p_tenant and world=p_world returning revision into v;
 insert into control.nexloop_control_events(tenant_id,world,revision,event_kind,scope_kind,scope_ref,detail,principal_id,intent_id)
  values(p_tenant,p_world,v,p_kind,p_scope_kind,p_scope_ref,p_detail,p_principal,p_intent);
 return v;
end $$;
alter function control.nexloop_nx022_bump(text,text,text,text,text,jsonb,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_nx022_bump(text,text,text,text,text,jsonb,text,text) from public;

create function control.nexloop_nx022_timestamp(p_value text) returns timestamptz language plpgsql immutable set search_path=pg_catalog as $$
begin
 if p_value is null or p_value !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|\+00:00)$' then
  raise exception 'UTC timestamp required' using errcode='22023';end if;
 return p_value::timestamptz;
end $$;
alter function control.nexloop_nx022_timestamp(text) owner to nexloop_owner;
revoke all on function control.nexloop_nx022_timestamp(text) from public;

create function control.nexloop_nx022_decimal(p_value text) returns numeric language plpgsql immutable set search_path=pg_catalog as $$
begin
 if p_value is null or p_value !~ '^-?\d{1,18}(\.\d{1,12})?$' then raise exception 'decimal string required' using errcode='22023';end if;
 return p_value::numeric;
end $$;
alter function control.nexloop_nx022_decimal(text) owner to nexloop_owner;
revoke all on function control.nexloop_nx022_decimal(text) from public;

-- Goal chain alignment: every ancestor link must still point at the ancestor's
-- current published version. Returns the goal ids in the chain.
create function control.nexloop_nx022_goal_chain(p_tenant text,p_world text,p_goal text,p_version integer) returns text[]
language plpgsql set search_path=pg_catalog as $$
declare g control.nexloop_goals%rowtype;v control.nexloop_goal_versions%rowtype;ids text[]:='{}';depth integer:=0;cur_goal text:=p_goal;cur_version integer:=p_version;
begin
 loop
  depth:=depth+1;
  if depth>8 then raise exception 'goal chain too deep' using errcode='22023';end if;
  select * into g from control.nexloop_goals where tenant_id=p_tenant and world=p_world and goal_id=cur_goal for share;
  if not found then raise exception 'goal unavailable' using errcode='NXC03';end if;
  select * into v from control.nexloop_goal_versions where tenant_id=p_tenant and world=p_world and goal_id=cur_goal and version=cur_version;
  if not found or g.current_version<>cur_version or v.status<>'published' then
   raise exception 'goal version % of % superseded',cur_version,cur_goal using errcode='NXC03';end if;
  ids:=ids||cur_goal;
  exit when v.parent_goal_id is null;
  cur_goal:=v.parent_goal_id;cur_version:=v.parent_version;
 end loop;
 return ids;
end $$;
alter function control.nexloop_nx022_goal_chain(text,text,text,integer) owner to nexloop_owner;
revoke all on function control.nexloop_nx022_goal_chain(text,text,text,integer) from public;

create function control.nexloop_nx022_write_goal(p_tenant text,p_world text,p_principal text,p_subject_kind text,p_intent text,body jsonb,p_agent boolean)
 returns jsonb language plpgsql set search_path=pg_catalog as $$
declare g control.nexloop_goals%rowtype;prev control.nexloop_goal_versions%rowtype;parent control.nexloop_goals%rowtype;pv control.nexloop_goal_versions%rowtype;
 v_exists boolean;v_kind text:=body->>'goal_kind';v_goal text:=body->>'goal_id';v_expected integer;v_version integer;v_rev bigint;
 v_start timestamptz;v_end timestamptz;v_parent text;v_parent_version integer;kr jsonb;v_impact jsonb:='{}'::jsonb;v_changed jsonb:='[]'::jsonb;
 m control.nexloop_metric_definitions%rowtype;v_ws timestamptz;v_we timestamptz;v_keys text[];
begin
 if jsonb_typeof(body->'expected_current_version') is distinct from 'number' or jsonb_typeof(body->'priority') is distinct from 'number'
  or jsonb_typeof(body->'budget') is distinct from 'object' or jsonb_typeof(body->'constraints') is distinct from 'array'
  or jsonb_typeof(body->'key_results') is distinct from 'array' or jsonb_array_length(body->'key_results') not between 1 and 16
  or jsonb_typeof(body->'objective') is distinct from 'string' or jsonb_typeof(body->'change_summary') is distinct from 'string'
  or v_goal !~ '^[a-z0-9][a-z0-9._-]{0,127}$' then
  raise exception 'goal command shape invalid' using errcode='22023';end if;
 v_expected:=(body->>'expected_current_version')::integer;
 v_start:=control.nexloop_nx022_timestamp(body->>'period_start');v_end:=control.nexloop_nx022_timestamp(body->>'period_end');
 if p_agent then
  if v_kind is distinct from 'agent' then raise exception 'agent proposals only create agent sub-goals' using errcode='42501';end if;
 elsif v_kind not in ('long_term','stage') then raise exception 'owner goal kind invalid' using errcode='22023';end if;
 if jsonb_typeof(body->'parent')='object' then
  v_parent:=body->'parent'->>'goal_id';v_parent_version:=(body->'parent'->>'version')::integer;
 elsif body->'parent' is distinct from 'null'::jsonb then raise exception 'goal parent shape invalid' using errcode='22023';end if;

 select * into g from control.nexloop_goals where tenant_id=p_tenant and world=p_world and goal_id=v_goal for update;
 v_exists:=found;
 if v_exists then
  -- AT-005: an Agent can never version an upper/owner goal, nor another Agent's goal.
  if p_agent and (g.goal_kind<>'agent' or g.owner_principal_id<>p_principal) then
   raise exception 'upper goal is immutable for agent authors' using errcode='42501';end if;
  if not p_agent and g.goal_kind='agent' then raise exception 'owner Action cannot rewrite agent goals' using errcode='42501';end if;
  if g.goal_kind<>v_kind or g.parent_goal_id is distinct from v_parent then raise exception 'goal kind and parent are immutable' using errcode='22023';end if;
  if g.current_version<>v_expected then raise exception 'goal version conflict' using errcode='40001';end if;
  select * into prev from control.nexloop_goal_versions where tenant_id=p_tenant and world=p_world and goal_id=v_goal and version=g.current_version for update;
 elsif v_expected<>0 then raise exception 'goal version conflict' using errcode='40001';
 end if;

 if v_kind='long_term' and v_parent is not null then raise exception 'long-term goal has no parent' using errcode='22023';end if;
 if v_kind='agent' and v_parent is null then raise exception 'agent sub-goal requires parent goal version' using errcode='22023';end if;
 if v_parent is not null then
  select * into parent from control.nexloop_goals where tenant_id=p_tenant and world=p_world and goal_id=v_parent for share;
  if not found or (v_kind='stage' and parent.goal_kind<>'long_term') or (v_kind='agent' and parent.goal_kind not in ('long_term','stage')) then
   raise exception 'parent goal unavailable' using errcode='42501';end if;
  if parent.current_version<>v_parent_version then raise exception 'parent goal version is not current' using errcode='NXC03';end if;
  perform control.nexloop_nx022_goal_chain(p_tenant,p_world,v_parent,v_parent_version);
  select * into pv from control.nexloop_goal_versions where tenant_id=p_tenant and world=p_world and goal_id=v_parent and version=v_parent_version;
  if v_start<pv.period_start or v_end>pv.period_end then raise exception 'goal period outside parent period' using errcode='22023';end if;
 end if;

 v_version:=v_expected+1;
 if v_exists then
  select coalesce(jsonb_agg(f),'[]'::jsonb) into v_changed from (values
   ('objective',prev.objective is distinct from body->>'objective'),('period',prev.period_start<>v_start or prev.period_end<>v_end),
   ('priority',prev.priority<>(body->>'priority')::integer),('budget',prev.budget is distinct from body->'budget'),
   ('constraints',prev.constraints is distinct from body->'constraints'),('parent_version',prev.parent_version is distinct from v_parent_version)) x(f,changed) where changed;
  select coalesce(array_agg(value->>'kr_key'),'{}') into v_keys from jsonb_array_elements(body->'key_results');
  v_impact:=jsonb_build_object('previous_version',prev.version,'changed_fields',v_changed,
   'kr_added',(select coalesce(jsonb_agg(k order by k),'[]'::jsonb) from unnest(v_keys) k where not exists(select 1 from control.nexloop_key_results r where r.tenant_id=p_tenant and r.world=p_world and r.goal_id=v_goal and r.goal_version=prev.version and r.kr_key=k)),
   'kr_removed',(select coalesce(jsonb_agg(r.kr_key order by r.kr_key),'[]'::jsonb) from control.nexloop_key_results r where r.tenant_id=p_tenant and r.world=p_world and r.goal_id=v_goal and r.goal_version=prev.version and not (r.kr_key=any(v_keys))),
   'kr_changed',(select coalesce(jsonb_agg(r.kr_key order by r.kr_key),'[]'::jsonb) from control.nexloop_key_results r join jsonb_array_elements(body->'key_results') n on n.value->>'kr_key'=r.kr_key
     where r.tenant_id=p_tenant and r.world=p_world and r.goal_id=v_goal and r.goal_version=prev.version
      and (r.metric_id is distinct from n.value->>'metric_id' or r.metric_version is distinct from (n.value->>'metric_version')::integer
       or r.target is distinct from control.nexloop_nx022_decimal(n.value->>'target') or r.direction is distinct from n.value->>'direction'
       or r.window_start is distinct from control.nexloop_nx022_timestamp(n.value->>'window_start') or r.window_end is distinct from control.nexloop_nx022_timestamp(n.value->>'window_end'))),
   'children_requiring_realignment',(select coalesce(jsonb_agg(c.goal_id order by c.goal_id),'[]'::jsonb) from control.nexloop_goals c
     join control.nexloop_goal_versions cv on cv.tenant_id=c.tenant_id and cv.world=c.world and cv.goal_id=c.goal_id and cv.version=c.current_version
     where c.tenant_id=p_tenant and c.world=p_world and cv.parent_goal_id=v_goal and cv.parent_version=prev.version),
   'run_bindings_on_previous_version',(select count(*) from control.nexloop_run_goal_bindings b where b.tenant_id=p_tenant and b.world=p_world and b.goal_id=v_goal and b.goal_version=prev.version));
 else
  v_impact:=jsonb_build_object('previous_version',null,'changed_fields','["created"]'::jsonb);
 end if;
 v_rev:=control.nexloop_nx022_bump(p_tenant,p_world,case when p_agent then 'agent_goal' else 'goal_version' end,'goal',v_goal,
  jsonb_build_object('goal_version',v_version,'impact',v_impact),p_principal,p_intent);

 if v_exists then
  update control.nexloop_goal_versions set status='superseded',superseded_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and goal_id=v_goal and version=prev.version;
  update control.nexloop_goals set current_version=v_version where tenant_id=p_tenant and world=p_world and goal_id=v_goal;
 else
  insert into control.nexloop_goals(tenant_id,world,goal_id,goal_kind,owner_principal_id,parent_goal_id,current_version)
   values(p_tenant,p_world,v_goal,v_kind,p_principal,v_parent,1);
 end if;
 insert into control.nexloop_goal_versions(tenant_id,world,goal_id,version,objective,period_start,period_end,priority,budget,constraints,
  parent_goal_id,parent_version,role_ref,status,publisher_principal_id,publisher_kind,change_summary,impact,control_revision,intent_id)
  values(p_tenant,p_world,v_goal,v_version,body->>'objective',v_start,v_end,(body->>'priority')::integer,body->'budget',body->'constraints',
  v_parent,v_parent_version,case when p_agent then body->>'role_ref' else null end,'published',p_principal,p_subject_kind,body->>'change_summary',v_impact,v_rev,p_intent);
 for kr in select value from jsonb_array_elements(body->'key_results') loop
  select * into m from control.nexloop_metric_definitions where tenant_id=p_tenant and world=p_world
   and metric_id=kr->>'metric_id' and version=(kr->>'metric_version')::integer and status='approved' for share;
  if not found then raise exception 'KR requires an approved MetricDefinition' using errcode='42501';end if;
  v_ws:=control.nexloop_nx022_timestamp(kr->>'window_start');v_we:=control.nexloop_nx022_timestamp(kr->>'window_end');
  if v_ws<v_start or v_we>v_end then raise exception 'KR window outside goal period' using errcode='22023';end if;
  insert into control.nexloop_key_results(tenant_id,world,goal_id,goal_version,kr_key,metric_id,metric_version,target,direction,window_start,window_end)
   values(p_tenant,p_world,v_goal,v_version,kr->>'kr_key',m.metric_id,m.version,control.nexloop_nx022_decimal(kr->>'target'),kr->>'direction',v_ws,v_we);
 end loop;
 return jsonb_build_object('goal_id',v_goal,'version',v_version,'control_revision',v_rev,'impact',v_impact);
end $$;
alter function control.nexloop_nx022_write_goal(text,text,text,text,text,jsonb,boolean) owner to nexloop_owner;
revoke all on function control.nexloop_nx022_write_goal(text,text,text,text,text,jsonb,boolean) from public;

create function control.nexloop_nx022_owner_change(p_tenant text,p_world text,p_principal text,p_intent text,body jsonb)
 returns jsonb language plpgsql set search_path=pg_catalog as $$
declare v_rev bigint;v_version integer;v_amount numeric;v_start timestamptz;v_end timestamptz;
begin
 if body->>'operation'='approve_metric' then
  -- Statistical definitions (denominator/window/currency/refund) are versioned and immutable.
  select coalesce(max(version),0)+1 into v_version from control.nexloop_metric_definitions
   where tenant_id=p_tenant and world=p_world and metric_id=body->>'metric_id';
  if jsonb_typeof(body->'version') is distinct from 'number' or (body->>'version')::integer<>v_version
   or jsonb_typeof(body->'maturity_seconds') is distinct from 'number' then
   raise exception 'metric version conflict' using errcode='40001';end if;
  v_rev:=control.nexloop_nx022_bump(p_tenant,p_world,'metric','metric',body->>'metric_id',jsonb_build_object('metric_version',v_version),p_principal,p_intent);
  insert into control.nexloop_metric_definitions(tenant_id,world,metric_id,version,name,aggregation,unit,currency,maturity_seconds,refund_rule,cohort_rule,status,approved_by,intent_id)
   values(p_tenant,p_world,body->>'metric_id',v_version,body->>'name',body->>'aggregation',body->>'unit',body->>'currency',
    (body->>'maturity_seconds')::integer,body->>'refund_rule',body->>'cohort_rule','approved',p_principal,p_intent);
  return jsonb_build_object('metric_id',body->>'metric_id','version',v_version,'control_revision',v_rev);
 elsif body->>'operation'='set_control' then
  if jsonb_typeof(body->'paused') is distinct from 'boolean' then raise exception 'control state invalid' using errcode='22023';end if;
  v_rev:=control.nexloop_nx022_bump(p_tenant,p_world,case when (body->>'paused')::boolean then 'pause' else 'resume' end,
   body->>'scope_kind',body->>'scope_ref',jsonb_build_object('paused',(body->>'paused')::boolean,'reason',body->>'reason'),p_principal,p_intent);
  insert into control.nexloop_control_scopes(tenant_id,world,scope_kind,scope_ref,paused,revision,reason,updated_at)
   values(p_tenant,p_world,body->>'scope_kind',body->>'scope_ref',(body->>'paused')::boolean,v_rev,body->>'reason',clock_timestamp())
   on conflict(tenant_id,world,scope_kind,scope_ref) do update set paused=excluded.paused,revision=excluded.revision,reason=excluded.reason,updated_at=excluded.updated_at;
  return jsonb_build_object('scope_kind',body->>'scope_kind','scope_ref',body->>'scope_ref','paused',(body->>'paused')::boolean,'control_revision',v_rev);
 elsif body->>'operation'='set_budget' then
  v_amount:=control.nexloop_nx022_decimal(body->>'limit_amount');
  v_start:=control.nexloop_nx022_timestamp(body->>'period_start');v_end:=control.nexloop_nx022_timestamp(body->>'period_end');
  v_rev:=control.nexloop_nx022_bump(p_tenant,p_world,'budget','budget',body->>'budget_kind',
   jsonb_build_object('unit',body->>'unit','limit_amount',body->>'limit_amount','period_start',body->>'period_start','period_end',body->>'period_end'),p_principal,p_intent);
  -- Lowering a limit never deletes or rewrites already recorded legal consumption.
  insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision,updated_at)
   values(p_tenant,p_world,body->>'budget_kind',body->>'unit',v_amount,v_start,v_end,v_rev,clock_timestamp())
   on conflict(tenant_id,world,budget_kind) do update set unit=excluded.unit,limit_amount=excluded.limit_amount,
    period_start=excluded.period_start,period_end=excluded.period_end,revision=excluded.revision,updated_at=excluded.updated_at;
  if exists(select 1 from control.nexloop_budget_consumption c where c.tenant_id=p_tenant and c.world=p_world and c.budget_kind=body->>'budget_kind' and c.unit<>body->>'unit') then
   raise exception 'budget unit cannot change under recorded consumption' using errcode='22023';end if;
  return jsonb_build_object('budget_kind',body->>'budget_kind','limit_amount',body->>'limit_amount','control_revision',v_rev);
 end if;
 raise exception 'unsupported owner control operation' using errcode='22023';
end $$;
alter function control.nexloop_nx022_owner_change(text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_nx022_owner_change(text,text,text,text,jsonb) from public;

-- Governed Action entry point. Authority is the current EIOS Action EXECUTE
-- decision, the published Action contract, the reserved claim and the live
-- identity: owner-only capabilities additionally require a human subject.
create function authz.nexloop_goal_governed_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;stored runtime.nexloop_action_claims%rowtype;
 permit jsonb:=a->'permit';v_claim jsonb:=permit->'claim';ref jsonb:=v_claim->'binding'->'action_reference';d jsonb;
 ident jsonb;v_subject text;v_cap text;v_tenant text:=a->>'tenant_id';v_principal text:=a->>'principal_id';v_id text;v_outcome jsonb;v_result jsonb;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>262144 then raise exception 'goal command too large' using errcode='22023';end if;
 if a->>'expires_at' is null or permit->>'expires_at' is null or v_claim->>'lease_expires_at' is null then
  raise exception 'goal command expiry required' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-goal-governed-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-goal-governed-v1'
  or a->>'resource_id' is distinct from 'eios:action:'||(ref->>'stable_name')||':'||(ref->>'version')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'goal permit rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'resource_id' and active;
 v_cap:=d->'capability_binding'->>'capability_name';
 if d is null or d is distinct from a->'definition' or d->'governance'->>'approval_mode'<>'none'
  or jsonb_array_length(d->'governance'->'policy_refs')<>0
  or not (d->'governance'->'change_scope'->'target_systems' @> '["postgres"]'::jsonb)
  or body->>'operation' is distinct from (case v_cap when 'goals.metric.approve' then 'approve_metric' when 'goals.version.publish' then 'publish_goal'
     when 'goals.agent.propose' then 'propose_agent_goal' when 'goals.control.set' then 'set_control' when 'goals.budget.set' then 'set_budget' end)
 then raise exception 'goal Action contract unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_subject:=ident->'binding'->>'subject_kind';
 if ident->'binding'->>'tenant_id' is distinct from v_tenant or ident->'binding'->>'subject_principal_id' is distinct from v_principal then
  raise exception 'goal identity binding mismatch' using errcode='42501';end if;
 if v_cap='goals.agent.propose' then
  if v_subject not in ('agent','human') then raise exception 'goal proposal requires an agent or human author' using errcode='42501';end if;
 elsif v_subject is distinct from 'human' then
  -- AT-005: only a human owner with a current EXECUTE grant can change
  -- owner goals, statistical definitions, controls or budgets.
  raise exception 'human goal authority required' using errcode='42501';
 end if;
 select * into stored from runtime.nexloop_action_claims where tenant_id=v_tenant and world=p_world
  and action_name=v_claim->'key'->>'action_stable_name' and intent_id=v_claim->'key'->>'idempotency_key' for update;
 if not found or stored.principal_id is distinct from v_principal or stored.claim is distinct from v_claim
  or v_claim->>'state'<>'active' or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or (permit->>'expires_at')::timestamptz<=clock_timestamp() or body->>'request_id' is distinct from stored.intent_id then
  raise exception 'goal claim fenced' using errcode='40001';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 if v_cap in ('goals.version.publish','goals.agent.propose') then
  v_result:=control.nexloop_nx022_write_goal(v_tenant,p_world,v_principal,v_subject,stored.intent_id,body,v_cap='goals.agent.propose');
 else
  v_result:=control.nexloop_nx022_owner_change(v_tenant,p_world,v_principal,stored.intent_id,body);
 end if;
 v_id:=encode(sha256(convert_to(jsonb_build_array(v_tenant,p_world,body->>'operation',stored.intent_id)::text,'UTF8')),'hex');
 v_outcome:=jsonb_build_object('outcome_id',v_id,'outcome_revision',1,'status','succeeded','outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;
 -- Commit tail: writes stay provisional until authority, contract and fences are still current.
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'resource_id' and active for share;
 if not found or d is distinct from a->'definition' then raise exception 'goal Action changed at commit' using errcode='42501';end if;
 if (a->>'expires_at')::timestamptz<=clock_timestamp() or (permit->>'expires_at')::timestamptz<=clock_timestamp()
  or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active and key_material=k) then
  raise exception 'goal authority expired at commit' using errcode='42501';end if;
 return v_result||jsonb_build_object('outcome_id',v_id,'operation',body->>'operation');
end $$;
alter function authz.nexloop_goal_governed_action(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_goal_governed_action(text,text,text,text,text) from public;
grant execute on function authz.nexloop_goal_governed_action(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

-- Planning-time control snapshot. A paused scope cannot be planned against.
create function authz.nexloop_control_snapshot(p_digest text,p_world text,p_request jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;v_tenant text;v_rev bigint;item jsonb;goals jsonb:='[]'::jsonb;objects jsonb:='[]'::jsonb;v_version integer;v_object_revision bigint;
begin
 ident:=control.nexloop_nx022_identity(p_digest,p_world,false);v_tenant:=ident->'binding'->>'tenant_id';
 perform control.nexloop_nx022_dispatch_shape(p_request,false);
 select revision into v_rev from control.nexloop_control_heads where tenant_id=v_tenant and world=p_world for share;
 v_rev:=coalesce(v_rev,0);
 perform control.nexloop_nx022_assert_not_paused(v_tenant,p_world,p_request->'scopes');
 for item in select value from jsonb_array_elements(p_request->'goals') loop
  select current_version into v_version from control.nexloop_goals where tenant_id=v_tenant and world=p_world and goal_id=item#>>'{}';
  if not found then raise exception 'goal unavailable' using errcode='NXC03';end if;
  perform control.nexloop_nx022_goal_chain(v_tenant,p_world,item#>>'{}',v_version);
  goals:=goals||jsonb_build_array(jsonb_build_object('goal_id',item#>>'{}','version',v_version));
 end loop;
 for item in select value from jsonb_array_elements(p_request->'objects') loop
  select nexloop_revision into v_object_revision from ontology.objects where tenant_id=v_tenant and world=p_world and type_name=item->>'type_name' and object_id=item->>'object_id';
  if not found then raise exception 'object unavailable' using errcode='NXC04';end if;
  objects:=objects||jsonb_build_array(jsonb_build_object('type_name',item->>'type_name','object_id',item->>'object_id','revision',v_object_revision));
 end loop;
 return jsonb_build_object('control_revision',v_rev,'scopes',p_request->'scopes','goals',goals,'objects',objects,'budgets',p_request->'budgets');
end $$;

create function control.nexloop_nx022_dispatch_shape(p jsonb,p_full boolean) returns void language plpgsql set search_path=pg_catalog as $$
begin
 if jsonb_typeof(p) is distinct from 'object' or jsonb_typeof(p->'scopes') is distinct from 'array' or jsonb_typeof(p->'goals') is distinct from 'array'
  or jsonb_typeof(p->'objects') is distinct from 'array' or jsonb_typeof(p->'budgets') is distinct from 'array'
  or jsonb_array_length(p->'scopes')>32 or jsonb_array_length(p->'goals')>16 or jsonb_array_length(p->'objects')>32
  or exists(select 1 from jsonb_array_elements(p->'scopes') s where jsonb_typeof(s) is distinct from 'object'
     or s->>'kind' not in ('role','consumer','strategy','action_type') or coalesce(s->>'ref','') !~ '^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$')
  or exists(select 1 from jsonb_array_elements(p->'budgets') b where b#>>'{}' not in ('model','incentive') or jsonb_typeof(b)<>'string')
  or exists(select 1 from jsonb_array_elements(p->'objects') o where jsonb_typeof(o) is distinct from 'object' or jsonb_typeof(o->'type_name') is distinct from 'string' or jsonb_typeof(o->'object_id') is distinct from 'string'
     or (p_full and jsonb_typeof(o->'revision') is distinct from 'number'))
  or exists(select 1 from jsonb_array_elements(p->'goals') g where (not p_full and jsonb_typeof(g) is distinct from 'string')
     or (p_full and (jsonb_typeof(g) is distinct from 'object' or jsonb_typeof(g->'goal_id') is distinct from 'string' or jsonb_typeof(g->'version') is distinct from 'number')))
  or (p_full and jsonb_typeof(p->'control_revision') is distinct from 'number') then
  raise exception 'control snapshot shape invalid' using errcode='22023';end if;
end $$;
alter function control.nexloop_nx022_dispatch_shape(jsonb,boolean) owner to nexloop_owner;
revoke all on function control.nexloop_nx022_dispatch_shape(jsonb,boolean) from public;

create function control.nexloop_nx022_assert_not_paused(p_tenant text,p_world text,p_scopes jsonb) returns void language plpgsql set search_path=pg_catalog as $$
declare hit control.nexloop_control_scopes%rowtype;
begin
 select cs.* into hit from control.nexloop_control_scopes cs where cs.tenant_id=p_tenant and cs.world=p_world and cs.paused
  and ((cs.scope_kind='tenant' and cs.scope_ref='*') or exists(select 1 from jsonb_array_elements(p_scopes) s where s->>'kind'=cs.scope_kind and s->>'ref'=cs.scope_ref))
  order by cs.revision limit 1;
 if found then raise exception 'control scope %:% paused at revision %',hit.scope_kind,hit.scope_ref,hit.revision using errcode='NXC01';end if;
end $$;
alter function control.nexloop_nx022_assert_not_paused(text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_nx022_assert_not_paused(text,text,jsonb) from public;
alter function authz.nexloop_control_snapshot(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_control_snapshot(text,text,jsonb) from public;
grant execute on function authz.nexloop_control_snapshot(text,text,jsonb) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

-- Dispatch-time check (AT-006/AT-007 control plane). A queued task or not yet
-- submitted Action presents the snapshot taken when it was planned; the current
-- control revision is re-read here under a share lock on the control head, so a
-- concurrently committing pause/goal/budget change is either seen or ordered
-- after the caller's transaction. Hook it in the caller's dispatch transaction.
create function authz.nexloop_assert_dispatch_controls(p_digest text,p_world text,p_snapshot jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;v_tenant text;v_rev bigint;v_seen bigint;item jsonb;chain text[]:='{}';v_object_revision bigint;hit control.nexloop_control_events%rowtype;
begin
 ident:=control.nexloop_nx022_identity(p_digest,p_world,true);v_tenant:=ident->'binding'->>'tenant_id';
 perform control.nexloop_nx022_dispatch_shape(p_snapshot,true);
 v_seen:=(p_snapshot->>'control_revision')::bigint;
 select revision into v_rev from control.nexloop_control_heads where tenant_id=v_tenant and world=p_world for share;
 v_rev:=coalesce(v_rev,0);
 if v_seen>v_rev or v_seen<0 then raise exception 'control snapshot from another lineage' using errcode='42501';end if;
 perform control.nexloop_nx022_assert_not_paused(v_tenant,p_world,p_snapshot->'scopes');
 for item in select value from jsonb_array_elements(p_snapshot->'goals') loop
  chain:=chain||control.nexloop_nx022_goal_chain(v_tenant,p_world,item->>'goal_id',(item->>'version')::integer);
 end loop;
 for item in select value from jsonb_array_elements(p_snapshot->'objects') loop
  select nexloop_revision into v_object_revision from ontology.objects where tenant_id=v_tenant and world=p_world and type_name=item->>'type_name' and object_id=item->>'object_id';
  if not found or v_object_revision<>(item->>'revision')::bigint then
   raise exception 'object %/% changed since planning',item->>'type_name',item->>'object_id' using errcode='NXC04';end if;
 end loop;
 select e.* into hit from control.nexloop_control_events e where e.tenant_id=v_tenant and e.world=p_world and e.revision>v_seen
  and (e.scope_kind='tenant'
   or (e.scope_kind='goal' and e.scope_ref=any(chain))
   or (e.scope_kind='budget' and exists(select 1 from jsonb_array_elements_text(p_snapshot->'budgets') b where b=e.scope_ref))
   or exists(select 1 from jsonb_array_elements(p_snapshot->'scopes') s where s->>'kind'=e.scope_kind and s->>'ref'=e.scope_ref))
  order by e.revision limit 1;
 if found then raise exception 'control revision % (%) supersedes snapshot %',hit.revision,hit.event_kind,v_seen using errcode='NXC02';end if;
 return jsonb_build_object('control_revision',v_rev,'snapshot_revision',v_seen,'checked_at',clock_timestamp());
end $$;
alter function authz.nexloop_assert_dispatch_controls(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_dispatch_controls(text,text,jsonb) from public;
grant execute on function authz.nexloop_assert_dispatch_controls(text,text,jsonb) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

-- Budget reservation (AT-043 decision part). The limit row lock serializes
-- concurrent consumers; an exhausted budget rejects new consumption without
-- touching any recorded consumption. Same consumption_id replays its result.
create function authz.nexloop_reserve_budget(p_digest text,p_world text,p_kind text,p_consumption_id text,p_amount text,p_unit text,p_source_ref text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;v_tenant text;lim control.nexloop_budget_limits%rowtype;old control.nexloop_budget_consumption%rowtype;v_amount numeric;v_used numeric;
begin
 ident:=control.nexloop_nx022_identity(p_digest,p_world,true);v_tenant:=ident->'binding'->>'tenant_id';
 v_amount:=control.nexloop_nx022_decimal(p_amount);
 if v_amount<=0 then raise exception 'budget consumption must be positive' using errcode='22023';end if;
 select * into lim from control.nexloop_budget_limits where tenant_id=v_tenant and world=p_world and budget_kind=p_kind for update;
 if not found then raise exception 'budget % not configured',p_kind using errcode='NXB03';end if;
 select * into old from control.nexloop_budget_consumption where tenant_id=v_tenant and world=p_world and budget_kind=p_kind and consumption_id=p_consumption_id;
 if found then
  if old.amount<>v_amount or old.unit<>p_unit or old.source_ref<>p_source_ref then raise exception 'budget consumption id reused' using errcode='NXB02';end if;
  return jsonb_build_object('consumption_id',p_consumption_id,'replayed',true,'limit_amount',lim.limit_amount::text,'limit_revision',lim.revision);
 end if;
 if p_unit is distinct from lim.unit or (p_kind='incentive' and v_amount<>trunc(v_amount)) then raise exception 'budget unit mismatch' using errcode='22023';end if;
 if clock_timestamp()<lim.period_start or clock_timestamp()>=lim.period_end then raise exception 'budget period closed' using errcode='NXB01';end if;
 select coalesce(sum(amount),0) into v_used from control.nexloop_budget_consumption where tenant_id=v_tenant and world=p_world and budget_kind=p_kind
  and recorded_at>=lim.period_start and recorded_at<lim.period_end;
 if v_used+v_amount>lim.limit_amount then
  raise exception 'budget % exhausted: used % of %',p_kind,v_used,lim.limit_amount using errcode='NXB01';end if;
 insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id)
  values(v_tenant,p_world,p_kind,p_consumption_id,v_amount,p_unit,p_source_ref,ident->'binding'->>'subject_principal_id');
 return jsonb_build_object('consumption_id',p_consumption_id,'replayed',false,'used',(v_used+v_amount)::text,'limit_amount',lim.limit_amount::text,
  'remaining',(lim.limit_amount-v_used-v_amount)::text,'limit_revision',lim.revision);
end $$;
alter function authz.nexloop_reserve_budget(text,text,text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_reserve_budget(text,text,text,text,text,text,text) from public;
grant execute on function authz.nexloop_reserve_budget(text,text,text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

create function authz.nexloop_budget_status(p_digest text,p_world text,p_kind text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;v_tenant text;lim control.nexloop_budget_limits%rowtype;v_used numeric;v_count bigint;
begin
 ident:=control.nexloop_nx022_identity(p_digest,p_world,false);v_tenant:=ident->'binding'->>'tenant_id';
 select * into lim from control.nexloop_budget_limits where tenant_id=v_tenant and world=p_world and budget_kind=p_kind;
 if not found then return jsonb_build_object('budget_kind',p_kind,'configured',false,'exhausted',true);end if;
 select coalesce(sum(amount),0),count(*) into v_used,v_count from control.nexloop_budget_consumption where tenant_id=v_tenant and world=p_world and budget_kind=p_kind
  and recorded_at>=lim.period_start and recorded_at<lim.period_end;
 return jsonb_build_object('budget_kind',p_kind,'configured',true,'unit',lim.unit,'limit_amount',lim.limit_amount::text,'used',v_used::text,
  'consumption_count',v_count,'exhausted',v_used>=lim.limit_amount,'limit_revision',lim.revision);
end $$;
alter function authz.nexloop_budget_status(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_budget_status(text,text,text) from public;
grant execute on function authz.nexloop_budget_status(text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;

-- Observations come from a trusted ingestion service, never from an Agent Run.
create function authz.nexloop_record_metric_observation(p_digest text,p_world text,p jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;v_tenant text;m control.nexloop_metric_definitions%rowtype;old control.nexloop_metric_observations%rowtype;v_at timestamptz;
 v_num numeric;v_den numeric;
begin
 ident:=control.nexloop_nx022_identity(p_digest,p_world,true);v_tenant:=ident->'binding'->>'tenant_id';
 if jsonb_typeof(p) is distinct from 'object' or jsonb_typeof(p->'metric_version') is distinct from 'number'
  or jsonb_typeof(p->'evidence_refs') is distinct from 'array' or jsonb_array_length(p->'evidence_refs')>32 then
  raise exception 'observation shape invalid' using errcode='22023';end if;
 select * into m from control.nexloop_metric_definitions where tenant_id=v_tenant and world=p_world and metric_id=p->>'metric_id' and version=(p->>'metric_version')::integer and status='approved';
 if not found then raise exception 'approved MetricDefinition required' using errcode='42501';end if;
 v_at:=control.nexloop_nx022_timestamp(p->>'occurred_at');v_num:=control.nexloop_nx022_decimal(p->>'numerator');
 v_den:=case when p->'denominator' is null or p->'denominator'='null'::jsonb then null else control.nexloop_nx022_decimal(p->>'denominator') end;
 if (m.aggregation='ratio_of_sums')<>(v_den is not null) then raise exception 'denominator required exactly for ratio metrics' using errcode='22023';end if;
 select * into old from control.nexloop_metric_observations where tenant_id=v_tenant and world=p_world and metric_id=m.metric_id and metric_version=m.version and observation_id=p->>'observation_id';
 if found then
  if old.numerator<>v_num or old.denominator is distinct from v_den or old.occurred_at<>v_at or old.data_mode<>p->>'data_mode' or old.source_ref<>p->>'source_ref' then
   raise exception 'observation id reused' using errcode='NXM01';end if;
  return jsonb_build_object('observation_id',old.observation_id,'replayed',true);
 end if;
 insert into control.nexloop_metric_observations(tenant_id,world,metric_id,metric_version,observation_id,numerator,denominator,occurred_at,matures_at,data_mode,source_ref,evidence_refs,recorded_by)
  values(v_tenant,p_world,m.metric_id,m.version,p->>'observation_id',v_num,v_den,v_at,v_at+make_interval(secs=>m.maturity_seconds),p->>'data_mode',p->>'source_ref',p->'evidence_refs',ident->'binding'->>'subject_principal_id');
 return jsonb_build_object('observation_id',p->>'observation_id','replayed',false);
end $$;
alter function authz.nexloop_record_metric_observation(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_record_metric_observation(text,text,jsonb) from public;
grant execute on function authz.nexloop_record_metric_observation(text,text,jsonb) to nexloop_api,nexloop_domain_worker;

-- KR value from the approved definition only: a fixed, enumerated aggregation
-- over the KR window, matured rows only; test/simulation rows never enter real.
create function authz.nexloop_compute_key_result(p_digest text,p_world text,p_goal text,p_version integer,p_kr text,p_as_of timestamptz)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;v_tenant text;kr control.nexloop_key_results%rowtype;m control.nexloop_metric_definitions%rowtype;v_as_of timestamptz;
 v_num numeric;v_den numeric;v_count bigint;v_immature bigint;v_excluded bigint;v_value numeric;
begin
 ident:=control.nexloop_nx022_identity(p_digest,p_world,false);v_tenant:=ident->'binding'->>'tenant_id';
 v_as_of:=least(coalesce(p_as_of,clock_timestamp()),clock_timestamp());
 select * into kr from control.nexloop_key_results where tenant_id=v_tenant and world=p_world and goal_id=p_goal and goal_version=p_version and kr_key=p_kr;
 if not found then raise exception 'key result unavailable' using errcode='42501';end if;
 select * into m from control.nexloop_metric_definitions where tenant_id=v_tenant and world=p_world and metric_id=kr.metric_id and version=kr.metric_version;
 select coalesce(sum(o.numerator) filter (where o.matures_at<=v_as_of and (p_world<>'real' or o.data_mode='real')),0),
        coalesce(sum(o.denominator) filter (where o.matures_at<=v_as_of and (p_world<>'real' or o.data_mode='real')),0),
        count(*) filter (where o.matures_at<=v_as_of and (p_world<>'real' or o.data_mode='real')),
        count(*) filter (where o.matures_at>v_as_of and (p_world<>'real' or o.data_mode='real')),
        count(*) filter (where p_world='real' and o.data_mode<>'real')
   into v_num,v_den,v_count,v_immature,v_excluded
   from control.nexloop_metric_observations o where o.tenant_id=v_tenant and o.world=p_world and o.metric_id=m.metric_id and o.metric_version=m.version
    and o.occurred_at>=kr.window_start and o.occurred_at<kr.window_end and o.occurred_at<=v_as_of;
 v_value:=case m.aggregation when 'count' then v_count when 'sum' then v_num when 'ratio_of_sums' then v_num/nullif(v_den,0) end;
 return jsonb_build_object('goal_id',p_goal,'goal_version',p_version,'kr_key',p_kr,'metric_id',m.metric_id,'metric_version',m.version,
  'aggregation',m.aggregation,'unit',m.unit,'currency',m.currency,'refund_rule',m.refund_rule,'as_of',v_as_of,
  'window_start',kr.window_start,'window_end',kr.window_end,'value',v_value::text,'target',kr.target::text,'direction',kr.direction,
  'met',case when v_value is null then null when kr.direction='at_least' then v_value>=kr.target else v_value<=kr.target end,
  'matured_observations',v_count,'immature_observations',v_immature,'excluded_non_real_observations',v_excluded);
end $$;
alter function authz.nexloop_compute_key_result(text,text,text,integer,text,timestamptz) owner to nexloop_owner;
revoke all on function authz.nexloop_compute_key_result(text,text,text,integer,text,timestamptz) from public;
grant execute on function authz.nexloop_compute_key_result(text,text,text,integer,text,timestamptz) to nexloop_api,nexloop_domain_worker,nexloop_scheduler;

-- Run -> goal version trace. Only a current, aligned goal version can be bound.
create function authz.nexloop_bind_run_goal(p_digest text,p_world text,p_run uuid,p_goal text,p_version integer) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;v_tenant text;v_rev bigint;old control.nexloop_run_goal_bindings%rowtype;
begin
 ident:=control.nexloop_nx022_identity(p_digest,p_world,true);v_tenant:=ident->'binding'->>'tenant_id';
 select * into old from control.nexloop_run_goal_bindings where tenant_id=v_tenant and world=p_world and run_id=p_run and goal_id=p_goal;
 if found then
  if old.goal_version<>p_version then raise exception 'Run already bound to another goal version' using errcode='40001';end if;
  return jsonb_build_object('run_id',p_run,'goal_id',p_goal,'goal_version',old.goal_version,'control_revision',old.control_revision,'replayed',true);
 end if;
 select revision into v_rev from control.nexloop_control_heads where tenant_id=v_tenant and world=p_world for share;
 perform control.nexloop_nx022_goal_chain(v_tenant,p_world,p_goal,p_version);
 insert into control.nexloop_run_goal_bindings(tenant_id,world,run_id,goal_id,goal_version,control_revision,principal_id)
  values(v_tenant,p_world,p_run,p_goal,p_version,coalesce(v_rev,0),ident->'binding'->>'subject_principal_id');
 return jsonb_build_object('run_id',p_run,'goal_id',p_goal,'goal_version',p_version,'control_revision',coalesce(v_rev,0),'replayed',false);
end $$;
alter function authz.nexloop_bind_run_goal(text,text,uuid,text,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_bind_run_goal(text,text,uuid,text,integer) from public;
grant execute on function authz.nexloop_bind_run_goal(text,text,uuid,text,integer) to nexloop_api,nexloop_domain_worker,nexloop_scheduler;

-- Read a goal version (current when p_version is null) with KRs and alignment.
create function authz.nexloop_read_goal(p_digest text,p_world text,p_goal text,p_version integer) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;v_tenant text;g control.nexloop_goals%rowtype;v control.nexloop_goal_versions%rowtype;v_aligned boolean:=true;
begin
 ident:=control.nexloop_nx022_identity(p_digest,p_world,false);v_tenant:=ident->'binding'->>'tenant_id';
 select * into g from control.nexloop_goals where tenant_id=v_tenant and world=p_world and goal_id=p_goal;
 if not found then raise exception 'goal unavailable' using errcode='42501';end if;
 select * into v from control.nexloop_goal_versions where tenant_id=v_tenant and world=p_world and goal_id=p_goal and version=coalesce(p_version,g.current_version);
 if not found then raise exception 'goal version unavailable' using errcode='42501';end if;
 begin
  perform control.nexloop_nx022_goal_chain(v_tenant,p_world,p_goal,v.version);
 exception when sqlstate 'NXC03' then v_aligned:=false;
 end;
 return jsonb_build_object('goal_id',g.goal_id,'goal_kind',g.goal_kind,'owner_principal_id',g.owner_principal_id,'current_version',g.current_version,
  'version',v.version,'status',v.status,'aligned',v_aligned,'objective',v.objective,'period_start',v.period_start,'period_end',v.period_end,
  'priority',v.priority,'budget',v.budget,'constraints',v.constraints,'parent',case when v.parent_goal_id is null then null else jsonb_build_object('goal_id',v.parent_goal_id,'version',v.parent_version) end,
  'role_ref',v.role_ref,'publisher_principal_id',v.publisher_principal_id,'publisher_kind',v.publisher_kind,'change_summary',v.change_summary,
  'impact',v.impact,'control_revision',v.control_revision,'published_at',v.published_at,'superseded_at',v.superseded_at,
  'key_results',(select coalesce(jsonb_agg(jsonb_build_object('kr_key',r.kr_key,'metric_id',r.metric_id,'metric_version',r.metric_version,'target',r.target::text,
     'direction',r.direction,'window_start',r.window_start,'window_end',r.window_end) order by r.kr_key),'[]'::jsonb)
   from control.nexloop_key_results r where r.tenant_id=v_tenant and r.world=p_world and r.goal_id=p_goal and r.goal_version=v.version));
end $$;
alter function authz.nexloop_read_goal(text,text,text,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_read_goal(text,text,text,integer) from public;
grant execute on function authz.nexloop_read_goal(text,text,text,integer) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
