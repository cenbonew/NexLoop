-- ADR-045 (owner 2026-11-07; docs/design/synthetic-model-fault.md v3, L6 PASS with C1/C2): a stage-only fault switch that makes the
-- first model call of a test Consumer's message Run fail, so NX-038/039 get failed Runs with retrieval misses without changing the
-- failure basis. The Run fails before its model call is registered (failure synthetic_model_fault): no effect, nothing to reconcile.
-- * control.nexloop_synthetic_fault_switches: one live switch per tenant and world at most (remaining 1-50, at most 2 hours, opened by
--   the owner: principal_id); live = not closed, not expired, remaining > 0, computed every time (D1).
-- * control.nexloop_synthetic_fault_members: the switch's Consumers (1-8); a Consumer erasure deletes its rows at once (trigger on
--   control.nexloop_consumer_erasures, the NX-029 propagation's request table).
-- * runtime.nexloop_synthetic_fault_decisions: one row per Run decided while a switch was live (injected or not); no row means not
--   injected, so nothing is written without a live switch. run_id and switch_id only: no customer data, so it is outside the erasure
--   (no consumer or identity column) and not purged; it grows only by the Runs decided during a drill (at most 50 injected per switch).
-- * runtime.nexloop_synthetic_fault_events: append-only audit (opened, closed, injected, exhausted, expired): the switch, the Run, the
--   remaining count and the owner's principal; no Consumer and no customer data.
-- * authz.nexloop_synthetic_fault_decide(digest, world, run, operation): called by the guard (runtime-worker: nexloop_domain_worker; the API
--   backend: nexloop_api) after a start / resume authorization
--   (nexloop_eios.runtime_activation); a start of a message Run of a synthetic / simulated_agent Consumer named by the live switch is
--   injected and takes one; a resume returns the start's decision (D2). The persona is the Consumer's governed one (ADR-034, 0220).
-- * control.nexloop_governed_synthetic_fault_set: the owner's governed handler (capability synthetic_fault.set), open / close.
--   The Action itself is published only from the stage-only manifest deploy/stage/capabilities.stage.v1.json (ADR-045 §2.4): a
--   production database has no Action, so the switch cannot be opened there.
-- * runtime.nexloop_synthetic_fault_snapshot(tenant, world): the metrics snapshot block synthetic_fault {active, remaining, injected_last_hour}.
-- Temporary number; the dispatcher renumbers.
create table control.nexloop_synthetic_fault_switches (
 tenant_id text not null,world text not null check(world='real'),switch_id uuid not null,
 remaining integer not null check(remaining between 0 and 50),
 opened_at timestamptz not null default clock_timestamp(),expires_at timestamptz not null,
 principal_id text not null,reason text not null check(char_length(reason) between 1 and 500),request_intent text not null,
 closed_at timestamptz,close_reason text check(close_reason in ('closed','exhausted','expired')),
 check(expires_at>opened_at and expires_at<=opened_at+interval '2 hours'),check((closed_at is null)=(close_reason is null)),
 primary key(tenant_id,world,switch_id),unique(tenant_id,world,request_intent),
 foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
create unique index nexloop_synthetic_fault_one_open on control.nexloop_synthetic_fault_switches(tenant_id,world) where closed_at is null;
create table control.nexloop_synthetic_fault_members (
 tenant_id text not null,world text not null check(world='real'),switch_id uuid not null,consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),
 primary key(tenant_id,world,switch_id,consumer_id),
 foreign key(tenant_id,world,switch_id) references control.nexloop_synthetic_fault_switches(tenant_id,world,switch_id)
);
create table runtime.nexloop_synthetic_fault_decisions (
 tenant_id text not null,world text not null check(world='real'),run_id uuid not null,switch_id uuid not null,injected boolean not null,
 decided_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,run_id),
 foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
create table runtime.nexloop_synthetic_fault_events (
 event_id bigint generated always as identity primary key,
 tenant_id text not null,world text not null check(world='real'),switch_id uuid not null,
 kind text not null check(kind in ('opened','closed','injected','exhausted','expired')),
 run_id uuid,remaining integer,principal_id text,
 recorded_at timestamptz not null default clock_timestamp(),
 check((kind='injected')=(run_id is not null)),
 foreign key(tenant_id) references control.nexloop_tenants(tenant_id)
);
do $tables$
declare t text;
begin
 foreach t in array array['control.nexloop_synthetic_fault_switches','control.nexloop_synthetic_fault_members','runtime.nexloop_synthetic_fault_decisions','runtime.nexloop_synthetic_fault_events'] loop
  execute 'alter table '||t||' owner to nexloop_owner';execute 'alter table '||t||' enable row level security';
  execute 'alter table '||t||' force row level security';execute 'revoke all on '||t||' from public';
 end loop;
end $tables$;
create policy synthetic_fault_switch_tenant on control.nexloop_synthetic_fault_switches to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy synthetic_fault_member_tenant on control.nexloop_synthetic_fault_members to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy synthetic_fault_decision_tenant on runtime.nexloop_synthetic_fault_decisions to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy synthetic_fault_event_tenant on runtime.nexloop_synthetic_fault_events to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create function runtime.nexloop_synthetic_fault_events_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'synthetic fault events are append-only' using errcode='42501';end $$;
alter function runtime.nexloop_synthetic_fault_events_append_only() owner to nexloop_owner;
revoke all on function runtime.nexloop_synthetic_fault_events_append_only() from public;
create trigger synthetic_fault_events_append_only before update or delete on runtime.nexloop_synthetic_fault_events
 for each row execute function runtime.nexloop_synthetic_fault_events_append_only();
-- NX-029 propagation: an erasure request removes the Consumer from every switch at once (before any later Run is decided).
create function control.nexloop_synthetic_fault_erasure_members() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 delete from control.nexloop_synthetic_fault_members where tenant_id=new.tenant_id and world=new.world and consumer_id=new.consumer_id;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return new;
end $$;
alter function control.nexloop_synthetic_fault_erasure_members() owner to nexloop_owner;
revoke all on function control.nexloop_synthetic_fault_erasure_members() from public;
create trigger synthetic_fault_erasure_members after insert on control.nexloop_consumer_erasures
 for each row execute function control.nexloop_synthetic_fault_erasure_members();

-- The live switch of a tenant: not closed, not expired, remaining > 0 (D1: computed, never the closed_at column alone).
create function runtime.nexloop_synthetic_fault_live(p_tenant text,p_world text) returns control.nexloop_synthetic_fault_switches
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select s from control.nexloop_synthetic_fault_switches s where s.tenant_id=p_tenant and s.world=p_world and s.closed_at is null
  and s.expires_at>clock_timestamp() and s.remaining>0 limit 1 $$;
alter function runtime.nexloop_synthetic_fault_live(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_synthetic_fault_live(text,text) from public;

-- An open switch that is no longer live gets its closing row and event (D1: an expired or exhausted switch never blocks a new one).
create function runtime.nexloop_synthetic_fault_lapse(p_tenant text,p_world text) returns void
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare s control.nexloop_synthetic_fault_switches;v_reason text;
begin
 for s in select * from control.nexloop_synthetic_fault_switches x where x.tenant_id=p_tenant and x.world=p_world and x.closed_at is null
   and (x.expires_at<=clock_timestamp() or x.remaining<=0) for update loop
  v_reason:=case when s.remaining<=0 then 'exhausted' else 'expired' end;
  update control.nexloop_synthetic_fault_switches set closed_at=clock_timestamp(),close_reason=v_reason
   where tenant_id=s.tenant_id and world=s.world and switch_id=s.switch_id;
  insert into runtime.nexloop_synthetic_fault_events(tenant_id,world,switch_id,kind,remaining) values(s.tenant_id,s.world,s.switch_id,v_reason,s.remaining);
 end loop;
end $$;
alter function runtime.nexloop_synthetic_fault_lapse(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_synthetic_fault_lapse(text,text) from public;

-- The guard's decision for one authorized start / resume (nexloop_eios.runtime_activation, after SQL authorized it).
create function authz.nexloop_synthetic_fault_decide(p_digest text,p_world text,p_run uuid,p_operation text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;prior text:=current_setting('eios.tenant_id',true);d runtime.nexloop_synthetic_fault_decisions;
 s control.nexloop_synthetic_fault_switches;v_consumer text;v_injected boolean:=false;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or p_run is null or p_operation not in ('start','resume') then
  raise exception 'synthetic fault decision unavailable' using errcode='42501';end if;
 if p_world is distinct from 'real' then return false;end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant:=ident->'binding'->>'tenant_id';
 if v_tenant is null then raise exception 'synthetic fault decision unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into d from runtime.nexloop_synthetic_fault_decisions where tenant_id=v_tenant and world=p_world and run_id=p_run;
 if found or p_operation='resume' then  -- D2: a resume keeps the start's decision; no row means not injected
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return coalesce(d.injected,false);
 end if;
 select (runtime.nexloop_synthetic_fault_live(v_tenant,p_world)).* into s;
 if s.switch_id is null then  -- no live switch: nothing is written (L6: the table does not grow with every Run)
  perform runtime.nexloop_synthetic_fault_lapse(v_tenant,p_world);
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return false;
 end if;
 select * into s from control.nexloop_synthetic_fault_switches x where x.tenant_id=v_tenant and x.world=p_world and x.switch_id=s.switch_id for update;
 -- A message Run only (never a fallback, role or plan Run): the Consumer of its inbound message, its governed persona.
 select cv.consumer_id into v_consumer from runtime.nexloop_message_routes mr
  join runtime.nexloop_conversation_messages m on m.tenant_id=mr.tenant_id and m.world=mr.world and m.message_id=mr.message_id
  join runtime.nexloop_conversations cv on cv.tenant_id=m.tenant_id and cv.world=m.world and cv.conversation_id=m.conversation_id
  where mr.tenant_id=v_tenant and mr.world=p_world and mr.run_id=p_run limit 1;
 v_injected:=v_consumer is not null and exists(select 1 from control.nexloop_synthetic_fault_members fm where fm.tenant_id=v_tenant
   and fm.world=p_world and fm.switch_id=s.switch_id and fm.consumer_id=v_consumer) and s.remaining>0 and s.expires_at>clock_timestamp()
  and runtime.nexloop_consumer_persona(v_tenant,p_world,v_consumer) in ('synthetic','simulated_agent')
  and not runtime.nexloop_consumer_erasing(v_tenant,p_world,v_consumer);
 insert into runtime.nexloop_synthetic_fault_decisions(tenant_id,world,run_id,switch_id,injected) values(v_tenant,p_world,p_run,s.switch_id,v_injected)
  on conflict do nothing;
 if not found then  -- a concurrent start of the same Run decided first
  select * into d from runtime.nexloop_synthetic_fault_decisions where tenant_id=v_tenant and world=p_world and run_id=p_run;
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return d.injected;
 end if;
 if v_injected then
  update control.nexloop_synthetic_fault_switches set remaining=remaining-1 where tenant_id=v_tenant and world=p_world and switch_id=s.switch_id;
  insert into runtime.nexloop_synthetic_fault_events(tenant_id,world,switch_id,kind,run_id,remaining)
   values(v_tenant,p_world,s.switch_id,'injected',p_run,s.remaining-1);
  perform runtime.nexloop_synthetic_fault_lapse(v_tenant,p_world);  -- the last one closes the switch (exhausted)
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v_injected;
end $$;
alter function authz.nexloop_synthetic_fault_decide(text,text,uuid,text) owner to nexloop_owner;
revoke all on function authz.nexloop_synthetic_fault_decide(text,text,uuid,text) from public;
grant execute on function authz.nexloop_synthetic_fault_decide(text,text,uuid,text) to nexloop_api,nexloop_domain_worker;  -- the guard runs in the runtime-worker (nexloop_domain_worker)

-- The owner's governed handler (capability synthetic_fault.set, operation set_synthetic_fault): state open {consumer_ids, remaining,
-- duration_seconds, reason} | closed (consumer_ids [], remaining 0, duration_seconds 0, reason). Every key is always present (one body shape).
create function control.nexloop_governed_synthetic_fault_set(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare op text:=case body->>'state' when 'open' then 'open_synthetic_fault' when 'closed' then 'close_synthetic_fault' end;v_ids text[];s control.nexloop_synthetic_fault_switches;v_id uuid;v_seconds integer;v_remaining integer;
begin
 if (select array_agg(k order by k) from jsonb_object_keys(body) k) is distinct from array['consumer_ids','duration_seconds','operation','reason','remaining','request_id','state']
  or body->>'operation' is distinct from 'set_synthetic_fault' then raise exception 'synthetic fault: invalid' using errcode='22023';end if;
 if op='open_synthetic_fault' then
  if jsonb_typeof(body->'consumer_ids') is distinct from 'array' or jsonb_typeof(body->'remaining') is distinct from 'number'
   or jsonb_typeof(body->'duration_seconds') is distinct from 'number' or char_length(coalesce(body->>'reason','')) not between 1 and 500 then
   raise exception 'synthetic fault: invalid' using errcode='22023';end if;
  v_ids:=array(select distinct e#>>'{}' from jsonb_array_elements(body->'consumer_ids') e order by 1);
  v_remaining:=(body->>'remaining')::numeric;v_seconds:=(body->>'duration_seconds')::numeric;
  if cardinality(v_ids) not between 1 and 8 or exists(select 1 from unnest(v_ids) c where c!~'^[a-f0-9]{64}$')
   or v_remaining not between 1 and 50 or v_seconds not between 60 and 7200 or (body->>'remaining')::numeric<>v_remaining
   or (body->>'duration_seconds')::numeric<>v_seconds then
   raise exception 'synthetic fault: invalid' using errcode='22023';end if;
 elsif op='close_synthetic_fault' then
  if body->'consumer_ids' is distinct from '[]'::jsonb or body->'remaining' is distinct from '0'::jsonb or body->'duration_seconds' is distinct from '0'::jsonb
   or char_length(coalesce(body->>'reason','')) not between 1 and 500 then raise exception 'synthetic fault: invalid' using errcode='22023';end if;
 else raise exception 'synthetic fault: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'synthetic fault: real_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform pg_advisory_xact_lock(hashtextextended('nexloop.synthetic_fault:'||p_tenant,0));
 select * into s from control.nexloop_synthetic_fault_switches where tenant_id=p_tenant and world=p_world and request_intent=p_intent;
 if found and op='open_synthetic_fault' then
  return jsonb_build_object('switch_id',s.switch_id,'opened',true,'replayed',true,'remaining',s.remaining,'expires_at',s.expires_at);
 end if;
 perform runtime.nexloop_synthetic_fault_lapse(p_tenant,p_world);  -- D1: a lapsed switch is closed first
 if op='close_synthetic_fault' then
  select * into s from control.nexloop_synthetic_fault_switches where tenant_id=p_tenant and world=p_world and closed_at is null for update;
  if not found then return jsonb_build_object('closed',false,'reason','no_live_switch');end if;
  update control.nexloop_synthetic_fault_switches set closed_at=clock_timestamp(),close_reason='closed'
   where tenant_id=p_tenant and world=p_world and switch_id=s.switch_id;
  insert into runtime.nexloop_synthetic_fault_events(tenant_id,world,switch_id,kind,remaining,principal_id) values(p_tenant,p_world,s.switch_id,'closed',s.remaining,p_principal);
  return jsonb_build_object('switch_id',s.switch_id,'closed',true,'remaining',s.remaining);
 end if;
 if exists(select 1 from control.nexloop_synthetic_fault_switches where tenant_id=p_tenant and world=p_world and closed_at is null) then
  raise exception 'synthetic fault: switch_open' using errcode='22023';end if;
 if exists(select 1 from unnest(v_ids) c where not exists(select 1 from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=c)) then
  raise exception 'synthetic fault: consumer_unavailable' using errcode='22023';end if;
 if exists(select 1 from unnest(v_ids) c where runtime.nexloop_consumer_persona(p_tenant,p_world,c) not in ('synthetic','simulated_agent')) then
  raise exception 'synthetic fault: consumer_not_synthetic' using errcode='22023';end if;  -- one real Consumer refuses the whole switch
 v_id:=gen_random_uuid();
 insert into control.nexloop_synthetic_fault_switches(tenant_id,world,switch_id,remaining,expires_at,principal_id,reason,request_intent)
  values(p_tenant,p_world,v_id,v_remaining,clock_timestamp()+make_interval(secs=>v_seconds),p_principal,body->>'reason',p_intent) returning * into s;
 insert into control.nexloop_synthetic_fault_members(tenant_id,world,switch_id,consumer_id) select p_tenant,p_world,v_id,c from unnest(v_ids) c;
 insert into runtime.nexloop_synthetic_fault_events(tenant_id,world,switch_id,kind,remaining,principal_id) values(p_tenant,p_world,v_id,'opened',v_remaining,p_principal);
 return jsonb_build_object('switch_id',v_id,'opened',true,'replayed',false,'remaining',s.remaining,'expires_at',s.expires_at,'consumers',cardinality(v_ids));
end $$;
alter function control.nexloop_governed_synthetic_fault_set(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_synthetic_fault_set(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('synthetic_fault.set','set_synthetic_fault','human','control.nexloop_governed_synthetic_fault_set(text,text,text,text,text,jsonb)','NX-038');

-- The metrics snapshot block (counts only): whether a switch is live now, its remaining count and the injections of the hour.
create function runtime.nexloop_synthetic_fault_snapshot(p_tenant text,p_world text) returns jsonb
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('active',case when (runtime.nexloop_synthetic_fault_live(p_tenant,p_world)).switch_id is null then 0 else 1 end,
  'remaining',coalesce((runtime.nexloop_synthetic_fault_live(p_tenant,p_world)).remaining,0),
  'injected_last_hour',(select count(*) from runtime.nexloop_synthetic_fault_events e where e.tenant_id=p_tenant and e.world=p_world
    and e.kind='injected' and e.recorded_at>clock_timestamp()-interval '1 hour')) $$;
alter function runtime.nexloop_synthetic_fault_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_synthetic_fault_snapshot(text,text) from public;
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_synthetic_fault;
revoke all on function runtime.nexloop_metrics_snapshot_before_synthetic_fault(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_synthetic_fault(p_tenant,p_world);prior text:=current_setting('eios.tenant_id',true);b jsonb;
begin
 if p_world is distinct from 'real' then return v;end if;
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 b:=runtime.nexloop_synthetic_fault_snapshot(p_tenant,p_world);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v||jsonb_build_object('synthetic_fault',b);
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
