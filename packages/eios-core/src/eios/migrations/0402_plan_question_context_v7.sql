-- K4h K1 (L6, before any stage enablement): the database's own guard for run-outcome 1.1 questions. The Host offers a question only to
-- a plan Run whose pack declares context-pack v7 (plan_question_tool, default off); this makes the outcome port refuse a question from
-- any other Run as well, so no question is ever opened whose answer could not reach the next plan Run.
-- 1. runtime.nexloop_run_context_v7(tenant, world, run): the Run has a bound context pack and every pack bound to it is
--    nexloop.context-pack.v7 (runtime.nexloop_context_packs, written at bind by the v6/v7 core of 0398).
-- 2. authz.nexloop_record_plan_outcome (0397 body, one edit registered with the copy guard): a 1.1 body with a question from a Run
--    that is not v7 is refused like any other invalid outcome (22023, which the guard answers invalid_run_outcome): the model can
--    correct it by recording without the question, and no question row is written. A 1.1 body without a question and 1.0 are unchanged.
-- Temporary number; the dispatcher renumbers.
create function runtime.nexloop_run_context_v7(p_tenant text,p_world text,p_run uuid) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select coalesce(bool_and(p.protocol='nexloop.context-pack.v7'),false) from runtime.nexloop_context_packs p
  where p.tenant_id=p_tenant and p.world=p_world and p.run_id=p_run
$$;
alter function runtime.nexloop_run_context_v7(text,text,uuid) owner to nexloop_owner;
revoke all on function runtime.nexloop_run_context_v7(text,text,uuid) from public;

create or replace function authz.nexloop_record_plan_outcome(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
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
 -- Contract run-outcome 1.0 or 1.1 (packages/contracts/run-outcome.schema.json), exact; 1.1 may carry one question (K4b).
 if jsonb_typeof(o) is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(o-'question') k) is distinct from array['evidence_refs','intent_ref','kind','plan_update','reasons','reassess_at','schema_version']
  or coalesce(o->>'schema_version','') not in ('1.0','1.1') or (o ? 'question' and (not runtime.nexloop_run_question_valid(o) or not runtime.nexloop_run_context_v7(t,p_world,act.run_id))) or o->>'kind' not in ('no_action','needs_information','waiting_external','escalate','plan_update','action_intent')
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
 -- NX-025: a goal in the plan's chain published a new version after this Run was linked: the Run reasoned on a goal
 -- that is no longer current; its outcome is refused (the goal_version_stale trigger already queued a fresh reevaluation).
 if exists(select 1 from runtime.nexloop_plans p2 cross join lateral jsonb_array_elements_text(p2.recipe->'goal_chain') g(goal_id)
   join control.nexloop_goal_versions gv on gv.tenant_id=p2.tenant_id and gv.world=p2.world and gv.goal_id=g.goal_id
   where p2.tenant_id=t and p2.world=p_world and p2.plan_id=pr.plan_id and p2.version=pr.version and gv.published_at>pr.created_at) then
  raise exception 'goal changed since the Run was linked' using errcode='40001';end if;
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
 if o ? 'question' then perform runtime.nexloop_human_question_open(t,p_world,act.run_id,p.plan_id,p.version,o->'question');end if;
 if v_new is not null then perform runtime.nexloop_plan_mark_due(t,p_world,p.plan_id,v_new,'self');
 elsif jsonb_typeof(o->'reassess_at')='string' then
  due:=greatest((o->>'reassess_at')::timestamptz,clock_timestamp()+make_interval(secs=>(p.settings->>'min_reassess_seconds')::integer));
  perform authz.nexloop_plan_feed_touch(t,p_world,p.plan_id,jsonb_build_object('kind','reassess_due','cause','self','due',due,'run_id',act.run_id,'at',clock_timestamp()),due);
 end if;
 return jsonb_build_object('recorded',true,'replay',false,'plan_id',p.plan_id,'version',p.version,'kind',o->>'kind','new_version',v_new);
end $$;
