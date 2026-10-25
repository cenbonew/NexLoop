-- NX-057 R2 (docs/design/remediation-intents.md v4 §4.3, §6.2, §9.1-§9.3, §13 R2; ADR-040 draft): reevaluate remediations wake the plan
-- that waits for the failed intent; nothing here creates an effect, an intent or a business object.
-- 1. Rules: kind reevaluate opens (governed_action stays refused until R3); the limits gain plan_wakes_per_week (design §9.2, 2 by
--    default) and breaker_per_day (design §9.3, 3).
-- 2. runtime.nexloop_remediation_wake (definer, the only waker): under a row lock it re-reads the switches (design §9.4), finds the
--    active plan whose current version names the failed intent (as the 0106 observation trigger does), applies the plan cap (wakes of
--    that plan in 7 days, counted from the remediation table, never from the work feed or its 16-trigger list), and touches the plan's
--    feed with {kind final_failure, cause self} (NX-024 then prechecks and may start one bounded reevaluation Run; its self-trigger
--    throttle only delays, J-2). Outcomes: succeeded (woken), escalated (chain_capped), not_applicable (no_plan), or left proposed while
--    a switch is closed.
-- 3. Origin (design §9.1, by the remediation table, L6 K-2): a Run started for a plan consumes every woken, unconsumed remediation of
--    that plan (trigger on runtime.nexloop_plan_runs); an intent whose Run (origin_run_id, else the earliest submission's Run) consumed a
--    wake is a remediation-origin intent, and its failure only escalates (code remediation_origin). Nothing in a Run's tool input sets it.
-- 4. Breaker (design §9.3): a rule with breaker_per_day failures of remediation-origin intents, or as many chain_capped escalation events,
--    in 24 hours (counted from the owner's last release of that rule, if later) is held: it generates nothing until the owner releases it (remediation.rule_release, human owner only).
-- 5. The planner, the rules publication and the owner's resolve handler are re-created from remediation_intents with only these
--    changes (tests/support/remediation_r2_edits.py, registered in the copy guard); resolve gains outcome wake for a manual reevaluate.
-- 6. The metrics snapshot and the workbench list are wrapped once more: held rules, wakes of the last 24 hours, the plan of a remediation.

-- 1. New columns, the rule holds, the scan decision rule_held ----------------------------------------------------------------------
alter table runtime.nexloop_remediation_intents add column plan_id uuid,add column woken_at timestamptz,add column consumed_by_run uuid,
 add column origin_remediation_id uuid references runtime.nexloop_remediation_intents(remediation_id);
create index nexloop_remediation_intents_plan on runtime.nexloop_remediation_intents(tenant_id,world,plan_id,woken_at) where plan_id is not null;
create index nexloop_remediation_intents_wakes on runtime.nexloop_remediation_intents(tenant_id,world,plan_id) where state='succeeded' and consumed_by_run is null;
create index nexloop_remediation_intents_consumed on runtime.nexloop_remediation_intents(consumed_by_run) where consumed_by_run is not null;
alter table runtime.nexloop_remediation_scans drop constraint nexloop_remediation_scans_decision_check;
alter table runtime.nexloop_remediation_scans add constraint nexloop_remediation_scans_decision_check
 check(decision in ('generated','no_rule','not_eligible','not_real','not_failed','bound_reply','rate_capped','switch_closed','rule_held'));
create table control.nexloop_remediation_rule_holds (
 hold_id uuid primary key default gen_random_uuid(),tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,
 rule_id text not null check(rule_id~'^[a-z][a-z0-9_]{0,63}$'),reason_code text not null check(reason_code in ('origin_failures','chain_capped')),
 observed integer not null check(observed>=1),held_at timestamptz not null default clock_timestamp(),
 released_at timestamptz,released_by text,release_reason text check(release_reason is null or length(release_reason) between 1 and 500),
 check((released_at is null)=(released_by is null) and (released_at is null)=(release_reason is null))
);
create unique index nexloop_remediation_rule_holds_active on control.nexloop_remediation_rule_holds(tenant_id,world,rule_id) where released_at is null;
alter table control.nexloop_remediation_rule_holds owner to nexloop_owner;
alter table control.nexloop_remediation_rule_holds enable row level security;
alter table control.nexloop_remediation_rule_holds force row level security;
create policy remediation_tenant on control.nexloop_remediation_rule_holds to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_remediation_rule_holds from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,
 nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker;

-- 2. Switches, origin, held rules ---------------------------------------------------------------------------------------------------
-- Which switch is closed now (null when all are open); read again before each state change (design §9.4). The deployment layer is the
-- planner process's own REMEDIATION_ENABLED: it gates the service's moves, not the owner's own governed wake (p_deployment false).
create function runtime.nexloop_remediation_closed(p_tenant text,p_world text,p_deployment boolean) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 v:=case when p_deployment and coalesce(current_setting('nexloop.remediation_enabled',true),'') is distinct from 'true' then 'deployment'
  when runtime.nexloop_remediation_paused(p_tenant,p_world) then 'tenant'
  when exists(select 1 from control.nexloop_control_scopes s where s.tenant_id=p_tenant and s.world=p_world and s.paused and s.scope_kind='tenant' and s.scope_ref='*')
   then 'control' end;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_remediation_closed(text,text,boolean) owner to nexloop_owner;
revoke all on function runtime.nexloop_remediation_closed(text,text,boolean) from public;

-- The remediation whose wake the intent's Run consumed: the intent's recorded origin Run, else its earliest submission's Run (the Run
-- the effect claim uses, 0042). Null for an intent of any other Run.
create function runtime.nexloop_remediation_origin(p_tenant text,p_world text,p_intent uuid) returns uuid
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_run uuid;v uuid;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 v_run:=coalesce((select i.origin_run_id from runtime.nexloop_effect_intents i where i.intent_id=p_intent and i.tenant_id=p_tenant and i.world=p_world),
  (select s.run_id from runtime.nexloop_effect_submissions s where s.intent_id=p_intent order by s.created_at,s.run_id limit 1));
 if v_run is not null then
  select q.remediation_id into v from runtime.nexloop_remediation_intents q where q.tenant_id=p_tenant and q.world=p_world and q.consumed_by_run=v_run
   order by q.created_at,q.remediation_id limit 1;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_remediation_origin(text,text,uuid) owner to nexloop_owner;
revoke all on function runtime.nexloop_remediation_origin(text,text,uuid) from public;

create function runtime.nexloop_remediation_rule_held(p_tenant text,p_world text,p_rule text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 v:=exists(select 1 from control.nexloop_remediation_rule_holds h where h.tenant_id=p_tenant and h.world=p_world and h.rule_id=p_rule and h.released_at is null);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_remediation_rule_held(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_remediation_rule_held(text,text,text) from public;

-- 3. The wake ---------------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_remediation_wake(p_tenant text,p_world text,p_id uuid,p_actor_kind text,p_actor text,p_reason text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r runtime.nexloop_remediation_intents;v_closed text;v_plan uuid;v_limit integer;v_wakes integer;
 v_result jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from runtime.nexloop_remediation_intents where remediation_id=p_id and tenant_id=p_tenant and world=p_world for update;
 if not found or r.state<>'proposed' or r.kind<>'reevaluate' then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return jsonb_build_object('remediation_id',p_id,'state',r.state);end if;
 v_closed:=runtime.nexloop_remediation_closed(p_tenant,p_world,p_actor_kind='service');
 if v_closed is not null then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return jsonb_build_object('remediation_id',p_id,'state','proposed','closed',v_closed);end if;
 select p.plan_id into v_plan from runtime.nexloop_active_plans(p_tenant,p_world) p
  where exists(select 1 from runtime.nexloop_plan_steps s where s.tenant_id=p.tenant_id and s.world=p.world and s.plan_id=p.plan_id and s.version=p.version
    and s.intent_ref=r.source_intent_id)
   or exists(select 1 from runtime.nexloop_plan_outcomes o where o.tenant_id=p.tenant_id and o.world=p.world and o.plan_id=p.plan_id and o.intent_ref=r.source_intent_id)
  order by p.plan_id limit 1;
 if v_plan is null then
  perform runtime.nexloop_remediation_move(p_id,'not_applicable','no_plan',p_actor_kind,p_actor,p_reason);
  v_result:=jsonb_build_object('remediation_id',p_id,'state','not_applicable','code','no_plan');
 else
  select coalesce((m.manifest->'limits'->>'plan_wakes_per_week')::integer,2) into v_limit from control.nexloop_remediation_rules m
   where m.tenant_id=p_tenant and m.world=p_world and m.manifest_version=r.rule_version;
  perform pg_advisory_xact_lock(hashtext('nexloop-remediation-wake:'||p_tenant||':'||p_world||':'||v_plan));
  select count(*) into v_wakes from runtime.nexloop_remediation_intents q where q.tenant_id=p_tenant and q.world=p_world and q.plan_id=v_plan
   and q.woken_at>clock_timestamp()-interval '7 days';
  update runtime.nexloop_remediation_intents set plan_id=v_plan where remediation_id=p_id;
  if v_wakes>=coalesce(v_limit,2) then
   perform runtime.nexloop_remediation_move(p_id,'escalated','chain_capped',p_actor_kind,p_actor,p_reason);
   v_result:=jsonb_build_object('remediation_id',p_id,'state','escalated','code','chain_capped','plan_id',v_plan);
  else
   perform authz.nexloop_plan_feed_touch(p_tenant,p_world,v_plan,jsonb_build_object('kind','final_failure','cause','self','intent_id',r.source_intent_id,
    'failure_kind',r.failure_kind,'failure_id',r.failure_id,'remediation_id',r.remediation_id,'at',clock_timestamp()),null);
   perform set_config('eios.tenant_id',p_tenant,true);
   update runtime.nexloop_remediation_intents set woken_at=clock_timestamp() where remediation_id=p_id;
   perform runtime.nexloop_remediation_move(p_id,'succeeded','woken',p_actor_kind,p_actor,p_reason);
   v_result:=jsonb_build_object('remediation_id',p_id,'state','succeeded','code','woken','plan_id',v_plan);
  end if;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v_result;
end $$;
alter function runtime.nexloop_remediation_wake(text,text,uuid,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_remediation_wake(text,text,uuid,text,text,text) from public;

-- 4. Origin by consumption: a Run started for a plan consumes the plan's woken, unconsumed remediations (design §9.1, K-2).
create function runtime.nexloop_remediation_consume_wakes() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 update runtime.nexloop_remediation_intents q set consumed_by_run=new.run_id,updated_at=clock_timestamp()
  where q.tenant_id=new.tenant_id and q.world=new.world and q.plan_id=new.plan_id and q.state='succeeded' and q.kind='reevaluate' and q.consumed_by_run is null;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_remediation_consume_wakes() owner to nexloop_owner;
revoke all on function runtime.nexloop_remediation_consume_wakes() from public;
create trigger nx057_remediation_consume_wakes after insert on runtime.nexloop_plan_runs for each row execute function runtime.nexloop_remediation_consume_wakes();

-- 5. The breaker: holds a rule past breaker_per_day in 24 hours; returns the rules newly held this round.
create function runtime.nexloop_remediation_breaker(p_tenant text,p_world text) returns text[]
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);m control.nexloop_remediation_rules;r jsonb;v_limit integer;v_origin integer;v_capped integer;
 v_held text[]:='{}';v_since timestamptz;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into m from control.nexloop_remediation_rules q where q.tenant_id=p_tenant and q.world=p_world order by q.manifest_version desc limit 1;
 v_limit:=coalesce((m.manifest->'limits'->>'breaker_per_day')::integer,3);
 for r in select value from jsonb_array_elements(coalesce(m.manifest->'rules','[]'::jsonb)) loop
  continue when exists(select 1 from control.nexloop_remediation_rule_holds h where h.tenant_id=p_tenant and h.world=p_world and h.rule_id=r->>'rule_id'
   and h.released_at is null);
  -- The window: the last 24 hours, but never before the owner's last release of this rule (what was counted for a hold the owner
  -- released does not hold the rule again, L6 R2 review).
  v_since:=greatest(clock_timestamp()-interval '24 hours',coalesce((select max(h.released_at) from control.nexloop_remediation_rule_holds h
   where h.tenant_id=p_tenant and h.world=p_world and h.rule_id=r->>'rule_id'),'-infinity'));
  -- Failures of intents of Runs that this rule's wakes started, and this rule's capped escalations by their event (design §9.3).
  select count(*) into v_origin from runtime.nexloop_remediation_intents q join runtime.nexloop_remediation_intents o on o.remediation_id=q.origin_remediation_id
   where q.tenant_id=p_tenant and q.world=p_world and o.rule_id=r->>'rule_id' and q.created_at>v_since;
  select count(*) into v_capped from runtime.nexloop_remediation_events e join runtime.nexloop_remediation_intents q on q.remediation_id=e.remediation_id
   where e.tenant_id=p_tenant and e.world=p_world and q.rule_id=r->>'rule_id' and e.code='chain_capped' and e.recorded_at>v_since;
  if v_origin>=v_limit or v_capped>=v_limit then
   insert into control.nexloop_remediation_rule_holds(tenant_id,world,rule_id,reason_code,observed)
    values(p_tenant,p_world,r->>'rule_id',case when v_origin>=v_limit then 'origin_failures' else 'chain_capped' end,greatest(v_origin,v_capped));
   v_held:=v_held||(r->>'rule_id');
  end if;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v_held;
end $$;
alter function runtime.nexloop_remediation_breaker(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_remediation_breaker(text,text) from public;

-- remediation.rule_release: the owner releases a held rule, with a reason (human owner only, governed entry).
create function runtime.nexloop_governed_release_remediation_rule(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare h control.nexloop_remediation_rule_holds;
begin
 if body->>'operation' is distinct from 'release_remediation_rule' or coalesce(body->>'rule_id','')!~'^[a-z][a-z0-9_]{0,63}$'
  or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'remediation: invalid' using errcode='22023';end if;
 if control.nexloop_workbench_role(p_tenant,p_principal) is distinct from 'owner' then raise exception 'remediation is owner only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 update control.nexloop_remediation_rule_holds set released_at=clock_timestamp(),released_by=p_principal,release_reason=body->>'reason'
  where tenant_id=p_tenant and world=p_world and rule_id=body->>'rule_id' and released_at is null returning * into h;
 if h.hold_id is null then raise exception 'remediation: not_held' using errcode='22023';end if;
 return jsonb_build_object('rule_id',h.rule_id,'released_at',h.released_at);
end $$;
alter function runtime.nexloop_governed_release_remediation_rule(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_governed_release_remediation_rule(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('remediation.rule_release','release_remediation_rule','human','runtime.nexloop_governed_release_remediation_rule(text,text,text,text,text,jsonb)','NX-057');

-- 6. The three R1 functions with the R2 changes ----------------------------------------------------------------------------------
create or replace function control.nexloop_configure_remediation_rules(p_tenant text,p_world text,p_manifest jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r jsonb;k text;ids text[]:='{}';keys text[]:='{}';cur integer;v integer;
begin
 if session_user<>'nexloop_configurator' then raise exception 'remediation rules are trusted configuration only' using errcode='42501';end if;
 if jsonb_typeof(p_manifest) is distinct from 'object'
  or (select array_agg(x order by x) from jsonb_object_keys(p_manifest) x) is distinct from array['decision','limits','manifest_version','rules','schema_version']
  or p_manifest->>'schema_version' is distinct from 'nexloop-remediation-rules/1' or jsonb_typeof(p_manifest->'manifest_version') is distinct from 'number'
  or coalesce(p_manifest->>'manifest_version','')!~'^[1-9][0-9]{0,8}$' or length(coalesce(p_manifest->>'decision','')) not between 1 and 2000
  or jsonb_typeof(p_manifest->'rules') is distinct from 'array' or jsonb_array_length(p_manifest->'rules')>100
  or jsonb_typeof(p_manifest->'limits') is distinct from 'object'
  or (select array_agg(x order by x) from jsonb_object_keys(p_manifest->'limits') x) is distinct from array['breaker_per_day','consumer_per_day','plan_wakes_per_week','tenant_per_hour']
  or coalesce(p_manifest->'limits'->>'tenant_per_hour','')!~'^[1-9][0-9]{0,3}$' or (p_manifest->'limits'->>'tenant_per_hour')::integer>1000
  or coalesce(p_manifest->'limits'->>'consumer_per_day','')!~'^[1-9][0-9]{0,1}$'
  or coalesce(p_manifest->'limits'->>'plan_wakes_per_week','')!~'^[1-9]$' or coalesce(p_manifest->'limits'->>'breaker_per_day','')!~'^[1-9][0-9]?$' then
  raise exception 'remediation rules: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 for r in select value from jsonb_array_elements(p_manifest->'rules') loop
  if jsonb_typeof(r) is distinct from 'object' or not (r ?& array['rule_id','source','failure_kinds','kind','mode','due_after_seconds','purpose'])
   or exists(select 1 from jsonb_object_keys(r) x where x not in ('rule_id','source','failure_kinds','kind','mode','due_after_seconds','purpose','exclude_bound_replies'))
   or coalesce(r->>'rule_id','')!~'^[a-z][a-z0-9_]{0,63}$' or r->>'rule_id'=any(ids)
   or jsonb_typeof(r->'source') is distinct from 'object'
   or (select array_agg(x order by x) from jsonb_object_keys(r->'source') x) is distinct from array['stable_name','version']
   or coalesce(r->'source'->>'stable_name','')!~'^[A-Za-z][A-Za-z0-9_.]{0,159}$' or coalesce(r->'source'->>'version','')!~'^[1-9][0-9]{0,8}$'
   or jsonb_typeof(r->'failure_kinds') is distinct from 'array' or jsonb_array_length(r->'failure_kinds') not between 1 and 2
   or exists(select 1 from jsonb_array_elements_text(r->'failure_kinds') f where f not in ('origin_expired','confirmed_not_delivered'))
   or (select count(distinct f) from jsonb_array_elements_text(r->'failure_kinds') f)<>jsonb_array_length(r->'failure_kinds')
   or r->>'kind' not in ('reevaluate','escalate','governed_action') or r->>'mode' not in ('automatic','manual')
   or coalesce(r->>'due_after_seconds','')!~'^[1-9][0-9]{3,6}$' or (r->>'due_after_seconds')::integer not between 3600 and 2592000
   or length(coalesce(r->>'purpose','')) not between 1 and 500
   or (r ? 'exclude_bound_replies' and jsonb_typeof(r->'exclude_bound_replies') is distinct from 'boolean') then
   raise exception 'remediation rules: invalid_rule' using errcode='22023';end if;
  -- R2 opens reevaluate; governed_action (R3) is refused until its slice.
  if r->>'kind'='governed_action' then raise exception 'remediation rules: kind_unavailable' using errcode='22023';end if;
  -- The source is an Action this tenant declared external (effect categories, 0153), at its exact version.
  if not exists(select 1 from control.nexloop_action_effect_externality e where e.tenant_id=p_tenant and e.world=p_world and e.external_effect
    and e.resource_id='eios:action:'||(r->'source'->>'stable_name')||':'||(r->'source'->>'version')) then
   raise exception 'remediation rules: source_not_effect' using errcode='22023';end if;
  -- Bound replies belong to the reply guarantee (design §3.3).
  if r->'source'->>'stable_name'='nexloop.service.request' and r->'exclude_bound_replies' is distinct from 'true'::jsonb then
   raise exception 'remediation rules: bound_replies_not_excluded' using errcode='22023';end if;
  for k in select r->'source'->>'stable_name'||':'||(r->'source'->>'version')||':'||f||':'||(r->>'kind') from jsonb_array_elements_text(r->'failure_kinds') f loop
   if k=any(keys) then raise exception 'remediation rules: duplicate_rule' using errcode='22023';end if;
   keys:=keys||k;
  end loop;
  ids:=ids||(r->>'rule_id');
 end loop;
 v:=(p_manifest->>'manifest_version')::integer;
 select max(manifest_version) into cur from control.nexloop_remediation_rules where tenant_id=p_tenant and world=p_world;
 if cur is not null and v<=cur then
  if exists(select 1 from control.nexloop_remediation_rules where tenant_id=p_tenant and world=p_world and manifest_version=v and manifest=p_manifest) then
   perform set_config('eios.tenant_id',coalesce(prior,''),true);
   return jsonb_build_object('configured',true,'manifest_version',v,'replay',true);end if;
  raise exception 'remediation rules: version_must_increase' using errcode='22023';end if;
 insert into control.nexloop_remediation_rules(tenant_id,world,manifest_version,manifest,digest)
  values(p_tenant,p_world,v,p_manifest,encode(sha256(convert_to(p_manifest::text,'UTF8')),'hex'));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('configured',true,'manifest_version',v,'replay',false,'rules',jsonb_array_length(p_manifest->'rules'));
end $$;

create or replace function authz.nexloop_remediation_plan_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-remediation-plan-v1',
  'eios:action:nexloop.remediation.plan:1',array['nexloop_domain_worker']);
 c jsonb:=p_payload::jsonb;v_limit integer;v_closed text;m control.nexloop_remediation_rules;f runtime.nexloop_effect_failures;
 i runtime.nexloop_effect_intents;r jsonb;v_decision text;v_rules integer;v_id uuid;v_recent integer;v_consumer_recent integer;
 v_code text;v_expired integer:=0;v_hour timestamptz;counts jsonb:='{}'::jsonb;x record;v_actor text:=(p_text::jsonb)->>'principal_id';v_origin uuid;v_held text[];
begin
 -- Design §6.1 (S-3): lock_timeout 1 s here (read at each lock wait); the 5 s statement_timeout is set by the port before this call
 -- (a setting made inside the statement would not bound it, L6 R1 review 1).
 perform set_config('lock_timeout','1000',true);
 perform set_config('eios.tenant_id',t,true);
 if c->>'verb' is distinct from 'round' or (select array_agg(k order by k) from jsonb_object_keys(c) k) is distinct from array['limit','verb']
  or coalesce(c->>'limit','')!~'^[1-9][0-9]?$' or (c->>'limit')::integer>50 then
  raise exception 'remediation plan invalid' using errcode='22023';end if;
 v_limit:=(c->>'limit')::integer;
 if not pg_try_advisory_xact_lock(hashtext('nexloop-remediation-plan:'||t||':'||p_world)) then
  return jsonb_build_object('status','busy');end if;
 -- Due proposals expire whatever the switches (design §9.4).
 for x in select q.remediation_id from runtime.nexloop_remediation_intents q where q.tenant_id=t and q.world=p_world and q.state='proposed'
   and q.due_by<=clock_timestamp() order by q.due_by limit 200 for update skip locked loop
  perform runtime.nexloop_remediation_move(x.remediation_id,'expired','due_by_passed','service',v_actor,null);
  v_expired:=v_expired+1;
 end loop;
 -- The switches, read at the start of the round (design §9.4): the deployment, the tenant switch, the tenant control pause.
 v_closed:=case when coalesce(current_setting('nexloop.remediation_enabled',true),'') is distinct from 'true' then 'deployment'
  when runtime.nexloop_remediation_paused(t,p_world) then 'tenant'
  when exists(select 1 from control.nexloop_control_scopes s where s.tenant_id=t and s.world=p_world and s.paused and s.scope_kind='tenant' and s.scope_ref='*') then 'control'
  end;
 select * into m from control.nexloop_remediation_rules q where q.tenant_id=t and q.world=p_world order by q.manifest_version desc limit 1;
 for f in select ff.* from runtime.nexloop_effect_failures ff where ff.tenant_id=t and ff.world=p_world
   and not exists(select 1 from runtime.nexloop_remediation_scans s where s.failure_id=ff.failure_id)
   order by ff.recorded_at,ff.failure_id limit v_limit for update of ff skip locked loop
  v_decision:=null;v_rules:=0;
  if v_closed is not null then v_decision:='switch_closed';
  elsif p_world<>'real' then v_decision:='not_real';
  elsif f.reason_code not in ('origin_expired','confirmed_not_delivered') then v_decision:='not_eligible';
  else
   select * into i from runtime.nexloop_effect_intents q where q.intent_id=f.intent_id and q.tenant_id=t and q.world=p_world for share;
   if not found or i.state<>'failed' then v_decision:='not_failed';end if;
  end if;
  -- R2 (design §9.1): an intent submitted by a remediation Run (its Run consumed a wake) only escalates, whatever the rule says.
  if v_decision is null then v_origin:=runtime.nexloop_remediation_origin(t,p_world,i.intent_id);end if;
  if v_decision is null then
   for r in select value from jsonb_array_elements(coalesce(m.manifest->'rules','[]'::jsonb)) loop
    continue when r->'source'->>'stable_name' is distinct from i.action_name or (r->'source'->>'version')::integer<>i.action_version
     or not (r->'failure_kinds' ? f.reason_code);
    -- R2 (design §9.3): a held rule generates nothing until the owner releases it.
    if runtime.nexloop_remediation_rule_held(t,p_world,r->>'rule_id') then v_decision:=coalesce(v_decision,'rule_held');continue;end if;
    if r->'exclude_bound_replies'='true'::jsonb and exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=t and o.world=p_world
      and o.intent_id=i.intent_id and o.trigger_message_id is not null) then
     v_decision:=coalesce(v_decision,'bound_reply');continue;end if;
    -- The tenant rate limit (S-2): past it, one aggregated escalation per hour carries the count; nothing per failure.
    select count(*) into v_recent from runtime.nexloop_remediation_intents q where q.tenant_id=t and q.world=p_world
     and q.created_at>clock_timestamp()-interval '1 hour';
    if v_recent>=(m.manifest->'limits'->>'tenant_per_hour')::integer then
     v_hour:=date_trunc('hour',clock_timestamp());
     insert into runtime.nexloop_remediation_rate_caps as rc(tenant_id,world,hour,over_count) values(t,p_world,v_hour,1)
      on conflict(tenant_id,world,hour) do update set over_count=rc.over_count+1,updated_at=clock_timestamp();
     v_decision:='rate_capped';exit;
    end if;
    -- The Consumer chain cap (design §9.2): past it the remediation is an escalation only, coded chain_capped.
    select count(*) into v_consumer_recent from runtime.nexloop_remediation_intents q join runtime.nexloop_effect_intents e
      on e.intent_id=q.source_intent_id and e.tenant_id=q.tenant_id and e.world=q.world
     where q.tenant_id=t and q.world=p_world and e.consumer_id=i.consumer_id and q.created_at>clock_timestamp()-interval '24 hours';
    v_code:=case when v_origin is not null then 'remediation_origin' when v_consumer_recent>=(m.manifest->'limits'->>'consumer_per_day')::integer then 'chain_capped' else 'proposed' end;
    insert into runtime.nexloop_remediation_intents(tenant_id,world,source_intent_id,source_action,source_action_version,failure_kind,failure_id,
      rule_id,rule_version,rule_digest,kind,mode,state,due_by,last_code,origin_remediation_id)
     values(t,p_world,i.intent_id,i.action_name,i.action_version,f.reason_code,f.failure_id,r->>'rule_id',m.manifest_version,m.digest,
      case when v_code in ('chain_capped','remediation_origin') then 'escalate' else r->>'kind' end,r->>'mode','proposed',
      clock_timestamp()+make_interval(secs=>(r->>'due_after_seconds')::integer),v_code,v_origin)
     on conflict(tenant_id,world,source_intent_id,rule_id) do nothing returning remediation_id into v_id;
    if v_id is not null then
     insert into runtime.nexloop_remediation_events(remediation_id,tenant_id,world,from_state,to_state,code,actor_kind,actor)
      values(v_id,t,p_world,null,'proposed',v_code,'service',v_actor);
     -- An automatic escalation goes to the owner at once; a manual one waits for the owner's resolution. An automatic reevaluate
     -- wakes the waiting plan now (R2); a capped or remediation-origin one is an escalation only.
     if v_code in ('chain_capped','remediation_origin') or (r->>'mode'='automatic' and r->>'kind'='escalate') then
      perform runtime.nexloop_remediation_move(v_id,'escalated',case when v_code='proposed' then 'escalated_automatic' else v_code end,
       'service',v_actor,null);
     elsif r->>'mode'='automatic' and r->>'kind'='reevaluate' then
      perform runtime.nexloop_remediation_wake(t,p_world,v_id,'service',v_actor,null);
     end if;
     v_rules:=v_rules+1;
    end if;
   end loop;
   v_decision:=case when v_rules>0 then 'generated' else coalesce(v_decision,'no_rule') end;
  end if;
  insert into runtime.nexloop_remediation_scans(failure_id,tenant_id,world,decision,rules) values(f.failure_id,t,p_world,v_decision,v_rules);
  counts:=counts||jsonb_build_object(v_decision,coalesce((counts->>v_decision)::integer,0)+1);
 end loop;
 -- R2 (design §9.3): the breaker holds a rule past its limit in 24 hours; the owner releases it.
 v_held:=runtime.nexloop_remediation_breaker(t,p_world);
 return jsonb_build_object('status',coalesce('closed:'||v_closed,'open'),'expired',v_expired,'decisions',counts,'held',to_jsonb(v_held));
end $$;

create or replace function runtime.nexloop_governed_resolve_remediation(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare r runtime.nexloop_remediation_intents;v_to text;v_wake jsonb;
begin
 if body->>'operation' is distinct from 'resolve_remediation'
  or coalesce(body->>'remediation_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or coalesce(body->>'outcome','') not in ('handled','cancelled','wake') or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'remediation: invalid' using errcode='22023';end if;
 if control.nexloop_workbench_role(p_tenant,p_principal) is distinct from 'owner' then raise exception 'remediation is owner only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from runtime.nexloop_remediation_intents where remediation_id=(body->>'remediation_id')::uuid and tenant_id=p_tenant and world=p_world for update;
 if not found then raise exception 'remediation: not_found' using errcode='22023';end if;
 if r.state not in ('proposed','escalated') then raise exception 'remediation: not_open' using errcode='22023';end if;
 -- R2: a manual reevaluate is woken by the owner (the same wake, its plan cap and switches; design §6.2).
 if body->>'outcome'='wake' then
  if r.kind<>'reevaluate' then raise exception 'remediation: not_reevaluate' using errcode='22023';end if;
  if r.state<>'proposed' then raise exception 'remediation: not_open' using errcode='22023';end if;
  v_wake:=runtime.nexloop_remediation_wake(p_tenant,p_world,r.remediation_id,'human',p_principal,body->>'reason');
  if v_wake ? 'closed' then raise exception 'remediation: switch_closed' using errcode='22023';end if;
  return v_wake;
 end if;
 v_to:=case body->>'outcome' when 'handled' then 'handled' else 'declined' end;
 perform runtime.nexloop_remediation_move(r.remediation_id,v_to,case body->>'outcome' when 'handled' then 'marked_handled' else 'cancelled' end,
  'human',p_principal,body->>'reason');
 return jsonb_build_object('remediation_id',r.remediation_id,'state',v_to);
end $$;

-- 7. Snapshot and workbench list, wrapped once more --------------------------------------------------------------------------------
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_remediation_wake;
revoke all on function runtime.nexloop_metrics_snapshot_before_remediation_wake(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s jsonb:=runtime.nexloop_metrics_snapshot_before_remediation_wake(p_tenant,p_world);prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 v:=jsonb_build_object(
  'held_rules',(select count(*) from control.nexloop_remediation_rule_holds where tenant_id=p_tenant and world=p_world and released_at is null),
  'woken_last_24h',(select count(*) from runtime.nexloop_remediation_intents where tenant_id=p_tenant and world=p_world
    and woken_at>clock_timestamp()-interval '24 hours'));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return s||jsonb_build_object('remediation',coalesce(s->'remediation','{}'::jsonb)||v);
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;

alter function authz.nexloop_workbench_remediations(text,text,text,text,integer) rename to nexloop_workbench_remediations_before_wake;
revoke all on function authz.nexloop_workbench_remediations_before_wake(text,text,text,text,integer) from public;
create function authz.nexloop_workbench_remediations(p_tenant text,p_world text,p_role text,p_state text,p_limit integer) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=authz.nexloop_workbench_remediations_before_wake(p_tenant,p_world,p_role,p_state,p_limit);prior text:=current_setting('eios.tenant_id',true);
 v_rows jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(x.value||jsonb_build_object('plan_id',q.plan_id) order by x.ordinality),'[]'::jsonb) into v_rows
  from jsonb_array_elements(v->'remediations') with ordinality x
  left join runtime.nexloop_remediation_intents q on q.remediation_id=(x.value->>'remediation_id')::uuid;
 v:=v||jsonb_build_object('remediations',v_rows,'held_rules',coalesce((select jsonb_agg(jsonb_build_object('rule_id',h.rule_id,'reason_code',h.reason_code,
   'observed',h.observed,'held_at',h.held_at) order by h.held_at) from control.nexloop_remediation_rule_holds h
  where h.tenant_id=p_tenant and h.world=p_world and h.released_at is null),'[]'::jsonb));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function authz.nexloop_workbench_remediations(text,text,text,text,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_remediations(text,text,text,text,integer) from public;
