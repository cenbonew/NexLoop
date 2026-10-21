-- NX-043 X3 (design §2, §4, §6, §8, §9, D7; NX-041 D7, S2, S4): the shadow world - redacted copy of real inbound events, shadow
-- decisions recorded and never sent, and the frozen aggregate difference report. The migration number lives only in the file name and
-- the catalog; nothing below repeats it.
-- 1. Switch per tenant, default off (D7): control.nexloop_shadow_copy_events, appended by the governed owner Action
--    nexloop.shadow_copy.set / set_shadow_copy (from real only); the latest event decides, and only while its shadow:<uuid> world is
--    registered, active and of the same tenant (runtime.nexloop_shadow_copy_target). No event: no copy.
-- 2. Redacted copy: a real inbound conversation message (direction inbound) inserted while the switch is on becomes one
--    runtime.nexloop_shadow_events row in the target world - an event key sha256(world|message id), a content hash sha256(world|payload
--    digest) and the hour it was copied; never the text, the message, conversation or Consumer id. A real Claim extracted from that
--    message adds its Claim type (epistemic kind) to runtime.nexloop_shadow_event_claims. The world in both hashes makes the keys of
--    one shadow world useless in another.
-- 3. Shadow decisions (record only): authz.nexloop_shadow_decision_record(digest, world, decision) - the simulation worker
--    (nexloop_sim_worker, S6c app_role, the shadow world's own credential; real and test refused before any lookup) records the decision
--    a published strategy version takes on a copied event: reply, none, follow_up or outreach. follow_up and outreach go through the S4
--    stand-in (authz.nexloop_sim_effect_submit, nexloop.outreach.send / outreach_outbox, origin replay): a simulated receipt, nothing is
--    sent; a reply is recorded only (no conversation exists in the shadow world). The real effect tables refuse a shadow row (NX-041
--    CHECK nx041_real_effect_world).
-- 4. Difference report: runtime.nexloop_shadow_report_freeze(tenant, world, strategy, version, since, until, by) (owner only) compares
--    the strategy's shadow decisions with what real did on the same inbound message (the next message of its conversation outbound:
--    reply, otherwise none). Aggregate cells only: (shadow decision, real decision) with event and distinct-Consumer counts; a cell, and
--    the agreement rate, is output only with at least runtime.nexloop_shadow_minimum_consumers() distinct Consumers (NX-042 D3: 30),
--    otherwise suppressed and only counted. The report is frozen (append-only).
-- 5. The copies, decisions and reports have a world column (shadow only) and leave with their world (NX-041 S2 whole deletion);
--    append-only otherwise. The switch ledger has none (its target is target_world): it never leaves with a world, and a world leaving
--    active appends an off event (L6 X3 review 1.1) - after a deletion nothing copies to any world.
-- 6. L6 X3 review fixes: a copy failure never fails the real message (an exception block is a savepoint: the copy is rolled back, the
--    real insert goes on, runtime.nexloop_shadow_copy_failures counts it); only direction = inbound is copied (a message without a
--    direction is not); report windows are whole UTC days and the side counts say only none / some.

-- 1. Switch ------------------------------------------------------------------------------------------------------------------------------
create table control.nexloop_shadow_copy_events (
 event_id bigint generated always as identity primary key,
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 target_world text not null check(runtime.nexloop_world_kind(target_world)='shadow'),
 enabled boolean not null,
 reason text not null check(char_length(reason) between 1 and 500),
 changed_by text not null check(char_length(changed_by) between 1 and 200),
 request_id text not null check(char_length(request_id) between 1 and 200),
 recorded_at timestamptz not null default clock_timestamp(),
 unique(tenant_id,request_id));
create index nexloop_shadow_copy_events_tenant on control.nexloop_shadow_copy_events(tenant_id,event_id);
-- Copy failures, counted per tenant, hour and SQLSTATE (no message, no id): the real message was written anyway.
create table runtime.nexloop_shadow_copy_failures (
 tenant_id text not null references control.nexloop_tenants(tenant_id),failed_hour timestamptz not null,sqlstate text not null check(sqlstate~'^[0-9A-Z]{5}$'),
 failures integer not null check(failures>=1),
 primary key(tenant_id,failed_hour,sqlstate));
alter table runtime.nexloop_shadow_copy_failures owner to nexloop_owner;
alter table runtime.nexloop_shadow_copy_failures enable row level security;
alter table runtime.nexloop_shadow_copy_failures force row level security;
create policy tenant_boundary on runtime.nexloop_shadow_copy_failures to nexloop_owner using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_shadow_copy_failures from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker,nexloop_twin_port,nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api;

-- 2. Copies ------------------------------------------------------------------------------------------------------------------------------
create table runtime.nexloop_shadow_events (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 world text not null check(runtime.nexloop_world_kind(world)='shadow'),
 event_key text not null check(event_key~'^[0-9a-f]{64}$'),
 content_hash text not null check(content_hash~'^[0-9a-f]{64}$'),
 copied_hour timestamptz not null check(copied_hour=date_trunc('hour',copied_hour)),
 data_mode text not null default 'simulation' check(data_mode='simulation'),
 origin text not null default 'shadow_copy' check(origin='shadow_copy'),
 primary key(tenant_id,world,event_key));
create table runtime.nexloop_shadow_event_claims (
 tenant_id text not null,world text not null check(runtime.nexloop_world_kind(world)='shadow'),
 event_key text not null,
 claim_type text not null check(claim_type in ('user_statement','preference','constraint','intent','need_problem','commitment','hypothesis','correction')),
 primary key(tenant_id,world,event_key,claim_type),
 foreign key(tenant_id,world,event_key) references runtime.nexloop_shadow_events(tenant_id,world,event_key));

-- 3. Decisions --------------------------------------------------------------------------------------------------------------------------
create table runtime.nexloop_shadow_decisions (
 tenant_id text not null,world text not null check(runtime.nexloop_world_kind(world)='shadow'),
 event_key text not null,
 strategy_id text not null,strategy_version integer not null,
 decision text not null check(decision in ('reply','none','follow_up','outreach')),
 stand_in_intent uuid,
 data_mode text not null default 'simulation' check(data_mode='simulation'),
 output_kind text not null default 'shadow_decision' check(output_kind='shadow_decision'),
 recorded_by text not null check(char_length(recorded_by) between 1 and 200),
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,event_key,strategy_id,strategy_version),
 check((stand_in_intent is not null)=(decision in ('follow_up','outreach'))),
 foreign key(tenant_id,world,event_key) references runtime.nexloop_shadow_events(tenant_id,world,event_key));

-- 4. Reports ----------------------------------------------------------------------------------------------------------------------------
create table runtime.nexloop_shadow_reports (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 world text not null check(runtime.nexloop_world_kind(world)='shadow'),
 report_id uuid not null,
 strategy_id text not null,strategy_version integer not null,
 window_since timestamptz not null,window_until timestamptz not null check(window_since<window_until),
 minimum_consumers integer not null check(minimum_consumers>=30),
 report jsonb not null check(jsonb_typeof(report)='object'),
 report_digest text not null check(report_digest~'^[0-9a-f]{64}$'),
 created_by text not null check(char_length(created_by) between 1 and 200),
 created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,report_id));

do $$declare t text;begin
 foreach t in array array['control.nexloop_shadow_copy_events','runtime.nexloop_shadow_events','runtime.nexloop_shadow_event_claims',
   'runtime.nexloop_shadow_decisions','runtime.nexloop_shadow_reports'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker,nexloop_twin_port,nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api',t);
  -- Append-only; only the S2 whole deletion (its erasure scope) removes rows (S4 runtime.nexloop_sim_ledger_guard).
  execute format('create trigger nx043_shadow_guard before update or delete on %s for each row execute function runtime.nexloop_sim_ledger_guard()',t);
  execute format('create trigger nx043_shadow_no_truncate before truncate on %s for each statement execute function runtime.nexloop_sim_ledger_guard()',t);
 end loop;
end $$;

-- The tenant's copy target: the latest switch event, if it is on and its shadow world is registered, active and the tenant's.
create function runtime.nexloop_shadow_copy_target(p_tenant text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);e control.nexloop_shadow_copy_events;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select * into e from control.nexloop_shadow_copy_events where tenant_id=p_tenant order by event_id desc limit 1;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 if not found or not e.enabled or control.nexloop_sim_world_status(p_tenant,e.target_world) is distinct from 'active' then return null;end if;
 return e.target_world;
end $$;
alter function runtime.nexloop_shadow_copy_target(text) owner to nexloop_owner;
revoke all on function runtime.nexloop_shadow_copy_target(text) from public;

create function runtime.nexloop_shadow_event_key(p_world text,p_message text) returns text language sql immutable set search_path=pg_catalog,pg_temp as $$
 select encode(sha256(convert_to(p_world||'|'||p_message,'UTF8')),'hex')
$$;
alter function runtime.nexloop_shadow_event_key(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_shadow_event_key(text,text) from public;

-- A copy failure, counted (never raised: the counter itself is in its own savepoint).
create function runtime.nexloop_shadow_copy_failed(p_tenant text,p_sqlstate text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);
begin
 begin
  perform set_config('eios.tenant_id',p_tenant,true);
  insert into runtime.nexloop_shadow_copy_failures(tenant_id,failed_hour,sqlstate,failures) values(p_tenant,date_trunc('hour',clock_timestamp()),coalesce(p_sqlstate,'XX000'),1)
   on conflict(tenant_id,failed_hour,sqlstate) do update set failures=runtime.nexloop_shadow_copy_failures.failures+1;
 exception when others then null;  -- the counter is best effort; the real write must go on
 end;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
end $$;
alter function runtime.nexloop_shadow_copy_failed(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_shadow_copy_failed(text,text) from public;

-- A shadow world leaving active (deleting, deleted) appends an off event for every tenant whose switch points at it (L6 review 1.1).
create function runtime.nexloop_shadow_world_left() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare e control.nexloop_shadow_copy_events;v_prior text:=current_setting('eios.tenant_id',true);
begin
 if runtime.nexloop_world_kind(new.world) is distinct from 'shadow' or old.status<>'active' or new.status='active' then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select * into e from control.nexloop_shadow_copy_events where tenant_id=new.tenant_id order by event_id desc limit 1;
 if found and e.enabled and e.target_world=new.world then
  insert into control.nexloop_shadow_copy_events(tenant_id,target_world,enabled,reason,changed_by,request_id)
   values(new.tenant_id,new.world,false,'shadow world '||new.status,'system:sim_world_'||new.status,'world-left:'||new.world||':'||new.status);
 end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_shadow_world_left() owner to nexloop_owner;
revoke all on function runtime.nexloop_shadow_world_left() from public;
create trigger nx043_shadow_world_left after update of status on control.nexloop_sim_worlds for each row execute function runtime.nexloop_shadow_world_left();

-- 2. The copy (real inbound messages and their Claims, while the switch is on) -------------------------------------------------------
create function runtime.nexloop_shadow_copy_message() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_world text;v_prior text:=current_setting('eios.tenant_id',true);
begin
 -- Only an explicitly inbound real message (L6 review 1.3: no direction is not inbound).
 if new.world<>'real' or new.record->>'direction' is distinct from 'inbound' then return null;end if;
 begin  -- a savepoint: whatever fails in the copy is rolled back here and never fails the real message (L6 review 1.2)
  v_world:=runtime.nexloop_shadow_copy_target(new.tenant_id);
  if v_world is null then return null;end if;  -- D7: off by default
  perform set_config('eios.tenant_id',new.tenant_id,true);
  insert into runtime.nexloop_shadow_events(tenant_id,world,event_key,content_hash,copied_hour)
   values(new.tenant_id,v_world,runtime.nexloop_shadow_event_key(v_world,new.message_id),
    encode(sha256(convert_to(v_world||'|'||new.payload_digest,'UTF8')),'hex'),date_trunc('hour',clock_timestamp()))
   on conflict do nothing;
 exception when others then
  perform runtime.nexloop_shadow_copy_failed(new.tenant_id,sqlstate);
 end;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_shadow_copy_message() owner to nexloop_owner;
revoke all on function runtime.nexloop_shadow_copy_message() from public;
create trigger nx043_shadow_copy after insert on runtime.nexloop_conversation_messages for each row execute function runtime.nexloop_shadow_copy_message();

create function runtime.nexloop_shadow_copy_claim() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_world text;v_key text;v_prior text:=current_setting('eios.tenant_id',true);
begin
 if new.world<>'real' or new.source_message_id is null then return null;end if;
 begin  -- a savepoint, as for the message: the real Claim is written whatever happens to the copy
  v_world:=runtime.nexloop_shadow_copy_target(new.tenant_id);
  if v_world is null then return null;end if;
  v_key:=runtime.nexloop_shadow_event_key(v_world,new.source_message_id);
  perform set_config('eios.tenant_id',new.tenant_id,true);
  insert into runtime.nexloop_shadow_event_claims(tenant_id,world,event_key,claim_type)
   select new.tenant_id,v_world,v_key,new.epistemic_kind
   where exists(select 1 from runtime.nexloop_shadow_events e where e.tenant_id=new.tenant_id and e.world=v_world and e.event_key=v_key)
   on conflict do nothing;
 exception when others then
  perform runtime.nexloop_shadow_copy_failed(new.tenant_id,sqlstate);
 end;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;
alter function runtime.nexloop_shadow_copy_claim() owner to nexloop_owner;
revoke all on function runtime.nexloop_shadow_copy_claim() from public;
create trigger nx043_shadow_copy_claim after insert on ontology.nexloop_claims for each row execute function runtime.nexloop_shadow_copy_claim();

-- 1. The governed owner switch ------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_set_shadow_copy(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare e control.nexloop_shadow_copy_events;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','world','enabled','reason'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(runtime.nexloop_world_kind(body->>'world'),'')<>'shadow' or jsonb_typeof(body->'enabled') is distinct from 'boolean'
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(body->>'reason')) not between 1 and 500 then
  raise exception 'shadow copy: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'shadow copy: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform pg_advisory_xact_lock(hashtextextended('nexloop:shadow-copy:'||p_tenant,0));
 select * into e from control.nexloop_shadow_copy_events where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if (e.target_world,e.enabled) is distinct from (body->>'world',(body->>'enabled')::boolean) then raise exception 'shadow copy: request_reused' using errcode='22023';end if;
  return jsonb_build_object('world',e.target_world,'enabled',e.enabled,'changed',false);
 end if;
 if (body->>'enabled')::boolean and control.nexloop_sim_world_status(p_tenant,body->>'world') is distinct from 'active' then
  raise exception 'shadow copy: world_not_active' using errcode='22023';end if;
 insert into control.nexloop_shadow_copy_events(tenant_id,target_world,enabled,reason,changed_by,request_id)
  values(p_tenant,body->>'world',(body->>'enabled')::boolean,btrim(body->>'reason'),p_principal,body->>'request_id') returning * into e;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('shadow-copy:'||e.event_id||':'||md5(p_tenant),'nexloop.shadow_copy.set',p_tenant,p_principal,'shadow_copy',p_intent,
   jsonb_build_object('world',e.target_world,'enabled',e.enabled),clock_timestamp());
 return jsonb_build_object('world',e.target_world,'enabled',e.enabled,'changed',true);
end $$;
alter function control.nexloop_governed_set_shadow_copy(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_set_shadow_copy(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('shadow_copy.set','set_shadow_copy','human','control.nexloop_governed_set_shadow_copy(text,text,text,text,text,jsonb)','NX-043');

-- 3. Shadow decisions ---------------------------------------------------------------------------------------------------------------
create function authz.nexloop_shadow_decision_record(p_digest text,p_world text,p_decision jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;d runtime.nexloop_shadow_decisions;v_intent uuid;v_receipt jsonb;v_principal text;
begin
 -- The simulation worker in a shadow world (S6c app_role); real and test refused before any lookup (sim_effect_tenant).
 if session_user is distinct from 'nexloop_sim_worker' or authz.nexloop_app_role(p_world) is distinct from 'nexloop_domain_worker'
  or coalesce(runtime.nexloop_world_kind(p_world),'')<>'shadow' then
  raise exception 'shadow decision: unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 if jsonb_typeof(p_decision) is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(p_decision) k) is distinct from array['decision','event_key','strategy_id','strategy_version']
  or coalesce(p_decision->>'event_key','')!~'^[0-9a-f]{64}$' or coalesce(p_decision->>'strategy_id','')!~'^[a-z][a-z0-9_.-]{0,63}$'
  or jsonb_typeof(p_decision->'strategy_version') is distinct from 'number' or (p_decision->>'strategy_version')!~'^[0-9]{1,9}$'
  or coalesce(p_decision->>'decision','') not in ('reply','none','follow_up','outreach') then
  raise exception 'shadow decision: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 if not exists(select 1 from runtime.nexloop_shadow_events e where e.tenant_id=v_tenant and e.world=p_world and e.event_key=p_decision->>'event_key') then
  raise exception 'shadow decision: event_unknown' using errcode='22023';end if;
 if runtime.nexloop_strategy_status(v_tenant,p_decision->>'strategy_id',(p_decision->>'strategy_version')::integer) is distinct from 'published' then
  raise exception 'shadow decision: strategy_not_published' using errcode='22023';end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop:shadow-decision:'||p_world||':'||(p_decision->>'event_key'),0));
 select * into d from runtime.nexloop_shadow_decisions where tenant_id=v_tenant and world=p_world and event_key=p_decision->>'event_key'
  and strategy_id=p_decision->>'strategy_id' and strategy_version=(p_decision->>'strategy_version')::integer;
 if found then
  if d.decision<>p_decision->>'decision' then raise exception 'shadow decision: decision_reused' using errcode='22023';end if;
  return jsonb_build_object('event_key',d.event_key,'decision',d.decision,'stand_in_intent',d.stand_in_intent,'recorded',false,'sent',false);
 end if;
 -- An effect decision goes through the S4 stand-in (a simulated receipt); nothing is sent.
 if p_decision->>'decision' in ('follow_up','outreach') then
  v_intent:=md5(p_world||'|'||(p_decision->>'event_key')||'|'||(p_decision->>'strategy_id')||'|'||(p_decision->>'strategy_version'))::uuid;
  v_receipt:=authz.nexloop_sim_effect_submit(p_digest,p_world,jsonb_build_object('intent_id',v_intent,'action_name','nexloop.outreach.send','action_version',1,
   'provider_kind','outreach_outbox','business_digest',encode(sha256(convert_to(p_world||'|'||(p_decision->>'event_key')||'|'||(p_decision->>'decision'),'UTF8')),'hex'),
   'request_digest',encode(sha256(convert_to(v_intent::text,'UTF8')),'hex'),'origin','replay'));
  perform set_config('eios.tenant_id',v_tenant,true);
 end if;
 v_principal:=authz.nexloop_service_identity(p_digest,p_world)->'binding'->>'principal_id';
 insert into runtime.nexloop_shadow_decisions(tenant_id,world,event_key,strategy_id,strategy_version,decision,stand_in_intent,recorded_by)
  values(v_tenant,p_world,p_decision->>'event_key',p_decision->>'strategy_id',(p_decision->>'strategy_version')::integer,p_decision->>'decision',v_intent,
   coalesce(v_principal,'service'))
  returning * into d;
 return jsonb_build_object('event_key',d.event_key,'decision',d.decision,'stand_in_intent',d.stand_in_intent,'recorded',true,'sent',false,
  'simulated',coalesce((v_receipt->>'simulated')::boolean,false));
end $$;
alter function authz.nexloop_shadow_decision_record(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_shadow_decision_record(text,text,jsonb) from public;
grant execute on function authz.nexloop_shadow_decision_record(text,text,jsonb) to nexloop_sim_worker;

-- 4. The frozen difference report ---------------------------------------------------------------------------------------------------
create function runtime.nexloop_shadow_minimum_consumers() returns integer language sql immutable set search_path=pg_catalog,pg_temp as $$ select 30 $$;
alter function runtime.nexloop_shadow_minimum_consumers() owner to nexloop_owner;

create function runtime.nexloop_shadow_report_freeze(p_tenant text,p_world text,p_strategy text,p_version integer,p_since timestamptz,p_until timestamptz,p_by text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_min integer:=runtime.nexloop_shadow_minimum_consumers();v_report jsonb;v_id uuid;r runtime.nexloop_shadow_reports;v_prior text:=current_setting('eios.tenant_id',true);
begin
 if coalesce(runtime.nexloop_world_kind(p_world),'')<>'shadow' or control.nexloop_sim_world_status(p_tenant,p_world) is null then
  raise exception 'shadow report: world_unknown' using errcode='22023';end if;
 if p_since is null or p_until is null or p_since>=p_until or coalesce(p_by,'')='' then raise exception 'shadow report: invalid' using errcode='22023';end if;
 -- L6 review 4.1 (as NX-042 T5): whole UTC days only, so two reports cannot differ by one hour's events; a window answers once (frozen).
 if p_since<>date_trunc('day',p_since at time zone 'UTC') at time zone 'UTC' or p_until<>date_trunc('day',p_until at time zone 'UTC') at time zone 'UTC' then
  raise exception 'shadow report: window_not_on_grid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 with dec as (
   select d.decision shadow,e.event_key from runtime.nexloop_shadow_decisions d join runtime.nexloop_shadow_events e
    on e.tenant_id=d.tenant_id and e.world=d.world and e.event_key=d.event_key
   where d.tenant_id=p_tenant and d.world=p_world and d.strategy_id=p_strategy and d.strategy_version=p_version
    and e.copied_hour>=date_trunc('hour',p_since) and e.copied_hour<p_until),
  inbound as (  -- the real inbound messages, keyed as the copy keyed them (identifiers stay inside this statement)
   select runtime.nexloop_shadow_event_key(p_world,m.message_id) event_key,cv.consumer_id,
    case when (select coalesce(n.record->>'direction','inbound') from runtime.nexloop_conversation_messages n where n.tenant_id=m.tenant_id and n.world=m.world
      and n.conversation_id=m.conversation_id and n.sequence>m.sequence order by n.sequence limit 1)='outbound' then 'reply' else 'none' end real
   from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations cv on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
   where m.tenant_id=p_tenant and m.world='real' and m.record->>'direction'='inbound'),
  joined as (select dec.shadow,i.real,i.consumer_id from dec join inbound i on i.event_key=dec.event_key),
  cells as (select shadow,real,count(*) events,count(distinct consumer_id) consumers from joined group by shadow,real)
 select jsonb_build_object(
   'schema','nexloop-shadow-report/1','world',p_world,'strategy',jsonb_build_object('strategy_id',p_strategy,'version',p_version),
   'window',jsonb_build_object('since',p_since,'until',p_until),'minimum_consumers',v_min,'data_mode','simulation',
   'conclusion_scope','observed in the shadow world only; not a real effect, not a causal estimate',
   'cells',coalesce((select jsonb_agg(jsonb_build_object('shadow',shadow,'real',real,'events',events,'consumers',consumers) order by shadow,real)
     from cells where consumers>=v_min),'[]'::jsonb),
   'suppressed_cells',case when exists(select 1 from cells where consumers<v_min) then 'some' else 'none' end,
   'total',case when (select count(distinct consumer_id) from joined)>=v_min then jsonb_build_object('events',(select count(*) from joined),
     'consumers',(select count(distinct consumer_id) from joined),
     'agreement_rate',round((select count(*) filter (where shadow=real)::numeric/nullif(count(*),0) from joined),4)) else null end,
   'unmatched_events',case when exists(select 1 from dec where not exists(select 1 from inbound i where i.event_key=dec.event_key)) then 'some' else 'none' end) into v_report;
 v_id:=md5(p_world||'|'||p_strategy||'|'||p_version||'|'||p_since::text||'|'||p_until::text)::uuid;
 select * into r from runtime.nexloop_shadow_reports where tenant_id=p_tenant and world=p_world and report_id=v_id;
 if not found then
  insert into runtime.nexloop_shadow_reports(tenant_id,world,report_id,strategy_id,strategy_version,window_since,window_until,minimum_consumers,report,report_digest,created_by)
   values(p_tenant,p_world,v_id,p_strategy,p_version,p_since,p_until,v_min,v_report,encode(sha256(convert_to(v_report::text,'UTF8')),'hex'),p_by)
   returning * into r;
 end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return r.report||jsonb_build_object('report_id',r.report_id,'report_digest',r.report_digest,'frozen',true);
end $$;
alter function runtime.nexloop_shadow_report_freeze(text,text,text,integer,timestamptz,timestamptz,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_shadow_report_freeze(text,text,text,integer,timestamptz,timestamptz,text) from public;
