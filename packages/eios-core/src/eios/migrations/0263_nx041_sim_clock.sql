-- NX-041 S5 "simulation clock" (docs/implementation/NX-041-design.md §4, D5, §11 S5, migration D; owner D1-D13 decided). The migration
-- number lives only in the file name and the catalog; nothing below repeats it.
--
-- 1. control.nexloop_sim_clocks: one logical clock per registered sim:/shadow: world, created with the world (paused at its creation
--    time: a simulation world never reads the wall by default). It moves only forward: advance by seconds, pause, resume, set a rate.
--    Every change is a row of control.nexloop_sim_clock_events (append-only, one per revision) and an audit event. Real and test have
--    no clock and are refused.
-- 2. runtime.nexloop_world_now(tenant, world): real and test -> clock_timestamp() (unchanged behaviour); sim/shadow -> the world's
--    clock, never the wall (D5: no fallback; a missing clock is an error). runtime.nexloop_world_wall_at(tenant, world, at): the wall
--    moment a world time is reached (real and test: the time itself; a paused world: infinity), used where a world due time is put on
--    the wall-clock work feed. A clock change makes the world's pending commitment and plan items due now so they are re-evaluated.
-- 3. Governed owner Action sim_clock.control / control_sim_clock (owner only, like sim_world.create).
-- 4. The time-dependent logic that reads the world time (create or replace of the current bodies; only the time expressions change):
--      authz.nexloop_commitment_command        evaluation time (due_soon, breached, condition_unresolved_at_due, no_due_date); next stage
--      runtime.nexloop_commitment_touch        a due time put on the monitor feed at its wall moment
--      runtime.nexloop_plan_mark_due           reassessment due time (min_reassess_seconds from the world time)
--      authz.nexloop_plan_feed_touch           a due time put on the plan feed at its wall moment
--      runtime.nexloop_outreach_accept         the business intent day, the approval expiry
--      control.nexloop_governed_outreach_approve  approval expiry check, decided_at, finished_at
--      runtime.nexloop_outreach_expire_approvals  approval expiry, decided_at, finished_at
--      authz.nexloop_outreach_command          gates at the world time (quiet hours, 24 h and 7 day frequency, 7 day marketing allowance),
--                                              admitted_at, finished_at
--      runtime.nexloop_outreach_gates          unchanged except the model budget, which is money: its period stays the wall in sim
--      runtime.nexloop_outreach_metrics        the 7-day windows end at the world time
--    and a stamp trigger gives outreach sends (created_at) and approvals (requested_at) of a sim world their world creation time.
--    Deliberately unchanged (wall clock in every world): leases, work-feed retries and ordering, updated_at, audit and event recording
--    times, credential and signature expiry, the sim world's own expiry and the 30-day summary retention (D2: an experiment's real
--    lifetime), retention sweeps (sim data leaves by whole deletion, D2), cost budgets and their periods (D6), the plan self-trigger
--    rate window. Not reachable in a sim world today (their tables are real only: 0046/0049/0079 check(world='real')): the reply
--    guarantee (reply-due items come from inbound consumer messages), the marketing unsubscribe window, contact restrictions from
--    inbound messages; S7's consumer port decides those (D5: disabled in simulation until then).
-- Real and test behave exactly as before: world_now is clock_timestamp() and wall_at is the identity there.

-- 1. The clock -----------------------------------------------------------------------------------------------------------------
alter table control.nexloop_sim_worlds add constraint nexloop_sim_worlds_world_tenant unique(world,tenant_id);
create table control.nexloop_sim_clocks (
 world text primary key,
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 sim_at timestamptz not null,          -- the world time at anchor_at
 anchor_at timestamptz not null,       -- the wall time of the last change
 rate numeric(12,3) not null check(rate>0 and rate<=100000),
 paused boolean not null,
 revision bigint not null check(revision>=1),
 foreign key(world,tenant_id) references control.nexloop_sim_worlds(world,tenant_id));
create table control.nexloop_sim_clock_events (
 world text not null,
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 revision bigint not null check(revision>=1),
 operation text not null check(operation in ('created','advance','pause','resume','set_rate')),
 from_at timestamptz,to_at timestamptz not null,check(from_at is null or to_at>=from_at),
 rate numeric(12,3) not null,paused boolean not null,seconds bigint check(seconds is null or seconds>=1),
 principal_id text not null,request_id text check(char_length(request_id) between 1 and 200),request_intent text,reason text,
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(world,revision),unique(world,request_id),
 foreign key(world,tenant_id) references control.nexloop_sim_worlds(world,tenant_id));
do $$declare t text;begin
 foreach t in array array['nexloop_sim_clocks','nexloop_sim_clock_events'] loop
  execute format('alter table control.%I owner to nexloop_owner',t);
  execute format('alter table control.%I enable row level security',t);
  execute format('alter table control.%I force row level security',t);
  execute format('create policy tenant_boundary on control.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on control.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics',t);
 end loop;
end $$;

-- The world time a clock row shows at a wall time.
create function runtime.nexloop_sim_clock_at(c control.nexloop_sim_clocks,p_wall timestamptz) returns timestamptz
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when c.paused then c.sim_at else c.sim_at+(p_wall-c.anchor_at)*(c.rate::double precision) end
$$;
alter function runtime.nexloop_sim_clock_at(control.nexloop_sim_clocks,timestamptz) owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_clock_at(control.nexloop_sim_clocks,timestamptz) from public;

-- A clock changes only through the governed Action (its flag), one revision at a time, never backwards; it leaves only while its
-- world is being deleted as a whole (S2 purge). Its events are append-only and leave the same way.
create function control.nexloop_sim_clock_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='TRUNCATE' then raise exception 'simulation clock leaves with its world only' using errcode='42501';end if;
 if tg_op='DELETE' then
  if control.nexloop_sim_world_status(old.tenant_id,old.world)='deleting' then return old;end if;
  raise exception 'simulation clock leaves with its world only' using errcode='42501';
 end if;
 if tg_table_name='nexloop_sim_clock_events' then raise exception 'simulation clock events are append-only' using errcode='42501';end if;
 if coalesce(current_setting('nexloop.sim_clock_change',true),'')<>'on'
  or (new.world,new.tenant_id) is distinct from (old.world,old.tenant_id) or new.revision<>old.revision+1
  or new.anchor_at<old.anchor_at or new.sim_at<runtime.nexloop_sim_clock_at(old,new.anchor_at) then
  raise exception 'simulation clock moves forward only, through the governed Action' using errcode='42501';end if;
 return new;
end $$;
alter function control.nexloop_sim_clock_guard() owner to nexloop_owner;
revoke all on function control.nexloop_sim_clock_guard() from public;
create trigger nx041_sim_clock_guard before update or delete on control.nexloop_sim_clocks for each row execute function control.nexloop_sim_clock_guard();
create trigger nx041_sim_clock_no_truncate before truncate on control.nexloop_sim_clocks for each statement execute function control.nexloop_sim_clock_guard();
create trigger nx041_sim_clock_events_guard before update or delete on control.nexloop_sim_clock_events for each row execute function control.nexloop_sim_clock_guard();
create trigger nx041_sim_clock_events_no_truncate before truncate on control.nexloop_sim_clock_events for each statement execute function control.nexloop_sim_clock_guard();

-- Every registered world gets its clock, paused at its creation time (revision 1).
create function control.nexloop_sim_clock_create() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_old text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 insert into control.nexloop_sim_clocks(world,tenant_id,sim_at,anchor_at,rate,paused,revision) values(new.world,new.tenant_id,new.created_at,new.created_at,1,true,1);
 insert into control.nexloop_sim_clock_events(world,tenant_id,revision,operation,from_at,to_at,rate,paused,principal_id,request_intent)
  values(new.world,new.tenant_id,1,'created',null,new.created_at,1,true,new.created_by,new.request_id);
 perform set_config('eios.tenant_id',coalesce(v_old,''),true);
 return new;
end $$;
alter function control.nexloop_sim_clock_create() owner to nexloop_owner;
revoke all on function control.nexloop_sim_clock_create() from public;
create trigger nx041_sim_clock_create after insert on control.nexloop_sim_worlds for each row execute function control.nexloop_sim_clock_create();

-- 2. Reading the time of a world -------------------------------------------------------------------------------------------------
create function runtime.nexloop_world_clock(p_tenant text,p_world text) returns control.nexloop_sim_clocks
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_old text:=current_setting('eios.tenant_id',true);c control.nexloop_sim_clocks;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select * into c from control.nexloop_sim_clocks where world=p_world and tenant_id=p_tenant;
 perform set_config('eios.tenant_id',coalesce(v_old,''),true);
 -- D5: a simulation world never falls back to the wall.
 if c.world is null then raise exception 'world clock unavailable' using errcode='55000';end if;
 return c;
end $$;
alter function runtime.nexloop_world_clock(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_world_clock(text,text) from public;

create function runtime.nexloop_world_now(p_tenant text,p_world text) returns timestamptz
 language plpgsql volatile set search_path=pg_catalog,pg_temp as $$
begin
 if coalesce(runtime.nexloop_world_kind(p_world),'') not in ('simulation','shadow') then return clock_timestamp();end if;
 return runtime.nexloop_sim_clock_at(runtime.nexloop_world_clock(p_tenant,p_world),clock_timestamp());
end $$;
create function runtime.nexloop_world_wall_at(p_tenant text,p_world text,p_at timestamptz) returns timestamptz
 language plpgsql volatile set search_path=pg_catalog,pg_temp as $$
declare c control.nexloop_sim_clocks;v_wall timestamptz:=clock_timestamp();
begin
 if p_at is null or coalesce(runtime.nexloop_world_kind(p_world),'') not in ('simulation','shadow') then return p_at;end if;
 c:=runtime.nexloop_world_clock(p_tenant,p_world);
 if p_at<=runtime.nexloop_sim_clock_at(c,v_wall) then return v_wall;end if;
 if c.paused then return 'infinity'::timestamptz;end if;
 return v_wall+(p_at-runtime.nexloop_sim_clock_at(c,v_wall))/(c.rate::double precision);
end $$;
do $$declare f text;begin
 foreach f in array array['runtime.nexloop_world_now(text,text)','runtime.nexloop_world_wall_at(text,text,timestamptz)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public';
 end loop;
end $$;

-- 3. The governed owner Action ----------------------------------------------------------------------------------------------------
create function control.nexloop_governed_control_sim_clock(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_target text:=body->>'world';v_op text:=body->>'action';c control.nexloop_sim_clocks;n control.nexloop_sim_clocks;e control.nexloop_sim_clock_events;
 v_wall timestamptz:=clock_timestamp();v_from timestamptz;v_seconds bigint;v_rate numeric;v_requeued integer;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','world','action','seconds','rate','reason'))
  or jsonb_typeof(body->'world') is distinct from 'string' or v_op is null or v_op not in ('advance','pause','resume','set_rate')
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(btrim(body->>'reason')) not between 1 and 500
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or (v_op='advance') is distinct from (body ? 'seconds') or (v_op='set_rate') is distinct from (body ? 'rate')
  or (v_op='advance' and (jsonb_typeof(body->'seconds') is distinct from 'number' or (body->>'seconds')!~'^[0-9]{1,9}$'
      or (body->>'seconds')::bigint not between 1 and 315360000))
  or (v_op='set_rate' and (jsonb_typeof(body->'rate') is distinct from 'number' or (body->>'rate')!~'^[0-9]{1,6}(\.[0-9]{1,3})?$'
      or (body->>'rate')::numeric<=0 or (body->>'rate')::numeric>100000)) then
  raise exception 'sim clock: invalid' using errcode='22023';end if;
 -- Real and test run on the wall: they have no clock to set.
 if coalesce(runtime.nexloop_world_kind(v_target),'') not in ('simulation','shadow') then raise exception 'sim clock: not_a_simulation_world' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'sim clock: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if control.nexloop_sim_world_status(p_tenant,v_target) is distinct from 'active' then raise exception 'sim clock: world_unavailable' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into c from control.nexloop_sim_clocks where world=v_target and tenant_id=p_tenant for update;
 if not found then raise exception 'sim clock: world_unavailable' using errcode='22023';end if;
 -- Idempotent per request: a retry answers the change it made.
 select * into e from control.nexloop_sim_clock_events where world=v_target and request_id=body->>'request_id';
 if found then
  if e.operation<>v_op or e.seconds is distinct from (case when v_op='advance' then (body->>'seconds')::bigint end) then
   raise exception 'sim clock: request_reused' using errcode='22023';end if;
  return jsonb_build_object('world',v_target,'revision',e.revision,'action',e.operation,'world_time',runtime.nexloop_commercial_ts(e.to_at),
   'paused',e.paused,'rate',e.rate,'changed',false);
 end if;
 v_from:=runtime.nexloop_sim_clock_at(c,v_wall);
 if v_op='pause' and c.paused then raise exception 'sim clock: already_paused' using errcode='22023';end if;
 if v_op='resume' and not c.paused then raise exception 'sim clock: already_running' using errcode='22023';end if;
 v_seconds:=case when v_op='advance' then (body->>'seconds')::bigint end;
 v_rate:=case when v_op='set_rate' then (body->>'rate')::numeric else c.rate end;
 n:=c;n.anchor_at:=v_wall;n.sim_at:=v_from+make_interval(secs=>coalesce(v_seconds,0));n.rate:=v_rate;n.revision:=c.revision+1;
 n.paused:=case v_op when 'pause' then true when 'resume' then false else c.paused end;
 perform set_config('nexloop.sim_clock_change','on',true);
 update control.nexloop_sim_clocks set sim_at=n.sim_at,anchor_at=n.anchor_at,rate=n.rate,paused=n.paused,revision=n.revision where world=v_target;
 perform set_config('nexloop.sim_clock_change','',true);
 insert into control.nexloop_sim_clock_events(world,tenant_id,revision,operation,from_at,to_at,rate,paused,seconds,principal_id,request_id,request_intent,reason)
  values(v_target,p_tenant,n.revision,v_op,v_from,n.sim_at,n.rate,n.paused,v_seconds,p_principal,body->>'request_id',p_intent,btrim(body->>'reason'));
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('sim-clock:'||v_target||':'||n.revision,'nexloop.sim_clock.'||v_op,p_tenant,p_principal,'sim_clock',p_intent,
   jsonb_build_object('world',v_target,'revision',n.revision,'from',runtime.nexloop_commercial_ts(v_from),'to',runtime.nexloop_commercial_ts(n.sim_at),
    'rate',n.rate,'paused',n.paused,'seconds',v_seconds),v_wall);
 -- Items waiting for a world time are scheduled on the wall; after any change they are re-evaluated against the new clock.
 update runtime.nexloop_work_feed set available_at=v_wall
  where tenant_id=p_tenant and world=v_target and feed in ('commitment-monitor','plan-reevaluate') and status='pending' and available_at>v_wall;
 get diagnostics v_requeued=row_count;
 return jsonb_build_object('world',v_target,'revision',n.revision,'action',v_op,'world_time',runtime.nexloop_commercial_ts(n.sim_at),
  'paused',n.paused,'rate',n.rate,'changed',true,'requeued',v_requeued);
end $$;
alter function control.nexloop_governed_control_sim_clock(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_control_sim_clock(text,text,text,text,text,jsonb) from public;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('sim_clock.control','control_sim_clock','human','control.nexloop_governed_control_sim_clock(text,text,text,text,text,jsonb)','NX-041');

-- A simulation world's outreach sends and approvals are created at the world time (the column defaults are the wall).
create function runtime.nexloop_sim_clock_stamp() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if coalesce(runtime.nexloop_world_kind(new.world),'') not in ('simulation','shadow') then return new;end if;
 if tg_table_name='nexloop_outreach_sends' then new.created_at:=runtime.nexloop_world_now(new.tenant_id,new.world);
 else new.requested_at:=runtime.nexloop_world_now(new.tenant_id,new.world);end if;
 return new;
end $$;
alter function runtime.nexloop_sim_clock_stamp() owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_clock_stamp() from public;
create trigger nx041_sim_clock_stamp before insert on runtime.nexloop_outreach_sends for each row execute function runtime.nexloop_sim_clock_stamp();
create trigger nx041_sim_clock_stamp before insert on runtime.nexloop_outreach_approvals for each row execute function runtime.nexloop_sim_clock_stamp();

-- 4. The time-dependent logic (current bodies; only the time expressions change) ----------------------------------------------------
-- Commitments (D5): evaluated at the world time; the monitor item waits until the wall moment that time is reached.
CREATE OR REPLACE FUNCTION authz.nexloop_commitment_command(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-commitment-keep-v1','eios:action:nexloop.commitment.keep:1',
  array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;r runtime.nexloop_commitments;o ontology.objects;v_patch jsonb;v_status text;v_last text;
 v_due timestamptz;v_lead integer;v_next timestamptz;v_now timestamptz:=runtime.nexloop_world_now(t,p_world);v_subject text;v_restricted boolean;f runtime.nexloop_work_feed;
begin
 perform set_config('eios.tenant_id',t,true);
 if c->>'verb'='prepare' then
  if c ? 'claim_id' then
   if coalesce(c->>'claim_id','')!~'^[0-9a-f]{64}$' then raise exception 'commitment command invalid' using errcode='22023';end if;
   return runtime.nexloop_commitment_prepare_claim(t,p_world,c->>'claim_id');
  end if;
  if coalesce(c->>'commitment_id','')!~'^[0-9a-f]{64}$' then raise exception 'commitment command invalid' using errcode='22023';end if;
  select * into r from runtime.nexloop_commitments where tenant_id=t and world=p_world and commitment_id=c->>'commitment_id';
  if not found then raise exception 'commitment unavailable' using errcode='42501';end if;
  if r.registered_at is not null then return jsonb_build_object('action','registered','commitment_id',r.commitment_id);end if;
  return jsonb_build_object('action','create','commitment_id',r.commitment_id,'intent_id',r.intent_id,'properties',r.properties);
 end if;
 if coalesce(c->>'commitment_id','')!~'^[0-9a-f]{64}$' then raise exception 'commitment command invalid' using errcode='22023';end if;
 select * into r from runtime.nexloop_commitments where tenant_id=t and world=p_world and commitment_id=c->>'commitment_id' for update;
 if not found then raise exception 'commitment unavailable' using errcode='42501';end if;
 v_subject:='commitment:'||r.commitment_id;
 select * into o from ontology.objects where tenant_id=t and world=p_world and type_name='Commitment' and object_id=r.commitment_id for share;
 if c->>'verb'='registered' then
  if o.object_id is null or o.properties is distinct from r.properties then raise exception 'commitment not created as prepared' using errcode='40001';end if;
  if r.registered_at is not null then return jsonb_build_object('commitment_id',r.commitment_id,'replay',true);end if;
  update runtime.nexloop_commitments set registered_at=v_now where tenant_id=t and world=p_world and commitment_id=r.commitment_id;
  insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref,detail)
   values(t,p_world,v_subject,'registered',coalesce('claim:'||r.claim_id,'commitment:'||r.supersedes),jsonb_build_object('origin',r.origin,'status',r.properties->>'status'));
  if r.claim_id is not null then
   update ontology.nexloop_claims set resolution_state='resolved' where tenant_id=t and world=p_world and claim_id=r.claim_id and resolution_state in ('unresolved','needs_resolution');
  end if;
  if r.supersedes is not null then
   insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref)
    values(t,p_world,'commitment:'||r.supersedes,'superseded',v_subject);
   perform runtime.nexloop_commitment_touch(t,p_world,r.supersedes,v_now);
  end if;
  if exists(select 1 from control.nexloop_contact_restrictions x where x.tenant_id=t and x.world=p_world and x.consumer_id=r.consumer_id and x.active) and r.origin='claim' then
   perform runtime.nexloop_commitment_raise(t,p_world,v_subject,r.consumer_id,'made_under_contact_restriction',jsonb_build_object('message_id',r.message_id));
  end if;
  if r.claim_id is not null and exists(select 1 from ontology.nexloop_claims cl where cl.tenant_id=t and cl.world=p_world and cl.claim_id=r.claim_id
    and cl.valid_time->>'kind'='past_reference') then
   perform runtime.nexloop_commitment_raise(t,p_world,v_subject,r.consumer_id,'due_in_past',jsonb_build_object('claim_id',r.claim_id));
  end if;
  -- Without a due date the plans are marked now: the business rule is to clarify it (docs/04 §8).
  if r.properties->>'due_at' is null then perform runtime.nexloop_commitment_mark_plans(t,p_world,r.commitment_id,r.consumer_id,null,'clarify_due');end if;
  perform runtime.nexloop_commitment_touch(t,p_world,r.commitment_id,v_now);
  return jsonb_build_object('commitment_id',r.commitment_id,'replay',false);
 elsif c->>'verb'='exception' then
  if coalesce(c->>'reason','')!~'^[a-z_]{1,64}$' or jsonb_typeof(c->'detail') is distinct from 'object' then raise exception 'commitment command invalid' using errcode='22023';end if;
  return jsonb_build_object('raised',runtime.nexloop_commitment_raise(t,p_world,v_subject,r.consumer_id,c->>'reason',c->'detail'));
 end if;
 if o.object_id is null or r.registered_at is null then raise exception 'commitment not registered' using errcode='40001';end if;
 if c->>'verb'='evaluate' then
  v_patch:=runtime.nexloop_commitment_target(t,p_world,r.commitment_id,o.properties,v_now);
  return jsonb_build_object('commitment_id',r.commitment_id,'revision',o.nexloop_revision,'status',o.properties->>'status','patch',v_patch);
 elsif c->>'verb'='settle' then
  v_status:=o.properties->>'status';
  select e.detail->>'status' into v_last from runtime.nexloop_commitment_events e where e.tenant_id=t and e.world=p_world and e.subject_ref=v_subject
   and e.kind in ('status','registered') order by e.event_number desc limit 1;
  if v_last is distinct from v_status then
   insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,detail)
    values(t,p_world,v_subject,'status',jsonb_build_object('status',v_status,'from',v_last,'late',o.properties->'late','revision',o.nexloop_revision,
     'fulfillment_evidence',o.properties->'fulfillment_evidence'));
   if v_status='breached' then
    v_restricted:=exists(select 1 from control.nexloop_contact_restrictions x where x.tenant_id=t and x.world=p_world and x.consumer_id=r.consumer_id and x.active);
    perform runtime.nexloop_commitment_raise(t,p_world,v_subject,r.consumer_id,'breached',jsonb_build_object('due_at',o.properties->>'due_at',
     'contact_restricted',v_restricted,'paused',exists(select 1 from control.nexloop_control_scopes s where s.tenant_id=t and s.world=p_world and s.paused
      and ((s.scope_kind='consumer' and s.scope_ref=r.consumer_id) or s.scope_kind='tenant'))));
    perform runtime.nexloop_commitment_mark_plans(t,p_world,r.commitment_id,r.consumer_id,(o.properties->>'due_at')::timestamptz,'breached');
   end if;
  end if;
  v_due:=(o.properties->>'due_at')::timestamptz;v_lead:=runtime.nexloop_commitment_setting('lead_seconds');
  if v_status in ('open','in_progress') then
   -- A fulfilment intent that cannot dispatch to a restricted Consumer (ADR-023): owner-visible.
   if exists(select 1 from control.nexloop_contact_restrictions x where x.tenant_id=t and x.world=p_world and x.consumer_id=r.consumer_id and x.active)
    and exists(select 1 from runtime.nexloop_commitment_evidence q join runtime.nexloop_effect_intents i on 'intent:'||i.intent_id=q.ref
      and i.tenant_id=q.tenant_id and i.world=q.world
     where q.tenant_id=t and q.world=p_world and q.commitment_id=r.commitment_id and q.kind='requested' and i.state not in ('fulfilled','confirmed')
      and runtime.nexloop_commitment_effect_kind(t,p_world,i.intent_id)='delivered_message') then
    perform runtime.nexloop_commitment_raise(t,p_world,v_subject,r.consumer_id,'blocked_by_contact_restriction',jsonb_build_object('due_at',o.properties->>'due_at'));
   end if;
   if v_due is not null then
    if v_now>=v_due-make_interval(secs=>v_lead) and not exists(select 1 from runtime.nexloop_commitment_events e
      where e.tenant_id=t and e.world=p_world and e.subject_ref=v_subject and e.kind='due_soon') then
     insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,detail) values(t,p_world,v_subject,'due_soon',jsonb_build_object('due_at',o.properties->>'due_at'));
     perform runtime.nexloop_commitment_mark_plans(t,p_world,r.commitment_id,r.consumer_id,v_due,'due_soon');
    end if;
    v_next:=case when v_now<v_due-make_interval(secs=>v_lead) then v_due-make_interval(secs=>v_lead) else v_due end;
   end if;
  elsif v_status='conditional' and v_due is not null then
   if v_now>=v_due then
    if runtime.nexloop_commitment_raise(t,p_world,v_subject,r.consumer_id,'condition_unresolved_at_due',jsonb_build_object('due_at',o.properties->>'due_at')) then
     perform runtime.nexloop_commitment_mark_plans(t,p_world,r.commitment_id,r.consumer_id,v_due,'condition_unresolved');
    end if;
   else v_next:=v_due;end if;
  end if;
  if v_status in ('conditional','open','in_progress') and v_due is null then
   if v_now>=r.registered_at+make_interval(secs=>runtime.nexloop_commitment_setting('unspecified_max_open_seconds')) then
    perform runtime.nexloop_commitment_raise(t,p_world,v_subject,r.consumer_id,'no_due_date',jsonb_build_object('registered_at',r.registered_at));
   else v_next:=r.registered_at+make_interval(secs=>runtime.nexloop_commitment_setting('unspecified_max_open_seconds'));end if;
  end if;
  -- Next stage of this commitment's monitor item; an item changed while being worked on stays due now.
  if v_next is not null then
   select * into f from runtime.nexloop_work_feed where tenant_id=t and world=p_world and feed='commitment-monitor' and item_key=v_subject for update;
   if not found then
    insert into runtime.nexloop_work_feed(tenant_id,world,feed,item_key,payload,available_at)
     values(t,p_world,'commitment-monitor',v_subject,jsonb_build_object('commitment_id',r.commitment_id),runtime.nexloop_world_wall_at(t,p_world,v_next));
   elsif f.lease_seq is not distinct from (c->>'fence')::bigint and f.change_seq=f.lease_seq then
    update runtime.nexloop_work_feed set available_at=runtime.nexloop_world_wall_at(t,p_world,v_next),change_seq=change_seq+1,changed_at=clock_timestamp()
     where tenant_id=t and world=p_world and feed='commitment-monitor' and item_key=v_subject;
   end if;
  end if;
  return jsonb_build_object('commitment_id',r.commitment_id,'status',v_status,'next_at',v_next);
 end if;
 raise exception 'commitment command invalid' using errcode='22023';
end $function$;

-- Commitment monitor item: a due time of the world is scheduled at its wall moment.
CREATE OR REPLACE FUNCTION runtime.nexloop_commitment_touch(p_tenant text, p_world text, p_commitment text, p_at timestamp with time zone)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare prior text:=current_setting('eios.tenant_id',true);due timestamptz:=greatest(coalesce(runtime.nexloop_world_wall_at(p_tenant,p_world,p_at),clock_timestamp()),clock_timestamp());
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.nexloop_work_feed as f(tenant_id,world,feed,item_key,payload,available_at)
  values(p_tenant,p_world,'commitment-monitor','commitment:'||p_commitment,jsonb_build_object('commitment_id',p_commitment),due)
 on conflict(tenant_id,world,feed,item_key) do update set change_seq=f.change_seq+1,changed_at=clock_timestamp(),
  available_at=case when f.status='pending' then least(f.available_at,due) else due end,attempts=0,status='pending',last_code=null;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
end $function$;

-- Plan re-evaluation (D5): the reassessment is due by the world time.
CREATE OR REPLACE FUNCTION runtime.nexloop_plan_mark_due(p_tenant text, p_world text, p_plan uuid, p_version integer, p_cause text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare due timestamptz;settings jsonb;
begin
 select p.settings into settings from runtime.nexloop_plans p where p.tenant_id=p_tenant and p.world=p_world and p.plan_id=p_plan and p.version=p_version;
 select min(s.reassess_at) into due from runtime.nexloop_plan_steps s where s.tenant_id=p_tenant and s.world=p_world and s.plan_id=p_plan and s.version=p_version and s.reassess_at is not null;
 if due is null then return;end if;
 due:=greatest(due,runtime.nexloop_world_now(p_tenant,p_world)+make_interval(secs=>(settings->>'min_reassess_seconds')::integer));
 perform authz.nexloop_plan_feed_touch(p_tenant,p_world,p_plan,jsonb_build_object('kind','reassess_due','cause',p_cause,'due',due,'at',runtime.nexloop_world_now(p_tenant,p_world)),due);
end $function$;

-- Plan re-evaluation item: a due time of the world is scheduled at its wall moment.
CREATE OR REPLACE FUNCTION authz.nexloop_plan_feed_touch(p_tenant text, p_world text, p_plan uuid, p_trigger jsonb, p_due timestamp with time zone)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare prior text:=current_setting('eios.tenant_id',true);due timestamptz:=greatest(coalesce(runtime.nexloop_world_wall_at(p_tenant,p_world,p_due),clock_timestamp()),clock_timestamp());
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.nexloop_work_feed as f(tenant_id,world,feed,item_key,payload,available_at)
  values(p_tenant,p_world,'plan-reevaluate','plan:'||p_plan,jsonb_build_object('plan_id',p_plan,'triggers',jsonb_build_array(p_trigger)),due)
 on conflict(tenant_id,world,feed,item_key) do update set
  payload=jsonb_build_object('plan_id',p_plan,'triggers',(select coalesce(jsonb_agg(x order by n desc),'[]'::jsonb) from (select x,n from jsonb_array_elements(f.payload->'triggers'||jsonb_build_array(p_trigger)) with ordinality t(x,n) order by n desc limit 16) s)),
  change_seq=f.change_seq+1,changed_at=clock_timestamp(),
  available_at=case when f.status='pending' then least(f.available_at,due) else due end,attempts=0,status='pending',last_code=null;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
end $function$;

-- Outreach acceptance: the day in the business intent and the approval expiry are world times (the audit time stays the wall).
CREATE OR REPLACE FUNCTION runtime.nexloop_outreach_accept(p_tenant text, p_world text, p_consumer text, p_purpose text, p_origin text, p_body text, p_conversation text, p_initiator text, p_principal text, p_intent text, p_run uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare v_purpose jsonb;v_id text;v_digest text;v_head bigint;old runtime.nexloop_outreach_sends;v_day text;
 v_approval boolean;  -- Outreach slice 6
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=p_consumer)
  or runtime.nexloop_consumer_erasing(p_tenant,p_world,p_consumer) then raise exception 'outreach send: consumer_unavailable' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if p_conversation is not null and not exists(select 1 from runtime.nexloop_conversations cv where cv.tenant_id=p_tenant and cv.world=p_world
   and cv.conversation_id=p_conversation and cv.consumer_id=p_consumer) then raise exception 'outreach send: conversation_unavailable' using errcode='22023';end if;
 -- Topic, version and frequency class are frozen from the current list; whether the purpose is enabled is a dispatch gate.
 v_purpose:=runtime.nexloop_outreach_purpose(p_tenant,p_purpose);
 if not (v_purpose->>'listed')::boolean then raise exception 'outreach send: purpose_not_allowed' using errcode='22023';end if;
 if char_length(p_body)>(v_purpose->'definition'->'content_rules'->>'max_chars')::integer then raise exception 'outreach send: body_too_long' using errcode='22023';end if;
 -- Outreach slice 6 (P5): a purpose configured to need approval waits for it, whoever asked (owner or reevaluation Run); nothing is admitted before.
 v_approval:=coalesce((v_purpose->'definition'->>'requires_approval')::boolean,false);
 v_day:=to_char(runtime.nexloop_world_now(p_tenant,p_world) at time zone (runtime.nexloop_outreach_settings(p_tenant)->>'time_zone'),'YYYY-MM-DD');
 v_id:='outreach:'||encode(sha256(convert_to(jsonb_build_array(p_tenant,p_consumer,p_purpose,p_origin,v_day)::text,'UTF8')),'hex');
 v_digest:=encode(sha256(convert_to(p_body,'UTF8')),'hex');
 select * into old from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and intent_id=v_id;
 if found then
  if old.body_digest is distinct from v_digest or old.conversation_id is distinct from p_conversation then
   raise exception 'outreach send: intent_exists' using errcode='22023';end if;
  return jsonb_build_object('intent_id',old.intent_id,'state',old.state,'replayed',true);
 end if;
 select revision into v_head from control.nexloop_control_heads where tenant_id=p_tenant and world=p_world;
 insert into runtime.nexloop_outreach_sends(tenant_id,world,intent_id,consumer_id,purpose_id,purpose_version,topic,frequency_class,channel,origin_ref,conversation_id,
   body,body_digest,initiator,principal_id,request_intent,control_snapshot,run_id,state)
  values(p_tenant,p_world,v_id,p_consumer,p_purpose,(v_purpose->>'list_version')::integer,v_purpose->'definition'->>'topic',
   v_purpose->'definition'->>'frequency_class','native.webchat',p_origin,p_conversation,p_body,v_digest,p_initiator,p_principal,p_intent,
   jsonb_build_object('control_revision',coalesce(v_head,0),
    'scopes',jsonb_build_array(jsonb_build_object('kind','consumer','ref',p_consumer),jsonb_build_object('kind','consumer','ref','consumer:'||p_consumer),
      jsonb_build_object('kind','action_type','ref','nexloop.outreach.send')),'goals','[]'::jsonb,'objects','[]'::jsonb,'budgets','[]'::jsonb),p_run,
   case when v_approval then 'pending_approval' else 'accepted' end);
 if v_approval then  -- Outreach slice 6: the approval window starts at acceptance
  insert into runtime.nexloop_outreach_approvals(tenant_id,world,intent_id,requested_by,expires_at)
   values(p_tenant,p_world,v_id,p_principal,runtime.nexloop_world_now(p_tenant,p_world)+runtime.nexloop_outreach_approval_window());
 end if;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('outreach-send:'||v_id,'nexloop.outreach.send.accepted',p_tenant,p_principal,'outreach',p_intent,
   jsonb_build_object('intent_id',v_id,'purpose',p_purpose,'origin_ref',p_origin,'initiator',p_initiator),clock_timestamp()) on conflict do nothing;
 return jsonb_build_object('intent_id',v_id,'state',case when v_approval then 'pending_approval' else 'accepted' end,'replayed',false);
end $function$;

-- Outreach approval: expiry and decision times are world times.
CREATE OR REPLACE FUNCTION control.nexloop_governed_outreach_approve(p_tenant text, p_world text, p_principal text, p_subject text, p_intent text, body jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare s runtime.nexloop_outreach_sends;a runtime.nexloop_outreach_approvals;cfg control.nexloop_workbench_configurations;
 v_resource text:='eios:action:nexloop.outreach.approve:1';v_decision text;v_intent text:=body->>'intent_id';
begin
 if jsonb_typeof(body) is distinct from 'object'
  or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','intent_id','decision','reason'))
  or coalesce(body->>'intent_id','')!~'^outreach:[0-9a-f]{64}$' or body->>'decision' not in ('approve','reject') or body->>'decision' is null
  or jsonb_typeof(body->'reason') is distinct from 'string' or char_length(body->>'reason') not between 1 and 500 then
  raise exception 'outreach approval: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into s from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and intent_id=v_intent for update;
 select * into a from runtime.nexloop_outreach_approvals where tenant_id=p_tenant and world=p_world and intent_id=v_intent for update;
 if s.intent_id is null or a.intent_id is null then raise exception 'outreach approval: not_pending' using errcode='22023';end if;
 v_decision:=case body->>'decision' when 'approve' then 'approved' else 'rejected' end;
 -- The same decision by the same approver replays; any other decision on a decided approval is refused.
 if a.decision is not null then
  if a.decision=v_decision and a.decided_by=p_principal then return jsonb_build_object('intent_id',s.intent_id,'state',s.state,'decision',a.decision,'replayed',true);end if;
  raise exception 'outreach approval: not_pending' using errcode='22023';end if;
 -- Four eyes: the requester never decides their own request.
 if p_principal=s.principal_id or p_principal=a.requested_by then raise exception 'outreach approval: self_approval' using errcode='22023';end if;
 -- The approver's authority now: an active member whose current workbench role holds the Action.
 cfg:=control.nexloop_workbench_current(p_tenant);
 if not exists(select 1 from control.nexloop_browser_memberships m where m.tenant_id=p_tenant and m.principal_id=p_principal and m.payload->>'status'='active')
  or cfg.tenant_id is null or not coalesce(cfg.roles->(cfg.members->>p_principal) ? v_resource,false) then
  raise exception 'outreach approval: approver_unauthorized' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if a.expires_at<=runtime.nexloop_world_now(p_tenant,p_world) then
  -- Past its window: the send ends expired (terminal) and the decision is not taken.
  perform runtime.nexloop_outreach_expire_approvals(p_tenant,p_world);
  return jsonb_build_object('intent_id',s.intent_id,'state','refused','refusal_code','approval_expired','replayed',false);
 end if;
 update runtime.nexloop_outreach_approvals set decision=v_decision,decided_by=p_principal,decided_at=runtime.nexloop_world_now(p_tenant,p_world),reason=body->>'reason',decision_intent=p_intent
  where tenant_id=p_tenant and world=p_world and intent_id=s.intent_id;
 if v_decision='approved' then
  -- Approved is only accepted: the executor still admits it through every dispatch gate.
  update runtime.nexloop_outreach_sends set state='accepted',updated_at=clock_timestamp() where tenant_id=p_tenant and world=p_world and intent_id=s.intent_id;
 else
  update runtime.nexloop_outreach_sends set state='refused',refusal_code='approval_rejected',finished_at=runtime.nexloop_world_now(p_tenant,p_world),updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and intent_id=s.intent_id;
 end if;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('outreach-approval:'||s.intent_id,'nexloop.outreach.approval.'||v_decision,p_tenant,p_principal,'outreach',p_intent,
   jsonb_build_object('intent_id',s.intent_id,'decision',v_decision),clock_timestamp()) on conflict do nothing;
 return jsonb_build_object('intent_id',s.intent_id,'state',case when v_decision='approved' then 'accepted' else 'refused' end,'decision',v_decision,'replayed',false);
end $function$;

-- Outreach approval expiry by the world time.
CREATE OR REPLACE FUNCTION runtime.nexloop_outreach_expire_approvals(p_tenant text, p_world text)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare n integer:=0;r record;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 for r in select a.intent_id from runtime.nexloop_outreach_approvals a join runtime.nexloop_outreach_sends s
   on s.tenant_id=a.tenant_id and s.world=a.world and s.intent_id=a.intent_id
  where a.tenant_id=p_tenant and a.world=p_world and a.decision is null and a.expires_at<=runtime.nexloop_world_now(p_tenant,p_world) and s.state='pending_approval'
  order by a.expires_at limit 200 for update of a,s skip locked loop
  update runtime.nexloop_outreach_approvals set decision='expired',decided_at=runtime.nexloop_world_now(p_tenant,p_world) where tenant_id=p_tenant and world=p_world and intent_id=r.intent_id;
  update runtime.nexloop_outreach_sends set state='refused',refusal_code='approval_expired',finished_at=runtime.nexloop_world_now(p_tenant,p_world),updated_at=clock_timestamp()
   where tenant_id=p_tenant and world=p_world and intent_id=r.intent_id;
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('outreach-approval-expired:'||r.intent_id,'nexloop.outreach.approval.expired',p_tenant,'service:outreach','outreach',r.intent_id,
    jsonb_build_object('intent_id',r.intent_id),clock_timestamp()) on conflict do nothing;
  n:=n+1;
 end loop;
 return n;
end $function$;

-- Outreach metrics: the 7-day windows end at the world time.
CREATE OR REPLACE FUNCTION runtime.nexloop_outreach_metrics(p_tenant text, p_world text)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare n timestamptz:=runtime.nexloop_world_now(p_tenant,p_world);since timestamptz:=n-interval '7 days';s jsonb;
 v_candidates integer;v_msent bigint;v_unsub bigint;v_cap integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 -- The tenant's settings under its own row security (read after the tenant context is set).
 s:=runtime.nexloop_outreach_settings(p_tenant);v_cap:=(s->'marketing'->>'per_7d')::integer;
 v_candidates:=jsonb_array_length(coalesce(runtime.nexloop_outreach_candidates(p_tenant,p_world,n)->'candidates','[]'::jsonb));
 perform set_config('eios.tenant_id',p_tenant,true);
 select count(*) into v_msent from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and frequency_class='marketing'
  and admitted_at>since and state in ('dispatching','unknown','fulfilled','failed');
 select count(*) into v_unsub from runtime.nexloop_marketing_optouts where tenant_id=p_tenant and world=p_world and kind='unsubscribed' and recorded_at>since;
 return jsonb_build_object('computed_at',n,'window_days',7,'candidates',v_candidates,
  'accepted_7d',(select count(*) from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and created_at>since),
  'pending_approval',(select count(*) from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and state='pending_approval'),
  'approvals_7d',coalesce((select jsonb_object_agg(coalesce(decision,'pending'),c) from (select decision,count(*) c from runtime.nexloop_outreach_approvals
    where tenant_id=p_tenant and world=p_world and requested_at>since group by decision) q),'{}'::jsonb),
  'sent_7d',(select count(*) from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and admitted_at>since
    and state in ('dispatching','unknown','fulfilled','failed')),
  'fulfilled_7d',(select count(*) from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and state='fulfilled' and finished_at>since),
  'refused_7d',coalesce((select jsonb_object_agg(refusal_code,c) from (select refusal_code,count(*) c from runtime.nexloop_outreach_sends
    where tenant_id=p_tenant and world=p_world and state='refused' and finished_at>since group by refusal_code) q),'{}'::jsonb),
  'by_purpose_7d',coalesce((select jsonb_object_agg(purpose_id,jsonb_build_object('accepted',a,'sent',sn,'refused',rf)) from (select purpose_id,count(*) a,
    count(*) filter (where admitted_at is not null and state in ('dispatching','unknown','fulfilled','failed')) sn,count(*) filter (where state='refused') rf
    from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and created_at>since group by purpose_id) q),'{}'::jsonb),
  'frequency_capped_7d',(select count(*) from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world and state='refused'
    and refusal_code='frequency_capped' and finished_at>since),
  'marketing',jsonb_build_object('enabled',(s->'marketing'->>'enabled')::boolean,'consent_mode',s->'marketing'->>'consent_mode','sent_7d',v_msent,
    'unsubscribes_7d',v_unsub,'min_volume',20,'unsubscribe_rate_7d',case when v_msent>=20 then round(v_unsub::numeric/v_msent,4) end,
    'allowance_per_7d',v_cap,
    'consumers_at_allowance',(select count(*) from (select consumer_id from runtime.nexloop_outreach_sends where tenant_id=p_tenant and world=p_world
      and frequency_class='marketing' and admitted_at>since and state in ('dispatching','unknown','fulfilled') group by consumer_id having count(*)>=greatest(v_cap,1)) q)),
  'consent',jsonb_build_object(
    'granted_7d',(select count(*) from runtime.nexloop_marketing_consents where tenant_id=p_tenant and world=p_world and kind='granted' and recorded_at>since),
    'withdrawn_7d',(select count(*) from runtime.nexloop_marketing_consents where tenant_id=p_tenant and world=p_world and kind='withdrawn' and recorded_at>since),
    'consumers_with_record',(select count(distinct consumer_id) from runtime.nexloop_marketing_consents where tenant_id=p_tenant and world=p_world),
    'consumers_consented',(select count(*) from (select distinct on (consumer_id) consumer_id,kind from runtime.nexloop_marketing_consents
      where tenant_id=p_tenant and world=p_world order by consumer_id,event_id desc) q where kind='granted'),
    'consent_rate',(select case when count(*)>0 then round((count(*) filter (where kind='granted'))::numeric/count(*),4) end from (select distinct on (consumer_id) kind
      from runtime.nexloop_marketing_consents where tenant_id=p_tenant and world=p_world order by consumer_id,event_id desc) q)));
end $function$;

-- Outreach dispatch: quiet hours and frequency gates at the world time; admission and finish are world times (leases, ordering and audit stay the wall).
CREATE OR REPLACE FUNCTION authz.nexloop_outreach_command(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-outreach-v1',
  'eios:action:nexloop.outreach.execute:1',array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;v jsonb;i runtime.nexloop_outreach_sends;
 v_limit integer;v_code text;v_state text;g jsonb;x jsonb;cfg control.nexloop_workbench_configurations;v_purpose jsonb;v_text text;
 v_resource text:='eios:action:nexloop.outreach.send:1';v_approved boolean;  -- Outreach slice 6
begin
 perform set_config('eios.tenant_id',t,true);
 v_limit:=coalesce((c->>'limit')::integer,50);
 if v_limit not between 1 and 200 then raise exception 'outreach command invalid' using errcode='22023';end if;
 if c->>'verb'='due' then
  perform runtime.nexloop_outreach_expire_approvals(t,p_world);  -- Outreach slice 6: an approval past its window ends the intent
  select coalesce(jsonb_agg(jsonb_build_object('intent_id',x2.intent_id,'state',x2.state) order by x2.updated_at),'[]'::jsonb) into v
   from (select intent_id,state,updated_at from runtime.nexloop_outreach_sends where tenant_id=t and world=p_world
     and (state in ('accepted','unknown') or (state='dispatching' and lease_until<=clock_timestamp())) order by updated_at limit v_limit) x2;
  return jsonb_build_object('intents',v);
 end if;
 if coalesce(c->>'intent_id','')!~'^outreach:[0-9a-f]{64}$' then raise exception 'outreach command invalid' using errcode='22023';end if;
 select * into i from runtime.nexloop_outreach_sends where tenant_id=t and world=p_world and intent_id=c->>'intent_id' for update;
 if not found then raise exception 'outreach intent unavailable' using errcode='42501';end if;
 if c->>'verb'='admit' then
  -- Only an accepted intent is ever admitted; dispatching / unknown are queried, never sent again.
  if i.state<>'accepted' then return jsonb_build_object('intent_id',i.intent_id,'admitted',false,'state',i.state,'fence',i.fence);end if;
  -- One admission per consumer at a time: the frequency counts below cannot race.
  perform pg_advisory_xact_lock(hashtextextended('nexloop-outreach:'||t||':'||p_world||':'||i.consumer_id,0));
  begin
   if runtime.nexloop_consumer_erasing(t,p_world,i.consumer_id) then raise exception 'erasing' using errcode='42501',hint='consumer_erasing';end if;
   perform set_config('eios.tenant_id',t,true);
   -- ADR-023: the contact restriction first; a proactive send to a restricted customer has no exception (P6).
   if exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=t and r.world=p_world and r.consumer_id=i.consumer_id and r.active) then
    raise exception 'restricted' using errcode='42501',hint='contact_restricted';end if;
   -- The requester's authority now, not at acceptance: an active member whose current workbench role still holds the Action.
   cfg:=control.nexloop_workbench_current(t);
   if i.initiator='human' and (not exists(select 1 from control.nexloop_browser_memberships m where m.tenant_id=t and m.principal_id=i.principal_id and m.payload->>'status'='active')
    or (cfg.tenant_id is not null and not coalesce(cfg.roles->(cfg.members->>i.principal_id) ? v_resource,false))) then
    raise exception 'requester' using errcode='42501',hint='requester_unauthorized';end if;
   -- Outreach slice 5: a reevaluation Run's request stands only while its Role's execution ceiling still holds the Action.
   if i.initiator='agent' and exists(select 1 from authz.nexloop_role_run_bindings rb where rb.run_id=i.run_id) and not exists(
     select 1 from authz.nexloop_role_run_bindings rb join ontology.objects d on d.tenant_id=t and d.world=p_world and d.type_name='RoleDefinition' and d.object_id=rb.role_id
      join ontology.objects o on o.tenant_id=d.tenant_id and o.world=d.world and o.type_name='RoleExecutionCeiling' and o.object_id=d.properties->>'ceiling_ref'
     where rb.run_id=i.run_id and coalesce(o.properties->'action_resources','[]'::jsonb) ? v_resource) then
    raise exception 'requester' using errcode='42501',hint='requester_unauthorized';end if;
   perform set_config('eios.tenant_id',t,true);
   begin
    perform authz.nexloop_assert_dispatch_controls(p_digest,p_world,i.control_snapshot);
   exception when sqlstate 'NXC01' or sqlstate 'NXC02' or sqlstate 'NXC04' then
    raise exception 'control' using errcode='42501',hint=case sqlstate when 'NXC01' then 'control_paused' when 'NXC02' then 'control_revision_stale' else 'object_revision_stale' end;
   end;
   perform set_config('eios.tenant_id',t,true);
   v_purpose:=runtime.nexloop_outreach_purpose(t,i.purpose_id);
   if (v_purpose->>'listed')::boolean and v_purpose->'definition'->>'topic' is distinct from i.topic then
    raise exception 'purpose' using errcode='42501',hint='purpose_changed';end if;
   g:=runtime.nexloop_outreach_gates(t,p_world,i.consumer_id,i.purpose_id,runtime.nexloop_world_now(t,p_world));
   -- Outreach slice 6: an approval lets the purpose gate pass for this intent only; every other gate is checked as before.
   v_approved:=exists(select 1 from runtime.nexloop_outreach_approvals ap where ap.tenant_id=t and ap.world=p_world and ap.intent_id=i.intent_id and ap.decision='approved');
   for x in select value from jsonb_array_elements(g) loop
    if x->>'result'<>'pass' and not (x->>'gate'='purpose' and x->>'detail'='approval_required' and v_approved) then
     raise exception 'gate' using errcode='42501',hint=case x->>'gate'
      when 'contact_restricted' then 'contact_restricted' when 'control_paused' then 'control_paused'
      when 'purpose' then x->>'detail'
      when 'marketing' then x->>'detail' when 'persona_excluded' then 'persona_excluded' when 'quiet_hours' then 'quiet_hours'
      when 'frequency' then 'frequency_capped' when 'consumer_preference' then x->>'detail' when 'real_dispatch' then 'real_dispatch_closed'
      when 'budget' then x->>'detail' else 'gate_refused' end;
    end if;
   end loop;
   -- A switch change after acceptance supersedes the request, as for every external effect.
   if exists(select 1 from control.nexloop_control_events e where e.tenant_id=t and e.world=p_world and e.event_kind='real_dispatch'
     and ((e.scope_kind='action_type' and e.scope_ref=v_resource) or e.scope_kind='tenant')
     and e.revision>coalesce((i.control_snapshot->>'control_revision')::bigint,-1)) then
    raise exception 'switch' using errcode='42501',hint='real_dispatch_closed';end if;
  exception when insufficient_privilege then
   get stacked diagnostics v_code=pg_exception_hint;
   if coalesce(v_code,'')='' then raise;end if;
  end;
  perform set_config('eios.tenant_id',t,true);
  if v_code is not null and v_code<>'' then
   update runtime.nexloop_outreach_sends set state='refused',refusal_code=v_code,refusal_gates=g,finished_at=runtime.nexloop_world_now(t,p_world),updated_at=clock_timestamp()
    where tenant_id=t and world=p_world and intent_id=i.intent_id;
   insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
    values('outreach-refused:'||i.intent_id,'nexloop.outreach.send.refused',t,'service:'||((p_text::jsonb)->>'principal_id'),'outreach',i.intent_id,
     jsonb_build_object('code',v_code,'gates',g),clock_timestamp()) on conflict do nothing;
   return jsonb_build_object('intent_id',i.intent_id,'admitted',false,'state','refused','code',v_code);
  end if;
  update runtime.nexloop_outreach_sends set state='dispatching',fence=fence+1,lease_until=clock_timestamp()+interval '30 seconds',
   admitted_at=runtime.nexloop_world_now(t,p_world),updated_at=clock_timestamp() where tenant_id=t and world=p_world and intent_id=i.intent_id returning * into i;
  -- A marketing message carries the tenant's unsubscribe notice, appended by the server (P8).
  v_text:=i.body||case when i.frequency_class='marketing' then E'\n'||(runtime.nexloop_outreach_settings(t)->'marketing'->>'unsubscribe_notice') else '' end;
  return jsonb_build_object('intent_id',i.intent_id,'admitted',true,'state',i.state,'fence',i.fence,
   'request',jsonb_build_object('intent_id',i.intent_id,'consumer_ref',i.consumer_id,'channel',i.channel,'topic',i.topic,'purpose',i.purpose_id,
    'conversation_id',i.conversation_id,'text',v_text));
 elsif c->>'verb'='observe' then
  if c->>'outcome' not in ('fulfilled','failed','unknown') or jsonb_typeof(c->'fence') is distinct from 'number'
   or (c ? 'provider_reference' and coalesce(c->>'provider_reference','')!~'^[A-Za-z0-9._:-]{1,200}$') then
   raise exception 'outreach command invalid' using errcode='22023';end if;
  if i.state not in ('dispatching','unknown') or i.fence<>(c->>'fence')::bigint then
   if i.state in ('fulfilled','failed') and i.state=c->>'outcome' then return jsonb_build_object('intent_id',i.intent_id,'state',i.state,'replayed',true);end if;
   raise exception 'outreach observation fenced' using errcode='40001';end if;
  v_state:=c->>'outcome';
  update runtime.nexloop_outreach_sends set state=v_state,provider_reference=coalesce(c->>'provider_reference',provider_reference),
   lease_until=null,finished_at=case when v_state in ('fulfilled','failed') then runtime.nexloop_world_now(t,p_world) end,updated_at=clock_timestamp()
   where tenant_id=t and world=p_world and intent_id=i.intent_id;
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('outreach-'||v_state||':'||i.intent_id||':'||i.fence,'nexloop.outreach.send.'||v_state,t,'service:'||((p_text::jsonb)->>'principal_id'),'outreach',
    i.intent_id,jsonb_build_object('fence',i.fence),clock_timestamp()) on conflict do nothing;
  return jsonb_build_object('intent_id',i.intent_id,'state',v_state,'fence',i.fence);
 elsif c->>'verb'='status' then
  return jsonb_build_object('intent_id',i.intent_id,'state',i.state,'refusal_code',i.refusal_code,'fence',i.fence,'purpose',i.purpose_id,
   'provider_reference',i.provider_reference);
 end if;
 raise exception 'outreach command invalid' using errcode='22023';
end $function$;

-- Outreach gates: quiet hours and frequency at p_at (the world time); the model budget is money and its period stays the wall in a simulation world.
CREATE OR REPLACE FUNCTION runtime.nexloop_outreach_gates(p_tenant text, p_world text, p_consumer text, p_purpose text, p_at timestamp with time zone)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
declare d jsonb:=runtime.nexloop_outreach_settings(p_tenant);g jsonb:='[]'::jsonb;v_local time:=(p_at at time zone (d->>'time_zone'))::time;
 v_from time:=split_part(d->>'quiet_hours','-',1)::time;v_to time:=split_part(d->>'quiet_hours','-',2)::time;v_n24 bigint;v_n7 bigint;v_code text;v_layer text;v_purpose jsonb;
 v_marketing boolean;v_m7 bigint;lim control.nexloop_budget_limits;v_used numeric;v_state jsonb;
 v_budget_at timestamptz:=case when runtime.nexloop_world_kind(p_world) in ('simulation','shadow') then clock_timestamp() else p_at end;
begin
 g:=g||jsonb_build_object('gate','contact_restricted','result',case when exists(select 1 from control.nexloop_contact_restrictions r where r.tenant_id=p_tenant
   and r.world=p_world and r.consumer_id=p_consumer and r.active) then 'refuse' else 'pass' end);
 g:=g||jsonb_build_object('gate','control_paused','result',case when exists(select 1 from control.nexloop_control_scopes s where s.tenant_id=p_tenant and s.world=p_world and s.paused
   and ((s.scope_kind='tenant' and s.scope_ref='*') or (s.scope_kind='consumer' and s.scope_ref in (p_consumer,'consumer:'||p_consumer)))) then 'refuse' else 'pass' end);
 -- Outreach slice 3: the purpose must be in the current purpose list and enabled for the tenant. Outreach slice 6: a purpose that
 -- requires approval refuses with approval_required; the dispatch lets it pass only for an intent whose approval was granted.
 v_purpose:=runtime.nexloop_outreach_purpose(p_tenant,p_purpose);
 g:=g||jsonb_build_object('gate','purpose','result',case when not (v_purpose->>'listed')::boolean or not (v_purpose->>'enabled')::boolean then 'refuse'
   when (v_purpose->'definition'->>'requires_approval')::boolean then 'refuse' else 'pass' end,  -- Outreach slice 6: refused until approved (admit)
  'detail',case when not (v_purpose->>'listed')::boolean then 'purpose_not_allowed' when not (v_purpose->>'enabled')::boolean then 'purpose_disabled'
   when (v_purpose->'definition'->>'requires_approval')::boolean then 'approval_required' else 'list v'||(v_purpose->>'list_version') end);
 -- Outreach slice 4, gate 4: marketing needs the tenant's marketing switch; opt-in (tenant or purpose) needs a consent record, and
 -- consent records arrive with slice 7, so it refuses until then. Opt-out refusals are gates 1 (restriction) and 8 (topic).
 v_marketing:=coalesce(v_purpose->'definition'->>'frequency_class','service')='marketing';
 -- Outreach slice 7: opt-out refuses after an unsubscribe; opt-in (tenant or purpose) needs the latest consent event to be granted.
 v_state:=runtime.nexloop_marketing_state(p_tenant,p_world,p_consumer);
 g:=g||jsonb_build_object('gate','marketing','result',case when not v_marketing then 'pass' when not (d->'marketing'->>'enabled')::boolean then 'refuse'
   when d->'marketing'->>'consent_mode'='opt_in' or coalesce((v_purpose->'definition'->>'requires_consent')::boolean,false) then
    case when (v_state->>'consent')::boolean then 'pass' else 'refuse' end
   when (v_state->>'opted_out')::boolean then 'refuse' else 'pass' end,
  'detail',case when not v_marketing then 'not_marketing' when not (d->'marketing'->>'enabled')::boolean then 'marketing_disabled'
   when d->'marketing'->>'consent_mode'='opt_in' or coalesce((v_purpose->'definition'->>'requires_consent')::boolean,false) then
    case when (v_state->>'consent')::boolean then 'consent_granted' else 'marketing_consent_missing' end
   when (v_state->>'opted_out')::boolean then 'marketing_opted_out' else 'opt_out' end);
 g:=g||jsonb_build_object('gate','persona_excluded','result',case when runtime.nexloop_consumer_persona(p_tenant,p_world,p_consumer)<>'real' then 'refuse' else 'pass' end,
   'detail',runtime.nexloop_consumer_persona(p_tenant,p_world,p_consumer));
 g:=g||jsonb_build_object('gate','quiet_hours','result',case when (v_from<=v_to and v_local>=v_from and v_local<v_to) or (v_from>v_to and (v_local>=v_from or v_local<v_to))
   then 'refuse' else 'pass' end,'detail',d->>'quiet_hours');
 v_n24:=runtime.nexloop_outreach_contacts_since(p_tenant,p_world,p_consumer,p_at-interval '24 hours');
 v_n7:=runtime.nexloop_outreach_contacts_since(p_tenant,p_world,p_consumer,p_at-interval '7 days');
 -- Outreach slice 4: the marketing allowance (P8) counts admitted marketing sends.
 select count(*) into v_m7 from runtime.nexloop_outreach_sends s where s.tenant_id=p_tenant and s.world=p_world and s.consumer_id=p_consumer
  and s.frequency_class='marketing' and s.admitted_at>p_at-interval '7 days' and s.state in ('dispatching','unknown','fulfilled');
 g:=g||jsonb_build_object('gate','frequency','result',case when v_n24>=(d->>'per_24h')::integer or v_n7>=(d->>'per_7d')::integer
   or (v_marketing and v_m7>=(d->'marketing'->>'per_7d')::integer) then 'refuse' else 'pass' end,
   'detail',jsonb_build_object('last_24h',v_n24,'last_7d',v_n7,'marketing_7d',v_m7));
 v_code:=runtime.nexloop_contact_preference_refusal(p_tenant,p_world,p_consumer,'native.webchat',coalesce(v_purpose->'definition'->>'topic',p_purpose),gen_random_uuid(),p_at);
 g:=g||jsonb_build_object('gate','consumer_preference','result',case when v_code is null then 'pass' else 'refuse' end,'detail',v_code);
 v_layer:=control.nexloop_real_dispatch_closed_layer(p_tenant,p_world,'eios:action:nexloop.outreach.send:1');
 g:=g||jsonb_build_object('gate','real_dispatch','result',case when v_layer is null then 'pass' else 'refuse' end,'detail',coalesce(v_layer,'open'));
 -- Outreach slice 4, gate 10: the tenant model budget must be configured, in its period and not used up.
 select * into lim from control.nexloop_budget_limits where tenant_id=p_tenant and world=p_world and budget_kind='model';
 if found then
  select coalesce(sum(amount),0) into v_used from control.nexloop_budget_consumption where tenant_id=p_tenant and world=p_world and budget_kind='model'
   and recorded_at>=lim.period_start and recorded_at<lim.period_end;
 end if;
 g:=g||jsonb_build_object('gate','budget','result',case when lim.tenant_id is null or v_budget_at<lim.period_start or v_budget_at>=lim.period_end or v_used>=lim.limit_amount
   then 'refuse' else 'pass' end,'detail',case when lim.tenant_id is null then 'budget_unconfigured'
   when v_budget_at<lim.period_start or v_budget_at>=lim.period_end or v_used>=lim.limit_amount then 'budget_exhausted' else 'model' end);
 return g;
end $function$;

