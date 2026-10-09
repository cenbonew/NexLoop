-- NX-024: strategy / plan / reevaluation scheduling (approved design docs/implementation/NX-024-design.md,
-- dispatcher rulings 2026-10-10). Plans, strategies, step metadata and Run outcomes are the Agent's working
-- state, not formal business facts: runtime-schema tables, append-only, FORCE RLS, written only through the
-- signed ports below. Reevaluations are marked in the same transaction as the change that causes them
-- (goal version / control event / dispatch denial / strategy change / reassess_at / external result) into the
-- 0093 work feed 'plan-reevaluate', whose available_at is the UTC due time. Recording a Run outcome needs the
-- Run's live activation under the caller's own current task lease and the exact stored command.

create table runtime.nexloop_plans (
 tenant_id text not null,world text not null,plan_id uuid not null,version integer not null check(version between 1 and 100000),
 consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),
 goal_version_ref text not null check(goal_version_ref~'^goal:[a-z0-9][a-z0-9._-]{0,127}@[1-9][0-9]{0,8}$'),
 strategy_ref text check(strategy_ref~'^strategy:[0-9a-f-]{36}@[1-9][0-9]{0,8}$'),
 context_strategy_ref text not null check(context_strategy_ref~'^context-strategy:[a-z][a-z0-9_]{0,63}@[1-9][0-9]{0,6}$'),
 recipe jsonb not null check(jsonb_typeof(recipe)='object'),settings jsonb not null check(jsonb_typeof(settings)='object'),
 control_snapshot jsonb not null check(jsonb_typeof(control_snapshot)='object'),
 created_by text not null,created_by_run uuid,created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,plan_id,version)
);
create table runtime.nexloop_plan_events (
 tenant_id text not null,world text not null,event_id bigint generated always as identity,plan_id uuid not null,version integer not null,
 status text not null check(status in ('active','superseded','stale','invalidated','closed')),reason text not null check(length(reason) between 1 and 500),
 run_id uuid,recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,event_id),foreign key(tenant_id,world,plan_id,version) references runtime.nexloop_plans
);
create index nexloop_plan_events_plan on runtime.nexloop_plan_events(tenant_id,world,plan_id,event_id desc);
create table runtime.nexloop_plan_steps (
 tenant_id text not null,world text not null,plan_id uuid not null,version integer not null,
 step_key text not null check(step_key~'^[a-z0-9][a-z0-9._-]{0,63}$'),step_object_id text check(step_object_id~'^[a-f0-9]{64}$'),
 prerequisites jsonb not null check(jsonb_typeof(prerequisites)='array'),expected_result text not null check(length(expected_result) between 1 and 2000),
 stop_if jsonb not null check(jsonb_typeof(stop_if)='array'),reassess_at timestamptz,budget jsonb not null check(jsonb_typeof(budget)='object'),intent_ref uuid,
 primary key(tenant_id,world,plan_id,version,step_key),foreign key(tenant_id,world,plan_id,version) references runtime.nexloop_plans
);
create index nexloop_plan_steps_intent on runtime.nexloop_plan_steps(tenant_id,world,intent_ref) where intent_ref is not null;
create table runtime.nexloop_strategies (
 tenant_id text not null,world text not null,strategy_id uuid not null,version integer not null check(version between 1 and 100000),
 consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),goal_version_ref text not null,content text not null check(length(content) between 1 and 8192),
 created_by text not null,created_by_run uuid,created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,strategy_id,version)
);
create table runtime.nexloop_plan_runs (
 tenant_id text not null,world text not null,run_id uuid not null,plan_id uuid not null,version integer not null,
 triggers jsonb not null check(jsonb_typeof(triggers)='array'),created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,run_id),foreign key(tenant_id,world,plan_id,version) references runtime.nexloop_plans
);
create table runtime.nexloop_plan_outcomes (
 tenant_id text not null,world text not null,run_id uuid not null,plan_id uuid not null,version integer not null,
 kind text not null check(kind in ('no_action','needs_information','waiting_external','escalate','plan_update','action_intent')),
 outcome jsonb not null check(jsonb_typeof(outcome)='object'),outcome_digest text not null check(outcome_digest~'^[a-f0-9]{64}$'),
 intent_ref uuid,new_version integer,recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,run_id),foreign key(tenant_id,world,run_id) references runtime.nexloop_plan_runs,
 foreign key(tenant_id,world,plan_id,version) references runtime.nexloop_plans
);
create index nexloop_plan_outcomes_intent on runtime.nexloop_plan_outcomes(tenant_id,world,intent_ref) where intent_ref is not null;

create function runtime.nexloop_plan_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'plan records are append-only' using errcode='22023';end $$;
do $tables$
declare t text;
begin
 foreach t in array array['nexloop_plans','nexloop_plan_events','nexloop_plan_steps','nexloop_strategies','nexloop_plan_runs','nexloop_plan_outcomes'] loop
  execute format('alter table runtime.%I owner to nexloop_owner',t);
  execute format('alter table runtime.%I enable row level security',t);
  execute format('alter table runtime.%I force row level security',t);
  execute format('create policy tenant_boundary on runtime.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on runtime.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator',t);
  execute format('create trigger nexloop_append_only before update or delete on runtime.%I for each row execute function runtime.nexloop_plan_append_only()',t);
 end loop;
end $tables$;
alter function runtime.nexloop_plan_append_only() owner to nexloop_owner;
revoke all on function runtime.nexloop_plan_append_only() from public;

-- Current version and status of a plan (latest event).
create function runtime.nexloop_plan_current(p_tenant text,p_world text,p_plan uuid,out version integer,out status text)
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select e.version,e.status from runtime.nexloop_plan_events e where e.tenant_id=p_tenant and e.world=p_world and e.plan_id=p_plan order by e.event_id desc limit 1
$$;

-- 0093 feed gains 'plan-reevaluate'; a due-time touch keeps the earliest pending due and accumulates triggers.
alter table runtime.nexloop_work_feed drop constraint nexloop_work_feed_feed_check;
alter table runtime.nexloop_work_feed add constraint nexloop_work_feed_feed_check check(feed in ('recall-instance','claim-match','plan-reevaluate'));
create function authz.nexloop_plan_feed_touch(p_tenant text,p_world text,p_plan uuid,p_trigger jsonb,p_due timestamptz) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);due timestamptz:=greatest(coalesce(p_due,clock_timestamp()),clock_timestamp());
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.nexloop_work_feed as f(tenant_id,world,feed,item_key,payload,available_at)
  values(p_tenant,p_world,'plan-reevaluate','plan:'||p_plan,jsonb_build_object('plan_id',p_plan,'triggers',jsonb_build_array(p_trigger)),due)
 on conflict(tenant_id,world,feed,item_key) do update set
  payload=jsonb_build_object('plan_id',p_plan,'triggers',(select coalesce(jsonb_agg(x order by n desc),'[]'::jsonb) from (select x,n from jsonb_array_elements(f.payload->'triggers'||jsonb_build_array(p_trigger)) with ordinality t(x,n) order by n desc limit 16) s)),
  change_seq=f.change_seq+1,changed_at=clock_timestamp(),
  available_at=case when f.status='pending' then least(f.available_at,due) else due end,attempts=0,status='pending',last_code=null;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
end $$;

-- Active (current, status active) plan versions of a tenant/world.
create function runtime.nexloop_active_plans(p_tenant text,p_world text) returns setof runtime.nexloop_plans
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select p.* from runtime.nexloop_plans p
 where p.tenant_id=p_tenant and p.world=p_world
  and (select e.version from runtime.nexloop_plan_events e where e.tenant_id=p.tenant_id and e.world=p.world and e.plan_id=p.plan_id order by e.event_id desc limit 1)=p.version
  and (select e.status from runtime.nexloop_plan_events e where e.tenant_id=p.tenant_id and e.world=p.world and e.plan_id=p.plan_id order by e.event_id desc limit 1)='active'
$$;

-- T1/T2/T3: control events (pause, resume, budget, goal version, agent goal, metric) in the owner's transaction.
create function runtime.nexloop_plan_on_control_event() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r record;kind text;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 kind:=case new.event_kind when 'pause' then 'control_paused' when 'resume' then 'resume' when 'goal_version' then 'goal_version_stale'
  when 'agent_goal' then 'goal_version_stale' else 'control_revision' end;
 for r in select p.plan_id from runtime.nexloop_active_plans(new.tenant_id,new.world) p
  where new.scope_kind='tenant'
   or (new.scope_kind='goal' and p.recipe->'goal_chain' ? new.scope_ref)
   or (new.scope_kind='budget' and p.control_snapshot->'budgets' ? new.scope_ref)
   or exists(select 1 from jsonb_array_elements(p.control_snapshot->'scopes') s where s->>'kind'=new.scope_kind and s->>'ref'=new.scope_ref) loop
  perform authz.nexloop_plan_feed_touch(new.tenant_id,new.world,r.plan_id,
   jsonb_build_object('kind',kind,'cause','external','control_revision',new.revision,'at',clock_timestamp()),null);
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
create trigger nexloop_plan_control_event after insert on control.nexloop_control_events for each row execute function runtime.nexloop_plan_on_control_event();

-- T2: a reevaluation Run's task denied at dispatch by a control check (resume is not replay).
create function runtime.nexloop_plan_on_job_failed() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);pr runtime.nexloop_plan_runs%rowtype;
begin
 if new.status<>'failed' or old.status='failed' or coalesce(new.result->>'code','') not in
   ('control_paused','goal_version_stale','control_revision_stale','object_revision_stale','budget_exhausted','control_snapshot_missing') then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select r.* into pr from runtime.nexloop_plan_runs r join authz.nexloop_runtime_run_bindings b on b.run_id=r.run_id
  where b.tenant_id=new.tenant_id and b.task_id=new.job_id and r.tenant_id=new.tenant_id;
 if found then
  perform authz.nexloop_plan_feed_touch(pr.tenant_id,pr.world,pr.plan_id,
   jsonb_build_object('kind','dispatch_denied','cause','external','code',new.result->>'code','run_id',pr.run_id,'at',clock_timestamp()),null);
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
create trigger nexloop_plan_job_failed after update of status on runtime.jobs for each row execute function runtime.nexloop_plan_on_job_failed();

-- T4: strategy changes (operational Strategy versions and Context strategy versions).
create function runtime.nexloop_plan_on_strategy() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r record;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 if tg_table_name='nexloop_strategies' then
  for r in select p.plan_id from runtime.nexloop_active_plans(new.tenant_id,new.world) p
   where p.strategy_ref like 'strategy:'||new.strategy_id||'@%' and split_part(p.strategy_ref,'@',2)::integer<new.version loop
   perform authz.nexloop_plan_feed_touch(new.tenant_id,new.world,r.plan_id,jsonb_build_object('kind','strategy_changed','cause',
    case when new.created_by_run is null then 'external' else 'self' end,'ref','strategy:'||new.strategy_id||'@'||new.version,'at',clock_timestamp()),null);
  end loop;
 else
  for r in select p.plan_id from runtime.nexloop_active_plans(new.tenant_id,new.world) p
   where p.context_strategy_ref like 'context-strategy:'||new.strategy_id||'@%' and split_part(p.context_strategy_ref,'@',2)::integer<new.version loop
   perform authz.nexloop_plan_feed_touch(new.tenant_id,new.world,r.plan_id,jsonb_build_object('kind','strategy_changed','cause','external',
    'ref','context-strategy:'||new.strategy_id||'@'||new.version,'at',clock_timestamp()),null);
  end loop;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
create trigger nexloop_plan_strategy after insert on runtime.nexloop_strategies for each row execute function runtime.nexloop_plan_on_strategy();
create trigger nexloop_plan_context_strategy after insert on control.nexloop_context_strategies for each row execute function runtime.nexloop_plan_on_strategy();

-- T6: an external result of an intent a plan waits on.
create function runtime.nexloop_plan_on_observation() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r record;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 for r in select distinct p.plan_id from runtime.nexloop_active_plans(new.tenant_id,new.world) p
  where exists(select 1 from runtime.nexloop_plan_steps s where s.tenant_id=p.tenant_id and s.world=p.world and s.plan_id=p.plan_id and s.version=p.version and s.intent_ref=new.intent_id)
   or exists(select 1 from runtime.nexloop_plan_outcomes o where o.tenant_id=p.tenant_id and o.world=p.world and o.plan_id=p.plan_id and o.intent_ref=new.intent_id) loop
  perform authz.nexloop_plan_feed_touch(new.tenant_id,new.world,r.plan_id,jsonb_build_object('kind','external_result','cause','external',
   'intent_id',new.intent_id,'provider_state',new.provider_state,'at',clock_timestamp()),null);
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
create trigger nexloop_plan_observation after insert on runtime.nexloop_effect_observations for each row execute function runtime.nexloop_plan_on_observation();

-- T7 (interface only, M18): a commitment due marks its plan; no caller is granted yet.
create function runtime.nexloop_plan_commitment_due(p_tenant text,p_world text,p_plan uuid,p_commitment_ref text,p_due timestamptz) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if p_commitment_ref is null or p_commitment_ref!~'^[A-Za-z][A-Za-z0-9_.-]*:\S+$' or length(p_commitment_ref)>512 then raise exception 'commitment reference invalid' using errcode='22023';end if;
 perform authz.nexloop_plan_feed_touch(p_tenant,p_world,p_plan,jsonb_build_object('kind','commitment_due','cause','external','ref',p_commitment_ref,'at',clock_timestamp()),p_due);
end $$;

-- T5 helper: the next reassess_at of a plan version, never earlier than the plan's min_reassess_seconds.
create function runtime.nexloop_plan_mark_due(p_tenant text,p_world text,p_plan uuid,p_version integer,p_cause text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare due timestamptz;settings jsonb;
begin
 select p.settings into settings from runtime.nexloop_plans p where p.tenant_id=p_tenant and p.world=p_world and p.plan_id=p_plan and p.version=p_version;
 select min(s.reassess_at) into due from runtime.nexloop_plan_steps s where s.tenant_id=p_tenant and s.world=p_world and s.plan_id=p_plan and s.version=p_version and s.reassess_at is not null;
 if due is null then return;end if;
 due:=greatest(due,clock_timestamp()+make_interval(secs=>(settings->>'min_reassess_seconds')::integer));
 perform authz.nexloop_plan_feed_touch(p_tenant,p_world,p_plan,jsonb_build_object('kind','reassess_due','cause',p_cause,'due',due,'at',clock_timestamp()),due);
end $$;

-- Exact step shape (establish and plan_update).
create function runtime.nexloop_plan_steps_valid(p_steps jsonb) returns boolean
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select jsonb_typeof(p_steps)='array' and jsonb_array_length(p_steps) between 1 and 32
  and (select count(distinct s->>'step_key') from jsonb_array_elements(p_steps) s)=jsonb_array_length(p_steps)
  and not exists(select 1 from jsonb_array_elements(p_steps) s where
   jsonb_typeof(s) is distinct from 'object'
   or (select array_agg(k order by k) from jsonb_object_keys(s) k) is distinct from array['budget','expected_result','intent_ref','prerequisites','reassess_at','step_key','step_object_id','stop_if']
   or coalesce(s->>'step_key','')!~'^[a-z0-9][a-z0-9._-]{0,63}$'
   or (jsonb_typeof(s->'step_object_id')<>'null' and coalesce(s->>'step_object_id','')!~'^[a-f0-9]{64}$')
   or jsonb_typeof(s->'prerequisites') is distinct from 'array' or jsonb_array_length(s->'prerequisites')>32
   or exists(select 1 from jsonb_array_elements(s->'prerequisites') q where jsonb_typeof(q)<>'string' or length(q#>>'{}') not between 1 and 500)
   or jsonb_typeof(s->'expected_result') is distinct from 'string' or length(s->>'expected_result') not between 1 and 2000
   or jsonb_typeof(s->'stop_if') is distinct from 'array' or jsonb_array_length(s->'stop_if')>16
   or exists(select 1 from jsonb_array_elements(s->'stop_if') c where jsonb_typeof(c) is distinct from 'object'
      or (select array_agg(k order by k) from jsonb_object_keys(c) k) is distinct from array['equals','object_id','property','type_name']
      or coalesce(c->>'type_name','')!~'^[A-Za-z][A-Za-z0-9_]{0,63}$' or coalesce(c->>'object_id','')!~'^[a-f0-9]{64}$'
      or coalesce(c->>'property','')!~'^[A-Za-z][A-Za-z0-9_]{0,63}$')
   or (jsonb_typeof(s->'reassess_at')<>'null' and (jsonb_typeof(s->'reassess_at')<>'string' or (s->>'reassess_at')!~'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|\+00:00)$'))
   or jsonb_typeof(s->'budget') is distinct from 'object'
   or (select array_agg(k order by k) from jsonb_object_keys(s->'budget') k) is distinct from array['active_timeout_seconds','maximum_model_turns','maximum_tool_calls']
   or jsonb_typeof(s->'budget'->'maximum_model_turns')<>'number' or (s->'budget'->>'maximum_model_turns')!~'^[1-9][0-9]?$' or (s->'budget'->>'maximum_model_turns')::integer>64
   or jsonb_typeof(s->'budget'->'maximum_tool_calls')<>'number' or (s->'budget'->>'maximum_tool_calls')!~'^[1-9][0-9]{0,2}$' or (s->'budget'->>'maximum_tool_calls')::integer>128
   or jsonb_typeof(s->'budget'->'active_timeout_seconds')<>'number' or (s->'budget'->>'active_timeout_seconds')!~'^[1-9][0-9]{0,3}$' or (s->'budget'->>'active_timeout_seconds')::integer>3600
   or (jsonb_typeof(s->'intent_ref')<>'null' and coalesce(s->>'intent_ref','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'))
$$;

create function runtime.nexloop_plan_insert_steps(p_tenant text,p_world text,p_plan uuid,p_version integer,p_steps jsonb) returns void
 language sql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 insert into runtime.nexloop_plan_steps(tenant_id,world,plan_id,version,step_key,step_object_id,prerequisites,expected_result,stop_if,reassess_at,budget,intent_ref)
 select p_tenant,p_world,p_plan,p_version,s->>'step_key',s->>'step_object_id',s->'prerequisites',s->>'expected_result',s->'stop_if',(s->>'reassess_at')::timestamptz,s->'budget',(s->>'intent_ref')::uuid
 from jsonb_array_elements(p_steps) s
$$;

-- Server-derived control snapshot for a plan (consumer scope, the plan's goal, the Consumer object, model budget).
create function runtime.nexloop_plan_snapshot(p_digest text,p_world text,p_consumer text,p_goal text) returns jsonb
 language sql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select authz.nexloop_control_snapshot(p_digest,p_world,jsonb_build_object('scopes',jsonb_build_array(jsonb_build_object('kind','consumer','ref',p_consumer)),
  'goals',jsonb_build_array(p_goal),'objects',jsonb_build_array(jsonb_build_object('type_name','Consumer','object_id',p_consumer)),'budgets','["model"]'::jsonb))
$$;

-- Shared signed-claims prologue for the two NX-024 ports; returns the tenant.
create function authz.nexloop_plan_port_tenant(p_digest text,p_world text,p_text text,p_signature text,p_payload text,p_protocol text,p_action text,p_roles text[]) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;k bytea;ident jsonb;
begin
 if not (session_user=any(p_roles)) or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>262144
  or a->>'protocol' is distinct from p_protocol or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from p_action or a->>'resource_id' is distinct from p_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'plan port unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to(p_protocol||':'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'plan port unavailable' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest)
  or not exists(select 1 from authz.nexloop_service_credentials t where t.token_digest=p_digest and t.tenant_id=a->>'tenant_id'
     and t.binding->>'subject_kind'='service' and t.status='active') then
  raise exception 'plan port unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb then raise exception 'plan port unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',a->>'tenant_id',true);
 return a->>'tenant_id';
end $$;

-- Reevaluator service port (domain worker; eios:action:nexloop.plan.reevaluate:1).
create function authz.nexloop_plan_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-plan-command-v1','eios:action:nexloop.plan.reevaluate:1',array['nexloop_domain_worker']);
 c jsonb:=p_payload::jsonb;s jsonb;v_plan uuid;cur record;p runtime.nexloop_plans%rowtype;v_goal_id text;v_goal_version integer;chain text[];snap jsonb;
 strategy_ref text;existing runtime.nexloop_plans%rowtype;reasons text[]:='{}';decision text;cond jsonb;stp jsonb;val jsonb;v_count integer;v_oldest timestamptz;
 revisions jsonb;v_goal_object text;v_current integer;
begin
 if c->>'verb'='establish' then
  s:=c->'plan';
  if jsonb_typeof(s) is distinct from 'object'
   or (select array_agg(k order by k) from jsonb_object_keys(s) k) is distinct from array['consumer_id','context_strategy_ref','goal_version_ref','plan_id','recipe','settings','steps','strategy']
   or coalesce(s->>'plan_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' or coalesce(s->>'consumer_id','')!~'^[a-f0-9]{64}$'
   or coalesce(s->>'goal_version_ref','')!~'^goal:[a-z0-9][a-z0-9._-]{0,127}@[1-9][0-9]{0,8}$'
   or coalesce(s->>'context_strategy_ref','')!~'^context-strategy:[a-z][a-z0-9_]{0,63}@[1-9][0-9]{0,6}$'
   or jsonb_typeof(s->'recipe') is distinct from 'object'
   or (select array_agg(k order by k) from jsonb_object_keys(s->'recipe') k) is distinct from array['binding_id','control_id','link_id','offering_id','role_id','step_id']
   or exists(select 1 from jsonb_each_text(s->'recipe') r where r.value!~'^[a-f0-9]{64}$')
   or jsonb_typeof(s->'settings') is distinct from 'object'
   or (select array_agg(k order by k) from jsonb_object_keys(s->'settings') k) is distinct from array['min_reassess_seconds','self_trigger_cap','self_window_seconds']
   or exists(select 1 from jsonb_each(s->'settings') r where jsonb_typeof(r.value)<>'number' or (r.value#>>'{}')!~'^[0-9]{1,6}$')
   or (s->'settings'->>'self_window_seconds')::integer not between 1 and 86400 or (s->'settings'->>'self_trigger_cap')::integer not between 1 and 100
   or (s->'settings'->>'min_reassess_seconds')::integer>86400
   or not runtime.nexloop_plan_steps_valid(s->'steps')
   or (jsonb_typeof(s->'strategy')<>'null' and (jsonb_typeof(s->'strategy')<>'object' or (select array_agg(k order by k) from jsonb_object_keys(s->'strategy') k) is distinct from array['content']
      or jsonb_typeof(s->'strategy'->'content')<>'string' or length(s->'strategy'->>'content') not between 1 and 8192)) then
   raise exception 'plan shape invalid' using errcode='22023';end if;
  v_plan:=(s->>'plan_id')::uuid;
  if not exists(select 1 from control.nexloop_context_strategies cs where cs.tenant_id=t and cs.world=p_world
    and 'context-strategy:'||cs.strategy_id||'@'||cs.version=s->>'context_strategy_ref') then raise exception 'context strategy unavailable' using errcode='22023';end if;
  v_goal_id:=split_part(substr(s->>'goal_version_ref',6),'@',1);v_goal_version:=split_part(s->>'goal_version_ref','@',2)::integer;
  chain:=control.nexloop_nx022_goal_chain(t,p_world,v_goal_id,v_goal_version);
  select * into existing from runtime.nexloop_plans where tenant_id=t and world=p_world and plan_id=v_plan and version=1;
  if found then
   if existing.consumer_id<>s->>'consumer_id' or existing.goal_version_ref<>s->>'goal_version_ref' or existing.context_strategy_ref<>s->>'context_strategy_ref'
    or existing.recipe-'goal_chain'<>s->'recipe' or existing.settings<>s->'settings' then raise exception 'plan id reused' using errcode='40001';end if;
   return jsonb_build_object('plan_id',v_plan,'version',1,'replay',true);
  end if;
  snap:=runtime.nexloop_plan_snapshot(p_digest,p_world,s->>'consumer_id',v_goal_id);
  if (snap->'goals'->0->>'version')::integer is distinct from v_goal_version then raise exception 'goal version superseded' using errcode='NXC03';end if;
  if jsonb_typeof(s->'strategy')='object' then
   strategy_ref:='strategy:'||v_plan||'@1';
   insert into runtime.nexloop_strategies(tenant_id,world,strategy_id,version,consumer_id,goal_version_ref,content,created_by)
    values(t,p_world,v_plan,1,s->>'consumer_id',s->>'goal_version_ref',s->'strategy'->>'content','service');
  end if;
  insert into runtime.nexloop_plans(tenant_id,world,plan_id,version,consumer_id,goal_version_ref,strategy_ref,context_strategy_ref,recipe,settings,control_snapshot,created_by)
   values(t,p_world,v_plan,1,s->>'consumer_id',s->>'goal_version_ref',strategy_ref,s->>'context_strategy_ref',s->'recipe'||jsonb_build_object('goal_chain',to_jsonb(chain)),s->'settings',snap,'service');
  perform runtime.nexloop_plan_insert_steps(t,p_world,v_plan,1,s->'steps');
  insert into runtime.nexloop_plan_events(tenant_id,world,plan_id,version,status,reason) values(t,p_world,v_plan,1,'active','established');
  perform runtime.nexloop_plan_mark_due(t,p_world,v_plan,1,'external');
  return jsonb_build_object('plan_id',v_plan,'version',1,'replay',false,'control_revision',snap->'control_revision');
 end if;
 v_plan:=(c->>'plan_id')::uuid;
 select * into cur from runtime.nexloop_plan_current(t,p_world,v_plan);
 if cur.version is null then raise exception 'plan unavailable' using errcode='42501';end if;
 select * into p from runtime.nexloop_plans where tenant_id=t and world=p_world and plan_id=v_plan and version=cur.version;
 if c->>'verb'='read' then
  return jsonb_build_object('plan',to_jsonb(p)-'tenant_id','status',cur.status,'steps',coalesce((select jsonb_agg(to_jsonb(x)-'tenant_id'-'world' order by x.step_key)
   from runtime.nexloop_plan_steps x where x.tenant_id=t and x.world=p_world and x.plan_id=v_plan and x.version=cur.version),'[]'::jsonb));
 elsif c->>'verb'='precheck' then
  if cur.status<>'active' then return jsonb_build_object('decision','closed','status',cur.status,'version',cur.version);end if;
  -- stop_if: a step's condition already true (e.g. renewed / resolved) ends the plan without a model call.
  for stp in select to_jsonb(x) from runtime.nexloop_plan_steps x where x.tenant_id=t and x.world=p_world and x.plan_id=v_plan and x.version=cur.version loop
   for cond in select value from jsonb_array_elements(stp->'stop_if') loop
    select o.properties->(cond->>'property') into val from ontology.objects o where o.tenant_id=t and o.world=p_world and o.type_name=cond->>'type_name' and o.object_id=cond->>'object_id';
    if val is not null and val=cond->'equals' then reasons:=array_append(reasons,'stop_if:'||(stp->>'step_key'));end if;
   end loop;
  end loop;
  if cardinality(reasons)>0 then return jsonb_build_object('decision','invalidated','version',cur.version,'reasons',to_jsonb(reasons));end if;
  begin
   perform authz.nexloop_assert_dispatch_controls(p_digest,p_world,p.control_snapshot);
  exception
   when sqlstate 'NXC01' then return jsonb_build_object('decision','paused','version',cur.version,'reasons','["control_paused"]'::jsonb);
   when sqlstate 'NXC03' then reasons:=array_append(reasons,'goal_version_stale');
   when sqlstate 'NXC04' then reasons:=array_append(reasons,'object_revision_stale');
   when sqlstate 'NXC02' then reasons:=array_append(reasons,'control_revision_stale');
  end;
  -- Self-trigger throttle (M16): only self-caused triggers, at most self_trigger_cap Runs per self_window.
  if jsonb_typeof(c->'triggers')='array' and jsonb_array_length(c->'triggers')>0
   and not exists(select 1 from jsonb_array_elements(c->'triggers') g where g->>'cause' is distinct from 'self') then
   select count(*),min(r.created_at) into v_count,v_oldest from runtime.nexloop_plan_runs r where r.tenant_id=t and r.world=p_world and r.plan_id=v_plan
    and r.created_at>clock_timestamp()-make_interval(secs=>(p.settings->>'self_window_seconds')::integer);
   if v_count>=(p.settings->>'self_trigger_cap')::integer then
    return jsonb_build_object('decision','throttled','version',cur.version,'reasons','["self_trigger_throttled"]'::jsonb,
     'retry_after_seconds',greatest(1,ceil(extract(epoch from v_oldest+make_interval(secs=>(p.settings->>'self_window_seconds')::integer)-clock_timestamp()))::integer));
   end if;
  end if;
  v_goal_id:=split_part(substr(p.goal_version_ref,6),'@',1);
  select g.current_version into v_current from control.nexloop_goals g where g.tenant_id=t and g.world=p_world and g.goal_id=v_goal_id;
  select o.properties->>'goal_id' into v_goal_object from ontology.objects o where o.tenant_id=t and o.world=p_world and o.type_name='PlanStep' and o.object_id=p.recipe->>'step_id';
  select coalesce(jsonb_object_agg(o.type_name,o.nexloop_revision),'{}'::jsonb) into revisions from ontology.objects o where o.tenant_id=t and o.world=p_world
   and (o.object_id in (p.consumer_id,p.recipe->>'step_id',p.recipe->>'control_id') or (o.type_name='Goal' and o.object_id=v_goal_object));
  return jsonb_build_object('decision','reevaluate','version',cur.version,'reasons',to_jsonb(reasons),'plan',to_jsonb(p)-'tenant_id',
   'goal_version_ref','goal:'||v_goal_id||'@'||coalesce(v_current,0),'revisions',revisions,'goal_object_id',v_goal_object,
   'plan_steps',coalesce((select jsonb_agg(jsonb_build_object('step_key',x.step_key,'budget',x.budget) order by x.step_key) from runtime.nexloop_plan_steps x
    where x.tenant_id=t and x.world=p_world and x.plan_id=v_plan and x.version=cur.version),'[]'::jsonb));
 elsif c->>'verb'='mark' then
  if c->>'status' not in ('stale','invalidated','closed') or length(coalesce(c->>'reason','')) not between 1 and 500 then raise exception 'plan mark invalid' using errcode='22023';end if;
  if cur.status<>'active' or cur.version<>(c->>'version')::integer then raise exception 'plan changed' using errcode='40001';end if;
  insert into runtime.nexloop_plan_events(tenant_id,world,plan_id,version,status,reason) values(t,p_world,v_plan,cur.version,c->>'status',c->>'reason');
  return jsonb_build_object('plan_id',v_plan,'version',cur.version,'status',c->>'status');
 elsif c->>'verb'='link_run' then
  if cur.status<>'active' or cur.version<>(c->>'version')::integer then raise exception 'plan changed' using errcode='40001';end if;
  if jsonb_typeof(c->'triggers') is distinct from 'array' or jsonb_array_length(c->'triggers')>16
   or not exists(select 1 from authz.nexloop_run_credentials r join authz.nexloop_service_credentials sc on sc.token_digest=r.source_digest
     where r.run_id=(c->>'run_id')::uuid and sc.tenant_id=t and r.world=p_world) then raise exception 'plan run invalid' using errcode='22023';end if;
  insert into runtime.nexloop_plan_runs(tenant_id,world,run_id,plan_id,version,triggers) values(t,p_world,(c->>'run_id')::uuid,v_plan,cur.version,c->'triggers')
   on conflict(tenant_id,world,run_id) do nothing;
  if not exists(select 1 from runtime.nexloop_plan_runs r where r.tenant_id=t and r.world=p_world and r.run_id=(c->>'run_id')::uuid and r.plan_id=v_plan and r.version=cur.version) then
   raise exception 'run linked to another plan' using errcode='40001';end if;
  return jsonb_build_object('plan_id',v_plan,'version',cur.version,'run_id',c->>'run_id');
 end if;
 raise exception 'plan verb invalid' using errcode='22023';
end $$;

-- Run outcome (Host → guard → runtime worker; eios:action:nexloop.plan.outcome:1). Bound in SQL to the Run's live
-- activation under the caller's current task lease and to the exact stored command; only the current plan version.
create function authz.nexloop_record_plan_outcome(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-plan-outcome-v1','eios:action:nexloop.plan.outcome:1',
  array['nexloop_scheduler','nexloop_domain_worker']);
 c jsonb:=p_payload::jsonb;o jsonb:=c->'outcome';act authz.nexloop_runtime_activations%rowtype;j runtime.jobs%rowtype;b authz.nexloop_runtime_run_bindings%rowtype;
 pr runtime.nexloop_plan_runs%rowtype;cur record;p runtime.nexloop_plans%rowtype;prior runtime.nexloop_plan_outcomes%rowtype;digest text;
 v_new integer;v_strategy text;v_goal_id text;v_current integer;snap jsonb;due timestamptz;
begin
 select * into act from authz.nexloop_runtime_activations where activation_ref=c->>'activation_ref' and tenant_id=t and world=p_world;
 if not found then raise exception 'plan outcome activation unavailable' using errcode='42501';end if;
 select * into j from runtime.jobs where tenant_id=t and job_id=act.task_id for share;
 if not found or j.status<>'running' or j.lease_until<=clock_timestamp() or j.lease_credential is distinct from p_digest
  or act.lease_credential is distinct from p_digest or act.fence is distinct from j.fencing_token then
  raise exception 'plan outcome lease unavailable' using errcode='42501';end if;
 select * into b from authz.nexloop_runtime_run_bindings where run_id=act.run_id;
 if b.command_digest is distinct from encode(sha256(convert_to(c->>'command_text','UTF8')),'hex')
  or (c->>'command_text')::jsonb->>'run_id' is distinct from act.run_id::text then raise exception 'plan outcome command mismatch' using errcode='42501';end if;
 select * into pr from runtime.nexloop_plan_runs where tenant_id=t and world=p_world and run_id=act.run_id;
 if not found then raise exception 'not a plan reevaluation Run' using errcode='42501';end if;
 -- Contract run-outcome 1.0 (packages/contracts/run-outcome.schema.json), exact.
 if jsonb_typeof(o) is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(o) k) is distinct from array['evidence_refs','intent_ref','kind','plan_update','reasons','reassess_at','schema_version']
  or o->>'schema_version' is distinct from '1.0' or o->>'kind' not in ('no_action','needs_information','waiting_external','escalate','plan_update','action_intent')
  or jsonb_typeof(o->'reasons') is distinct from 'array' or jsonb_array_length(o->'reasons')>8
  or exists(select 1 from jsonb_array_elements(o->'reasons') r where jsonb_typeof(r)<>'string' or length(r#>>'{}') not between 1 and 500)
  or jsonb_typeof(o->'evidence_refs') is distinct from 'array' or jsonb_array_length(o->'evidence_refs')>32
  or exists(select 1 from jsonb_array_elements(o->'evidence_refs') r where jsonb_typeof(r)<>'string' or (r#>>'{}')!~'^[A-Za-z][A-Za-z0-9_.-]*:\S+$' or length(r#>>'{}') not between 3 and 512)
  or (jsonb_typeof(o->'reassess_at')<>'null' and (jsonb_typeof(o->'reassess_at')<>'string' or (o->>'reassess_at')!~'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|\+00:00)$'
     or (o->>'reassess_at')::timestamptz>clock_timestamp()+interval '366 days'))
  or (jsonb_typeof(o->'intent_ref')<>'null' and coalesce(o->>'intent_ref','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
  or ((o->>'kind')='plan_update')<>(jsonb_typeof(o->'plan_update')='object')
  or (jsonb_typeof(o->'plan_update')='object' and ((select array_agg(k order by k) from jsonb_object_keys(o->'plan_update') k) is distinct from array['steps','strategy']
     or not runtime.nexloop_plan_steps_valid(o->'plan_update'->'steps')
     or (jsonb_typeof(o->'plan_update'->'strategy')<>'null' and (jsonb_typeof(o->'plan_update'->'strategy')<>'string' or length(o->'plan_update'->>'strategy') not between 1 and 8192))))
  or ((o->>'kind')='action_intent' and jsonb_typeof(o->'intent_ref')<>'string')
  or ((o->>'kind')='waiting_external' and jsonb_typeof(o->'reassess_at')<>'string' and jsonb_typeof(o->'intent_ref')<>'string') then
  raise exception 'run outcome invalid' using errcode='22023';end if;
 -- An intent named by the outcome must have been submitted by this very Run.
 if jsonb_typeof(o->'intent_ref')='string' and not exists(select 1 from runtime.nexloop_effect_submissions s where s.intent_id=(o->>'intent_ref')::uuid and s.run_id=act.run_id) then
  raise exception 'outcome intent not submitted by this Run' using errcode='42501';end if;
 digest:=encode(sha256(convert_to(o::text,'UTF8')),'hex');
 select * into prior from runtime.nexloop_plan_outcomes where tenant_id=t and world=p_world and run_id=act.run_id;
 if found then
  if prior.outcome_digest<>digest then raise exception 'run outcome already recorded' using errcode='40001';end if;
  return jsonb_build_object('recorded',true,'replay',true,'plan_id',prior.plan_id,'version',prior.version,'kind',prior.kind,'new_version',prior.new_version);
 end if;
 select * into cur from runtime.nexloop_plan_current(t,p_world,pr.plan_id);
 if cur.status<>'active' or cur.version<>pr.version then raise exception 'plan version not current' using errcode='40001';end if;
 select * into p from runtime.nexloop_plans where tenant_id=t and world=p_world and plan_id=pr.plan_id and version=pr.version;
 if o->>'kind'='plan_update' then
  v_new:=p.version+1;v_goal_id:=split_part(substr(p.goal_version_ref,6),'@',1);
  select g.current_version into v_current from control.nexloop_goals g where g.tenant_id=t and g.world=p_world and g.goal_id=v_goal_id;
  snap:=runtime.nexloop_plan_snapshot(p_digest,p_world,p.consumer_id,v_goal_id);
  v_strategy:=p.strategy_ref;
  if jsonb_typeof(o->'plan_update'->'strategy')='string' then
   insert into runtime.nexloop_strategies(tenant_id,world,strategy_id,version,consumer_id,goal_version_ref,content,created_by,created_by_run)
    select t,p_world,coalesce(split_part(substr(p.strategy_ref,10),'@',1)::uuid,p.plan_id),
     coalesce((select max(x.version) from runtime.nexloop_strategies x where x.tenant_id=t and x.world=p_world and x.strategy_id=coalesce(split_part(substr(p.strategy_ref,10),'@',1)::uuid,p.plan_id)),0)+1,
     p.consumer_id,'goal:'||v_goal_id||'@'||v_current,o->'plan_update'->>'strategy','run',act.run_id
    returning 'strategy:'||strategy_id||'@'||version into v_strategy;
  end if;
  insert into runtime.nexloop_plans(tenant_id,world,plan_id,version,consumer_id,goal_version_ref,strategy_ref,context_strategy_ref,recipe,settings,control_snapshot,created_by,created_by_run)
   values(t,p_world,p.plan_id,v_new,p.consumer_id,'goal:'||v_goal_id||'@'||v_current,v_strategy,p.context_strategy_ref,
    p.recipe||jsonb_build_object('goal_chain',to_jsonb(control.nexloop_nx022_goal_chain(t,p_world,v_goal_id,v_current))),p.settings,snap,'run',act.run_id);
  perform runtime.nexloop_plan_insert_steps(t,p_world,p.plan_id,v_new,o->'plan_update'->'steps');
  insert into runtime.nexloop_plan_events(tenant_id,world,plan_id,version,status,reason,run_id) values(t,p_world,p.plan_id,p.version,'superseded','plan_update',act.run_id);
  insert into runtime.nexloop_plan_events(tenant_id,world,plan_id,version,status,reason,run_id) values(t,p_world,p.plan_id,v_new,'active','plan_update',act.run_id);
 end if;
 insert into runtime.nexloop_plan_outcomes(tenant_id,world,run_id,plan_id,version,kind,outcome,outcome_digest,intent_ref,new_version)
  values(t,p_world,act.run_id,p.plan_id,p.version,o->>'kind',o,digest,(o->>'intent_ref')::uuid,v_new);
 if v_new is not null then perform runtime.nexloop_plan_mark_due(t,p_world,p.plan_id,v_new,'self');
 elsif jsonb_typeof(o->'reassess_at')='string' then
  due:=greatest((o->>'reassess_at')::timestamptz,clock_timestamp()+make_interval(secs=>(p.settings->>'min_reassess_seconds')::integer));
  perform authz.nexloop_plan_feed_touch(t,p_world,p.plan_id,jsonb_build_object('kind','reassess_due','cause','self','due',due,'run_id',act.run_id,'at',clock_timestamp()),due);
 end if;
 return jsonb_build_object('recorded',true,'replay',false,'plan_id',p.plan_id,'version',p.version,'kind',o->>'kind','new_version',v_new);
end $$;

-- 0093 feed port unchanged except that it also serves 'plan-reevaluate' (its own Action authority per feed).
create or replace function authz.nexloop_work_feed(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_tenant text:=a->>'tenant_id';v_feed text:=c->>'feed';v_verb text:=c->>'verb';
 v_result jsonb;v_limit integer;v_lease integer;v_delay integer;v_max integer;r runtime.nexloop_work_feed%rowtype;ident jsonb;
begin
 if session_user<>'nexloop_domain_worker' or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>65536
  or v_feed is null or v_feed not in ('recall-instance','claim-match','plan-reevaluate') or v_verb is null or v_verb not in ('claim','complete','retry','backlog')
  or a->>'protocol' is distinct from 'nexloop-work-feed-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from 'eios:action:NexLoop.feed.'||v_feed||':1' or a->>'resource_id' is distinct from a->>'action_resource'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'work feed unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-work-feed-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'work feed unavailable' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_browser_token_realms where token_digest=p_digest)
  or not exists(select 1 from authz.nexloop_service_credentials t where t.token_digest=p_digest and t.tenant_id=v_tenant
     and t.binding->>'subject_kind'='service' and t.status='active') then
  raise exception 'work feed unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb then raise exception 'work feed unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',v_tenant,true);
 if v_verb='claim' then
  v_limit:=(c->>'limit')::integer;v_lease:=(c->>'lease_seconds')::integer;
  if v_limit is null or v_limit not between 1 and 50 or v_lease is null or v_lease not between 1 and 600 then raise exception 'work feed input invalid' using errcode='22023';end if;
  with due as (
   select f.item_key from runtime.nexloop_work_feed f where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='pending'
    and f.available_at<=clock_timestamp() and (f.lease_until is null or f.lease_until<=clock_timestamp())
   order by f.available_at,f.changed_at,f.item_key limit v_limit for update skip locked),
  leased as (
   update runtime.nexloop_work_feed f set lease_until=clock_timestamp()+make_interval(secs=>v_lease),lease_seq=f.change_seq,attempts=f.attempts+1
   from due where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.item_key=due.item_key
   returning f.item_key,f.payload,f.change_seq,f.attempts,f.available_at)
  select coalesce(jsonb_agg(jsonb_build_object('item_key',l.item_key,'payload',l.payload,'fence',l.change_seq,'attempts',l.attempts)
   order by l.available_at,l.item_key),'[]'::jsonb) into v_result from leased l;
 elsif v_verb in ('complete','retry') then
  select * into r from runtime.nexloop_work_feed where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=c->>'item_key' for update;
  -- A lost or replaced lease never completes or retries someone else's work.
  if not found or r.lease_seq is distinct from (c->>'fence')::bigint or r.lease_until<=clock_timestamp() then
   v_result:=jsonb_build_object('status','lease_lost');
  elsif v_verb='complete' then
   if r.change_seq=r.lease_seq then
    delete from runtime.nexloop_work_feed where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=r.item_key;
    v_result:=jsonb_build_object('status','completed');
   else
    -- A newer change arrived while working: keep it pending (it is processed again from scratch).
    update runtime.nexloop_work_feed set lease_until=null,lease_seq=null,attempts=0
     where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=r.item_key;
    v_result:=jsonb_build_object('status','changed');
   end if;
  else
   v_delay:=(c->>'delay_seconds')::integer;v_max:=(c->>'max_attempts')::integer;
   if v_delay is null or v_delay not between 0 and 3600 or v_max is null or v_max not between 1 and 20 or coalesce(c->>'code','')!~'^[a-z0-9_]{1,64}$' then
    raise exception 'work feed input invalid' using errcode='22023';end if;
   update runtime.nexloop_work_feed set lease_until=null,lease_seq=null,last_code=c->>'code',
    status=case when r.change_seq=r.lease_seq and r.attempts>=v_max then 'dead_lettered' else 'pending' end,
    available_at=case when r.change_seq=r.lease_seq then clock_timestamp()+make_interval(secs=>v_delay) else clock_timestamp() end,
    attempts=case when r.change_seq=r.lease_seq then r.attempts else 0 end
   where tenant_id=v_tenant and world=p_world and feed=v_feed and item_key=r.item_key
   returning jsonb_build_object('status',status) into v_result;
  end if;
 else
  select jsonb_build_object(
   'pending',(select count(*) from runtime.nexloop_work_feed f where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='pending'),
   'oldest_pending_seconds',(select floor(extract(epoch from clock_timestamp()-min(f.changed_at)))::bigint from runtime.nexloop_work_feed f
     where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='pending'),
   'dead_lettered',(select coalesce(jsonb_agg(jsonb_build_object('item_key',x.item_key,'attempts',x.attempts,'code',x.last_code) order by x.changed_at desc),'[]'::jsonb)
     from (select * from runtime.nexloop_work_feed f where f.tenant_id=v_tenant and f.world=p_world and f.feed=v_feed and f.status='dead_lettered'
      order by f.changed_at desc limit 20) x)) into v_result;
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return v_result;
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['runtime.nexloop_plan_current(text,text,uuid)','authz.nexloop_plan_feed_touch(text,text,uuid,jsonb,timestamptz)',
  'runtime.nexloop_active_plans(text,text)','runtime.nexloop_plan_on_control_event()','runtime.nexloop_plan_on_job_failed()','runtime.nexloop_plan_on_strategy()',
  'runtime.nexloop_plan_on_observation()','runtime.nexloop_plan_commitment_due(text,text,uuid,text,timestamptz)','runtime.nexloop_plan_mark_due(text,text,uuid,integer,text)',
  'runtime.nexloop_plan_steps_valid(jsonb)','runtime.nexloop_plan_insert_steps(text,text,uuid,integer,jsonb)','runtime.nexloop_plan_snapshot(text,text,text,text)',
  'authz.nexloop_plan_port_tenant(text,text,text,text,text,text,text,text[])','authz.nexloop_plan_command(text,text,text,text,text)',
  'authz.nexloop_record_plan_outcome(text,text,text,text,text)','authz.nexloop_work_feed(text,text,text,text,text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
grant execute on function authz.nexloop_work_feed(text,text,text,text,text),authz.nexloop_plan_command(text,text,text,text,text) to nexloop_domain_worker;
grant execute on function authz.nexloop_record_plan_outcome(text,text,text,text,text) to nexloop_scheduler,nexloop_domain_worker;
