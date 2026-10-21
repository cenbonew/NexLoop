-- NX-041 stage finding 2026-10-22 (AT-046 first stage run): the simulation runtime worker failed every Run at its control stage
-- (runtime_control_unavailable, InsufficientPrivilege). Before a queued Run is activated the worker re-reads the owner controls
-- (nexloop_eios.goal_controls.ControlPlane.prepare_run_dispatch: goal binding, the task's control snapshot, the model budget and its
-- reservation) - entries the real workers hold (nexloop_domain_worker) and S6c never granted to nexloop_sim_worker. The local D10 test
-- called claim and activation directly and so never ran that stage (tests/test_nx041_sim_pi_run_pg.py now runs the worker's own path).
-- The migration number is temporary; it lives only in the file name and the catalog.
-- Least privilege: execute on these four entries only, to the simulation worker only (not its guard, not the intake); no role
-- membership. Each entry authenticates the caller's service credential for the world it names (the world's own worker credential:
-- D10) and works on that world's rows only.
-- Second defence (L6 batch-12 review 2.1, as 0275 for the D10 entries): each of the four refuses a simulation role called for a real
-- or test world - authz.nexloop_app_role(p_world) names it 'nexloop_sim_outside_its_world' there - even with a real credential's
-- digest in hand. A real role's app_role is its own session_user, so the real workers' path is unchanged. The bodies are the latest
-- ones (0097, 0068) with that one line added; definer search_path gains pg_temp (the definer check). Registered in the copy guard.
grant execute on function authz.nexloop_assert_task_dispatch_controls(text,text,text) to nexloop_sim_worker;
grant execute on function authz.nexloop_bind_run_goal(text,text,uuid,text,integer) to nexloop_sim_worker;
grant execute on function authz.nexloop_budget_status(text,text,text) to nexloop_sim_worker;
grant execute on function authz.nexloop_reserve_budget(text,text,text,text,text,text,text) to nexloop_sim_worker;

create or replace function authz.nexloop_assert_task_dispatch_controls(p_digest text,p_world text,p_task text) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;t runtime.nexloop_task_dispatch_controls%rowtype;j runtime.jobs%rowtype;cmd jsonb;
 v_scopes jsonb:='[]'::jsonb;v_goals jsonb:='[]'::jsonb;m text[];
begin
 -- L6 batch-12 review 2.1 (S6c second defence, as 0275): a simulation role never runs this outside its own simulation world.
 if authz.nexloop_app_role(p_world)='nexloop_sim_outside_its_world' then raise exception 'simulation role outside its world' using errcode='42501';end if;
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

create or replace function authz.nexloop_bind_run_goal(p_digest text,p_world text,p_run uuid,p_goal text,p_version integer) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;v_rev bigint;old control.nexloop_run_goal_bindings%rowtype;
begin
 -- L6 batch-12 review 2.1 (S6c second defence, as 0275): a simulation role never runs this outside its own simulation world.
 if authz.nexloop_app_role(p_world)='nexloop_sim_outside_its_world' then raise exception 'simulation role outside its world' using errcode='42501';end if;
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

create or replace function authz.nexloop_budget_status(p_digest text,p_world text,p_kind text) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;lim control.nexloop_budget_limits%rowtype;v_used numeric;v_count bigint;
begin
 -- L6 batch-12 review 2.1 (S6c second defence, as 0275): a simulation role never runs this outside its own simulation world.
 if authz.nexloop_app_role(p_world)='nexloop_sim_outside_its_world' then raise exception 'simulation role outside its world' using errcode='42501';end if;
 ident:=control.nexloop_nx022_identity(p_digest,p_world,false);v_tenant:=ident->'binding'->>'tenant_id';
 select * into lim from control.nexloop_budget_limits where tenant_id=v_tenant and world=p_world and budget_kind=p_kind;
 if not found then return jsonb_build_object('budget_kind',p_kind,'configured',false,'exhausted',true);end if;
 select coalesce(sum(amount),0),count(*) into v_used,v_count from control.nexloop_budget_consumption where tenant_id=v_tenant and world=p_world and budget_kind=p_kind
  and recorded_at>=lim.period_start and recorded_at<lim.period_end;
 return jsonb_build_object('budget_kind',p_kind,'configured',true,'unit',lim.unit,'limit_amount',lim.limit_amount::text,'used',v_used::text,
  'consumption_count',v_count,'exhausted',v_used>=lim.limit_amount,'limit_revision',lim.revision);
end $$;

create or replace function authz.nexloop_reserve_budget(p_digest text,p_world text,p_kind text,p_consumption_id text,p_amount text,p_unit text,p_source_ref text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;lim control.nexloop_budget_limits%rowtype;old control.nexloop_budget_consumption%rowtype;v_amount numeric;v_used numeric;
begin
 -- L6 batch-12 review 2.1 (S6c second defence, as 0275): a simulation role never runs this outside its own simulation world.
 if authz.nexloop_app_role(p_world)='nexloop_sim_outside_its_world' then raise exception 'simulation role outside its world' using errcode='42501';end if;
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
