-- NX-022 close-out (temporary number): persist an owner-control snapshot with every
-- effect submission and every runtime task, and assert it at dispatch time.
-- Snapshots are derived server-side from stored rows (never from caller payload):
--   * effect submission: consumer, action type, the submitting Run's Role (if any),
--     the NX-022 goal versions bound to that Run (bind_run) and the control head;
--   * runtime task: the control head at enqueue (scopes/goal come from the stored
--     Run command at dispatch).
-- Dispatch reuses authz.nexloop_assert_dispatch_controls (0068): paused scope, stale
-- goal chain, stale object or a later relevant control event all deny. Published
-- 0001..0091 are unchanged; only new tables, triggers and functions are added.

create table runtime.nexloop_effect_dispatch_controls (
 intent_id uuid not null references runtime.nexloop_effect_intents(intent_id),run_id uuid not null,
 tenant_id text not null,world text not null,snapshot jsonb not null check(jsonb_typeof(snapshot)='object'),
 captured_at timestamptz not null default clock_timestamp(),
 primary key(intent_id,run_id));
create index nexloop_effect_dispatch_controls_latest on runtime.nexloop_effect_dispatch_controls(intent_id,captured_at desc);

create table runtime.nexloop_task_dispatch_controls (
 job_id text primary key,tenant_id text not null,world text not null,control_revision bigint not null check(control_revision>=0),
 captured_at timestamptz not null default clock_timestamp());

do $$
declare t text;
begin
 foreach t in array array['nexloop_effect_dispatch_controls','nexloop_task_dispatch_controls'] loop
  execute format('alter table runtime.%I owner to nexloop_owner',t);
  execute format('alter table runtime.%I enable row level security',t);
  execute format('alter table runtime.%I force row level security',t);
  execute format('create policy nx022_dispatch_tenant on runtime.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on runtime.%I from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime',t);
  execute format('create trigger nx022_append_only before update or delete on runtime.%I for each row execute function control.nexloop_nx022_append_only()',t);
 end loop;
end $$;

-- Effect submission snapshot (one per submitting Run; the latest one is asserted, so
-- a re-evaluation by a new Run after resume re-validates the same business intent).
create function runtime.nexloop_nx022_capture_effect_controls() returns trigger
language plpgsql set search_path=pg_catalog as $$
declare i runtime.nexloop_effect_intents%rowtype;v_rev bigint;v_role text;v_scopes jsonb;v_goals jsonb;
begin
 select * into i from runtime.nexloop_effect_intents where intent_id=new.intent_id;
 if not found then return null;end if;
 perform set_config('eios.tenant_id',i.tenant_id,true);
 select revision into v_rev from control.nexloop_control_heads where tenant_id=i.tenant_id and world=i.world;
 select role_ref into v_role from authz.nexloop_role_run_bindings where run_id=new.run_id and tenant_id=i.tenant_id;
 v_scopes:=jsonb_build_array(jsonb_build_object('kind','consumer','ref',i.consumer_id),jsonb_build_object('kind','action_type','ref',i.action_name));
 if v_role ~ '^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$' then v_scopes:=v_scopes||jsonb_build_array(jsonb_build_object('kind','role','ref',v_role));end if;
 select coalesce(jsonb_agg(jsonb_build_object('goal_id',b.goal_id,'version',b.goal_version) order by b.goal_id),'[]'::jsonb) into v_goals
  from control.nexloop_run_goal_bindings b where b.tenant_id=i.tenant_id and b.world=i.world and b.run_id=new.run_id;
 insert into runtime.nexloop_effect_dispatch_controls(intent_id,run_id,tenant_id,world,snapshot)
  values(i.intent_id,new.run_id,i.tenant_id,i.world,jsonb_build_object('control_revision',coalesce(v_rev,0),'scopes',v_scopes,
   'goals',v_goals,'objects','[]'::jsonb,'budgets','[]'::jsonb))
  on conflict do nothing;
 return null;
end $$;
alter function runtime.nexloop_nx022_capture_effect_controls() owner to nexloop_owner;
revoke all on function runtime.nexloop_nx022_capture_effect_controls() from public;
create trigger nx022_capture_effect_controls after insert on runtime.nexloop_effect_submissions
 for each row execute function runtime.nexloop_nx022_capture_effect_controls();

create function runtime.nexloop_nx022_capture_task_controls() returns trigger
language plpgsql set search_path=pg_catalog as $$
declare v_rev bigint;
begin
 if new.capability_name is distinct from 'NexLoop.event' or jsonb_typeof(new.normalized_input->'run_command') is distinct from 'object' then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select revision into v_rev from control.nexloop_control_heads where tenant_id=new.tenant_id and world=new.world;
 insert into runtime.nexloop_task_dispatch_controls(job_id,tenant_id,world,control_revision)
  values(new.job_id,new.tenant_id,new.world,coalesce(v_rev,0)) on conflict do nothing;
 return null;
end $$;
alter function runtime.nexloop_nx022_capture_task_controls() owner to nexloop_owner;
revoke all on function runtime.nexloop_nx022_capture_task_controls() from public;
create trigger nx022_capture_task_controls after insert on runtime.jobs
 for each row execute function runtime.nexloop_nx022_capture_task_controls();

-- Effect admission check: the latest submission snapshot of this intent.
create function authz.nexloop_assert_intent_dispatch_controls(p_digest text,p_world text,p_intent uuid) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;v_snapshot jsonb;
begin
 ident:=control.nexloop_nx022_identity(p_digest,p_world,true);
 select c.snapshot into v_snapshot from runtime.nexloop_effect_dispatch_controls c
  where c.intent_id=p_intent and c.tenant_id=ident->'binding'->>'tenant_id' and c.world=p_world order by c.captured_at desc,c.run_id limit 1;
 if not found then raise exception 'control snapshot missing' using errcode='NXC02';end if;
 return authz.nexloop_assert_dispatch_controls(p_digest,p_world,v_snapshot);
end $$;
alter function authz.nexloop_assert_intent_dispatch_controls(text,text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_intent_dispatch_controls(text,text,uuid) from public;
grant execute on function authz.nexloop_assert_intent_dispatch_controls(text,text,uuid) to nexloop_action_worker,nexloop_domain_worker,nexloop_api;

-- Runtime task check: control head captured at enqueue + scopes/goal from the stored
-- Run command (consumer_ref, role_ref, goal_version_ref in the NX-022 form goal:<id>@<v>;
-- other goal refs are not NX-022 managed goals and add no goal check).
create function authz.nexloop_assert_task_dispatch_controls(p_digest text,p_world text,p_task text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;v_tenant text;t runtime.nexloop_task_dispatch_controls%rowtype;j runtime.jobs%rowtype;cmd jsonb;
 v_scopes jsonb:='[]'::jsonb;v_goals jsonb:='[]'::jsonb;m text[];
begin
 ident:=control.nexloop_nx022_identity(p_digest,p_world,true);v_tenant:=ident->'binding'->>'tenant_id';
 select * into t from runtime.nexloop_task_dispatch_controls where job_id=p_task and tenant_id=v_tenant and world=p_world;
 if not found then raise exception 'control snapshot missing' using errcode='NXC02';end if;
 select * into j from runtime.jobs where job_id=p_task and tenant_id=v_tenant and world=p_world;
 cmd:=j.normalized_input->'run_command';
 if cmd->>'consumer_ref' like 'consumer:%' then
  v_scopes:=v_scopes||jsonb_build_array(jsonb_build_object('kind','consumer','ref',substr(cmd->>'consumer_ref',10)));end if;
 if cmd->>'role_ref' ~ '^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$' then
  v_scopes:=v_scopes||jsonb_build_array(jsonb_build_object('kind','role','ref',cmd->>'role_ref'));end if;
 m:=regexp_match(coalesce(cmd->>'goal_version_ref',''),'^goal:([a-z0-9][a-z0-9._-]{0,127})@([1-9][0-9]{0,8})$');
 if m is not null then v_goals:=jsonb_build_array(jsonb_build_object('goal_id',m[1],'version',m[2]::integer));end if;
 return authz.nexloop_assert_dispatch_controls(p_digest,p_world,jsonb_build_object('control_revision',t.control_revision,
  'scopes',v_scopes,'goals',v_goals,'objects','[]'::jsonb,'budgets',jsonb_build_array('model')));
end $$;
alter function authz.nexloop_assert_task_dispatch_controls(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_task_dispatch_controls(text,text,text) from public;
grant execute on function authz.nexloop_assert_task_dispatch_controls(text,text,text) to nexloop_scheduler,nexloop_domain_worker,nexloop_api;

-- The runtime worker (scheduler role) reserves the Run's declared model budget at dispatch.
grant execute on function authz.nexloop_reserve_budget(text,text,text,text,text,text,text) to nexloop_scheduler;
