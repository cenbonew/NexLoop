-- NX-057 R1 (docs/design/remediation-intents.md v4 §3, §4, §5, §6.1, §9.2, §9.4, §9.5, §11; ADR-040 draft): remediation intents,
-- suggestions only. Nothing here touches a business object, an effect intent or a plan: a remediation is a record for the owner.
-- 1. control.nexloop_remediation_rules: the trusted rules manifest (deploy/configuration/remediation-rules.v1.json) per tenant and world,
--    versioned and append-only, published only by the configurator through control.nexloop_configure_remediation_rules, which checks it
--    fail closed: the source must be an Action declared external in control.nexloop_action_effect_externality, at its exact version; the
--    failure kinds are the R0 reasons origin_expired / confirmed_not_delivered; nexloop.service.request needs exclude_bound_replies. R1
--    opens kind escalate only (reevaluate comes with R2, governed_action with R3) and refuses the others.
-- 2. runtime.nexloop_remediation_intents (one per failure and rule) and its append-only events; runtime.nexloop_remediation_scans (each
--    failure row of effect_failures is considered once, with its decision); runtime.nexloop_remediation_rate_caps (one aggregated
--    escalation per tenant and hour past the rate limit, S-2); control.nexloop_remediation_switches (the tenant switch, append-only).
--    No consumer id is stored: the Consumer is the source effect intent's, read through it, so erasure keeps working on the intent.
-- 3. authz.nexloop_remediation_plan_command (eios:action:nexloop.remediation.plan:1, a domain worker service identity; the tenant comes
--    from the service identity, never from a parameter): verb round expires due proposals and considers at most 50 new failure rows.
--    One round per tenant at a time (a transaction advisory lock; a second planner answers busy). Its statement timeout (5 s) is set
--    by the port before the call, lock_timeout (1 s) inside. An escalated remediation is handed to the owner and stays open until the
--    owner marks it handled or cancels it; runtime.nexloop_remediation_move allows only the transitions of design §4.2.
--    A closed switch (deployment REMEDIATION_ENABLED, the tenant switch, or the tenant control pause NXC01) generates nothing: the failures
--    of that time are recorded as switch_closed and never remediated later (only failures after the switch opens count, L6 M-5).
-- 4. Owner Actions (governed entry handlers, owner only): remediation.resolve (an open remediation marked handled or cancelled, with a
--    reason) and remediation.switch (pause or resume the tenant's remediation; an NX-056 policy may later hold resume only, Q-6).
-- 5. authz.nexloop_workbench_read gains the verb remediations (rename and wrap; every other verb unchanged).
-- 6. The metrics snapshot gains a `remediation` block (counts only).

-- 1. Rules ----------------------------------------------------------------------------------------------------------------------
create table control.nexloop_remediation_rules (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,manifest_version integer not null check(manifest_version>=1),manifest jsonb not null,
 digest text not null check(digest~'^[0-9a-f]{64}$'),configured_by text not null default session_user,
 configured_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,manifest_version)
);
alter table control.nexloop_remediation_rules owner to nexloop_owner;
alter table control.nexloop_remediation_rules enable row level security;
alter table control.nexloop_remediation_rules force row level security;
create policy remediation_rules_tenant on control.nexloop_remediation_rules to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_remediation_rules from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,
 nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker;
create trigger nx057_remediation_rules_append_only before update or delete on control.nexloop_remediation_rules
 for each row execute function control.nexloop_nx022_append_only();

create function control.nexloop_configure_remediation_rules(p_tenant text,p_world text,p_manifest jsonb) returns jsonb
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
  or (select array_agg(x order by x) from jsonb_object_keys(p_manifest->'limits') x) is distinct from array['consumer_per_day','tenant_per_hour']
  or coalesce(p_manifest->'limits'->>'tenant_per_hour','')!~'^[1-9][0-9]{0,3}$' or (p_manifest->'limits'->>'tenant_per_hour')::integer>1000
  or coalesce(p_manifest->'limits'->>'consumer_per_day','')!~'^[1-9][0-9]{0,1}$' then
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
  -- R1 opens escalate only; reevaluate (R2) and governed_action (R3) are refused until their slice.
  if r->>'kind'<>'escalate' then raise exception 'remediation rules: kind_unavailable' using errcode='22023';end if;
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
alter function control.nexloop_configure_remediation_rules(text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_configure_remediation_rules(text,text,jsonb) from public;
grant execute on function control.nexloop_configure_remediation_rules(text,text,jsonb) to nexloop_configurator;

-- 2. Remediation intents, events, scans, rate caps, tenant switch -----------------------------------------------------------------
create table runtime.nexloop_remediation_intents (
 remediation_id uuid primary key default gen_random_uuid(),
 tenant_id text not null,world text not null check(world='real'),
 source_intent_id uuid not null,source_action text not null,source_action_version integer not null check(source_action_version>=1),
 failure_kind text not null check(failure_kind in ('origin_expired','confirmed_not_delivered')),
 failure_id uuid not null references runtime.nexloop_effect_failures(failure_id),
 rule_id text not null check(rule_id~'^[a-z][a-z0-9_]{0,63}$'),rule_version integer not null check(rule_version>=1),
 rule_digest text not null check(rule_digest~'^[0-9a-f]{64}$'),
 kind text not null check(kind in ('reevaluate','escalate','governed_action')),mode text not null check(mode in ('automatic','manual')),
 state text not null check(state in ('proposed','awaiting_approval','executing','succeeded','refused','failed','escalated','handled','declined','expired','not_applicable')),
 attempts integer not null default 0 check(attempts between 0 and 3),due_by timestamptz not null,
 last_code text check(last_code~'^[a-z][a-z0-9_:]{0,63}$'),
 created_at timestamptz not null default clock_timestamp(),updated_at timestamptz not null default clock_timestamp(),
 unique(tenant_id,world,source_intent_id,rule_id),unique(failure_id,rule_id),
 foreign key(source_intent_id,tenant_id,world) references runtime.nexloop_effect_intents(intent_id,tenant_id,world)
);
create index nexloop_remediation_intents_state on runtime.nexloop_remediation_intents(tenant_id,world,state,due_by);
create index nexloop_remediation_intents_recent on runtime.nexloop_remediation_intents(tenant_id,world,created_at);
create table runtime.nexloop_remediation_events (
 event_id uuid primary key default gen_random_uuid(),
 remediation_id uuid not null references runtime.nexloop_remediation_intents(remediation_id),
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,
 from_state text,to_state text not null,code text not null check(code~'^[a-z][a-z0-9_:]{0,63}$'),
 actor_kind text not null check(actor_kind in ('service','human')),actor text not null,
 reason text check(reason is null or length(reason) between 1 and 500),
 recorded_at timestamptz not null default clock_timestamp()
);
create index nexloop_remediation_events_of on runtime.nexloop_remediation_events(remediation_id,recorded_at);
create table runtime.nexloop_remediation_scans (
 failure_id uuid primary key references runtime.nexloop_effect_failures(failure_id),
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,
 decision text not null check(decision in ('generated','no_rule','not_eligible','not_real','not_failed','bound_reply','rate_capped','switch_closed')),
 rules integer not null default 0 check(rules>=0),scanned_at timestamptz not null default clock_timestamp()
);
create table runtime.nexloop_remediation_rate_caps (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,hour timestamptz not null,over_count integer not null check(over_count>=1),
 updated_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world,hour)
);
create table control.nexloop_remediation_switches (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,revision integer not null check(revision>=1),paused boolean not null,
 reason text not null check(length(reason) between 1 and 500),principal_id text not null,
 recorded_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world,revision)
);
do $$declare t text;begin
 foreach t in array array['runtime.nexloop_remediation_intents','runtime.nexloop_remediation_events','runtime.nexloop_remediation_scans',
   'runtime.nexloop_remediation_rate_caps','control.nexloop_remediation_switches'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy remediation_tenant on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker',t);
 end loop;
end $$;
create trigger nx057_remediation_events_append_only before update or delete on runtime.nexloop_remediation_events
 for each row execute function control.nexloop_nx022_append_only();
create trigger nx057_remediation_scans_append_only before update or delete on runtime.nexloop_remediation_scans
 for each row execute function control.nexloop_nx022_append_only();
create trigger nx057_remediation_switches_append_only before update or delete on control.nexloop_remediation_switches
 for each row execute function control.nexloop_nx022_append_only();

-- One state change: the row and its event (the only writer of both).
create function runtime.nexloop_remediation_move(p_id uuid,p_to text,p_code text,p_actor_kind text,p_actor text,p_reason text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r runtime.nexloop_remediation_intents;
begin
 select * into r from runtime.nexloop_remediation_intents where remediation_id=p_id for update;
 -- The allowed transitions (design §4.2; escalated is open until a person handles or cancels it, L6 R1 review 2).
 if not found or not (p_to = any(case r.state
   when 'proposed' then array['escalated','handled','declined','expired','not_applicable','succeeded','awaiting_approval','executing']
   when 'escalated' then array['handled','declined']
   when 'awaiting_approval' then array['executing','succeeded','proposed','declined']
   when 'executing' then array['succeeded','refused','failed','escalated','proposed']
   when 'failed' then array['escalated']
   else array[]::text[] end)) then
  raise exception 'remediation: illegal_transition' using errcode='22023',detail=coalesce(r.state,'none')||'->'||p_to;end if;
 update runtime.nexloop_remediation_intents set state=p_to,last_code=p_code,updated_at=clock_timestamp() where remediation_id=p_id;
 insert into runtime.nexloop_remediation_events(remediation_id,tenant_id,world,from_state,to_state,code,actor_kind,actor,reason)
  values(p_id,r.tenant_id,r.world,r.state,p_to,p_code,p_actor_kind,p_actor,p_reason);
end $$;
alter function runtime.nexloop_remediation_move(uuid,text,text,text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_remediation_move(uuid,text,text,text,text,text) from public;

-- The tenant switch: open unless its latest revision pauses.
create function runtime.nexloop_remediation_paused(p_tenant text,p_world text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 v:=coalesce((select s.paused from control.nexloop_remediation_switches s where s.tenant_id=p_tenant and s.world=p_world
  order by s.revision desc limit 1),false);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_remediation_paused(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_remediation_paused(text,text) from public;

-- 3. The planner ----------------------------------------------------------------------------------------------------------------
create function authz.nexloop_remediation_plan_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-remediation-plan-v1',
  'eios:action:nexloop.remediation.plan:1',array['nexloop_domain_worker']);
 c jsonb:=p_payload::jsonb;v_limit integer;v_closed text;m control.nexloop_remediation_rules;f runtime.nexloop_effect_failures;
 i runtime.nexloop_effect_intents;r jsonb;v_decision text;v_rules integer;v_id uuid;v_recent integer;v_consumer_recent integer;
 v_code text;v_expired integer:=0;v_hour timestamptz;counts jsonb:='{}'::jsonb;x record;v_actor text:=(p_text::jsonb)->>'principal_id';
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
  if v_decision is null then
   for r in select value from jsonb_array_elements(coalesce(m.manifest->'rules','[]'::jsonb)) loop
    continue when r->'source'->>'stable_name' is distinct from i.action_name or (r->'source'->>'version')::integer<>i.action_version
     or not (r->'failure_kinds' ? f.reason_code);
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
    v_code:=case when v_consumer_recent>=(m.manifest->'limits'->>'consumer_per_day')::integer then 'chain_capped' else 'proposed' end;
    insert into runtime.nexloop_remediation_intents(tenant_id,world,source_intent_id,source_action,source_action_version,failure_kind,failure_id,
      rule_id,rule_version,rule_digest,kind,mode,state,due_by,last_code)
     values(t,p_world,i.intent_id,i.action_name,i.action_version,f.reason_code,f.failure_id,r->>'rule_id',m.manifest_version,m.digest,
      case when v_code='chain_capped' then 'escalate' else r->>'kind' end,r->>'mode','proposed',
      clock_timestamp()+make_interval(secs=>(r->>'due_after_seconds')::integer),v_code)
     on conflict(tenant_id,world,source_intent_id,rule_id) do nothing returning remediation_id into v_id;
    if v_id is not null then
     insert into runtime.nexloop_remediation_events(remediation_id,tenant_id,world,from_state,to_state,code,actor_kind,actor)
      values(v_id,t,p_world,null,'proposed',v_code,'service',v_actor);
     -- An automatic escalation goes to the owner at once; a manual one waits for the owner's resolution.
     if r->>'mode'='automatic' or v_code='chain_capped' then
      perform runtime.nexloop_remediation_move(v_id,'escalated',case when v_code='chain_capped' then 'chain_capped' else 'escalated_automatic' end,
       'service',v_actor,null);
     end if;
     v_rules:=v_rules+1;
    end if;
   end loop;
   v_decision:=case when v_rules>0 then 'generated' else coalesce(v_decision,'no_rule') end;
  end if;
  insert into runtime.nexloop_remediation_scans(failure_id,tenant_id,world,decision,rules) values(f.failure_id,t,p_world,v_decision,v_rules);
  counts:=counts||jsonb_build_object(v_decision,coalesce((counts->>v_decision)::integer,0)+1);
 end loop;
 return jsonb_build_object('status',coalesce('closed:'||v_closed,'open'),'expired',v_expired,'decisions',counts);
end $$;
alter function authz.nexloop_remediation_plan_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_remediation_plan_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_remediation_plan_command(text,text,text,text,text) to nexloop_domain_worker;

-- 4. Owner Actions ---------------------------------------------------------------------------------------------------------------
-- remediation.resolve: an open remediation (proposed, or escalated to the owner) is marked handled (a person took care of it) or cancelled (declined), with
-- a reason. Owner only; the governed entry has re-verified the person's current authority (human only).
create function runtime.nexloop_governed_resolve_remediation(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare r runtime.nexloop_remediation_intents;v_to text;
begin
 if body->>'operation' is distinct from 'resolve_remediation'
  or coalesce(body->>'remediation_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or coalesce(body->>'outcome','') not in ('handled','cancelled') or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'remediation: invalid' using errcode='22023';end if;
 if control.nexloop_workbench_role(p_tenant,p_principal) is distinct from 'owner' then raise exception 'remediation is owner only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from runtime.nexloop_remediation_intents where remediation_id=(body->>'remediation_id')::uuid and tenant_id=p_tenant and world=p_world for update;
 if not found then raise exception 'remediation: not_found' using errcode='22023';end if;
 if r.state not in ('proposed','escalated') then raise exception 'remediation: not_open' using errcode='22023';end if;
 v_to:=case body->>'outcome' when 'handled' then 'handled' else 'declined' end;
 perform runtime.nexloop_remediation_move(r.remediation_id,v_to,case body->>'outcome' when 'handled' then 'marked_handled' else 'cancelled' end,
  'human',p_principal,body->>'reason');
 return jsonb_build_object('remediation_id',r.remediation_id,'state',v_to);
end $$;
alter function runtime.nexloop_governed_resolve_remediation(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_governed_resolve_remediation(text,text,text,text,text,jsonb) from public;

-- remediation.switch: pause (at once) or resume the tenant's remediation, with a reason. Owner only. Each change is one new revision.
create function runtime.nexloop_governed_switch_remediation(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_revision integer;
begin
 if body->>'operation' is distinct from 'switch_remediation' or jsonb_typeof(body->'paused') is distinct from 'boolean'
  or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'remediation: invalid' using errcode='22023';end if;
 if control.nexloop_workbench_role(p_tenant,p_principal) is distinct from 'owner' then raise exception 'remediation is owner only' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform pg_advisory_xact_lock(hashtext('nexloop-remediation-switch:'||p_tenant||':'||p_world));
 select coalesce(max(revision),0)+1 into v_revision from control.nexloop_remediation_switches where tenant_id=p_tenant and world=p_world;
 insert into control.nexloop_remediation_switches(tenant_id,world,revision,paused,reason,principal_id)
  values(p_tenant,p_world,v_revision,(body->>'paused')::boolean,body->>'reason',p_principal);
 return jsonb_build_object('paused',(body->>'paused')::boolean,'revision',v_revision);
end $$;
alter function runtime.nexloop_governed_switch_remediation(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function runtime.nexloop_governed_switch_remediation(text,text,text,text,text,jsonb) from public;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('remediation.resolve','resolve_remediation','human','runtime.nexloop_governed_resolve_remediation(text,text,text,text,text,jsonb)','NX-057'),
 ('remediation.switch','switch_remediation','human','runtime.nexloop_governed_switch_remediation(text,text,text,text,text,jsonb)','NX-057');

-- 5. Workbench read verb `remediations` ----------------------------------------------------------------------------------------------
-- The list for the Actions page: no content, no reason text; the Consumer id only where the member's role derives READ on it (ADR-025),
-- and never for a Consumer being erased (such rows are counted as hidden, NX-029).
create function authz.nexloop_workbench_remediations(p_tenant text,p_world text,p_role text,p_state text,p_limit integer) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 with rows as (
  select q.*,e.consumer_id,f.recorded_at failed_at,runtime.nexloop_consumer_erasing(p_tenant,p_world,e.consumer_id) erasing
  from runtime.nexloop_remediation_intents q
  join runtime.nexloop_effect_intents e on e.intent_id=q.source_intent_id and e.tenant_id=q.tenant_id and e.world=q.world
  join runtime.nexloop_effect_failures f on f.failure_id=q.failure_id
  where q.tenant_id=p_tenant and q.world=p_world and (p_state is null or q.state=p_state)
  order by q.created_at desc,q.remediation_id limit p_limit)
 select jsonb_build_object(
  'remediations',coalesce((select jsonb_agg(jsonb_build_object('remediation_id',x.remediation_id,'source_intent_id',x.source_intent_id,
     'source_action',x.source_action,'source_action_version',x.source_action_version,
     'consumer_id',case when authz.nexloop_workbench_member_may_read(p_tenant,p_world,p_role,'eios:object:Consumer/'||x.consumer_id) then x.consumer_id end,
     'failure_kind',x.failure_kind,'failed_at',x.failed_at,'rule_id',x.rule_id,'rule_version',x.rule_version,'kind',x.kind,'mode',x.mode,
     'state',x.state,'due_by',x.due_by,'attempts',x.attempts,'last_code',x.last_code,'created_at',x.created_at,'updated_at',x.updated_at)
    order by x.created_at desc,x.remediation_id) from rows x where not x.erasing),'[]'::jsonb),
  'hidden',(select count(*) from rows x where x.erasing),
  'paused',runtime.nexloop_remediation_paused(p_tenant,p_world),
  'switch_revision',coalesce((select max(s.revision) from control.nexloop_remediation_switches s where s.tenant_id=p_tenant and s.world=p_world),0),
  'rate_capped',coalesce((select jsonb_agg(jsonb_build_object('hour',rc.hour,'over_count',rc.over_count) order by rc.hour desc)
    from runtime.nexloop_remediation_rate_caps rc where rc.tenant_id=p_tenant and rc.world=p_world and rc.hour>clock_timestamp()-interval '24 hours'),'[]'::jsonb))
 into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function authz.nexloop_workbench_remediations(text,text,text,text,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_remediations(text,text,text,text,integer) from public;

alter function authz.nexloop_workbench_read(text,text,text,text,text) rename to nexloop_workbench_read_before_remediations;
revoke all on function authz.nexloop_workbench_read_before_remediations(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';v_limit integer;
begin
 if c->>'verb' is distinct from 'remediations' then
  return authz.nexloop_workbench_read_before_remediations(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0130 verification, for this verb (as the object_fields wrapper).
 if session_user is distinct from 'nexloop_api' or p_world is distinct from 'real'
  or octet_length(p_text)>1048576 or octet_length(p_payload)>8192
  or a->>'protocol' is distinct from 'nexloop-workbench-read-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench read unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench read unavailable' using errcode='42501';end if;
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action) then
  raise exception 'workbench read forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if exists(select 1 from jsonb_object_keys(c) x where x not in ('verb','state','limit'))
  or (c ? 'state' and coalesce(c->>'state','') not in ('proposed','awaiting_approval','executing','succeeded','refused','failed','escalated','handled','declined',
   'expired','not_applicable'))
  or (c ? 'limit' and coalesce(c->>'limit','')!~'^[1-9][0-9]{0,2}$') or coalesce((c->>'limit')::integer,50)>200 then
  raise exception 'workbench read invalid' using errcode='22023';end if;
 v_limit:=coalesce((c->>'limit')::integer,50);
 return authz.nexloop_workbench_remediations(v_tenant,p_world,w->>'role',c->>'state',v_limit);
end $$;
alter function authz.nexloop_workbench_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_read(text,text,text,text,text) to nexloop_api;

-- 6. Metrics: the `remediation` block (counts only) ----------------------------------------------------------------------------------
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_remediation;
revoke all on function runtime.nexloop_metrics_snapshot_before_remediation(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s jsonb:=runtime.nexloop_metrics_snapshot_before_remediation(p_tenant,p_world);prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 v:=jsonb_build_object(
  'by_state',coalesce((select jsonb_object_agg(q.state,q.n) from (select state,count(*) n from runtime.nexloop_remediation_intents
    where tenant_id=p_tenant and world=p_world group by state) q),'{}'::jsonb),
  'waiting_over_24h',(select count(*) from runtime.nexloop_remediation_intents where tenant_id=p_tenant and world=p_world and state in ('proposed','escalated')
    and created_at<=clock_timestamp()-interval '24 hours'),
  'generated_last_hour',(select count(*) from runtime.nexloop_remediation_intents where tenant_id=p_tenant and world=p_world
    and created_at>clock_timestamp()-interval '1 hour'),
  'rate_capped_last_hour',coalesce((select sum(over_count) from runtime.nexloop_remediation_rate_caps where tenant_id=p_tenant and world=p_world
    and hour>=date_trunc('hour',clock_timestamp()-interval '1 hour')),0),
  'paused',case when runtime.nexloop_remediation_paused(p_tenant,p_world) then 1 else 0 end);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return s||jsonb_build_object('remediation',v);
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
