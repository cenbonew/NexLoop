-- NX-041 S7 (design §5, §8, §11 S7): the consumer port, the one way the twin (NX-042) produces inbound events in a simulation world.
-- The migration number lives only in the file name and the catalog; nothing below repeats it.
-- 1. runtime.nexloop_sim_consumer_events: what a twin's consumer did (a message, a contact refusal, a payment intent, a visit), in a
--    registered active sim:/shadow: world only, for a synthetic Consumer generated there (S3). Every row is data_mode simulation,
--    output_kind simulated_input, origin twin, with the twin version, seed and step it came from, and the world time (S5) the server
--    read when it arrived; the client never gives a time. Append-only; it leaves with the world (S2).
-- 2. runtime.nexloop_sim_consumer_observations: what the twin's consumer is shown (an Agent reply, a stand-in receipt, an outreach),
--    written by the simulation driver (owner-only function) and read by the twin through the port.
-- 3. Entries for the port role nexloop_twin_port only, authenticated by the simulation world's one credential (S2, D10):
--    authz.nexloop_sim_consumer_act (contract nexloop-consumer-port-event/1) and authz.nexloop_sim_consumer_observe. The port holds no
--    real credential and calls no Action: real and test are refused before any lookup (S4 authz.nexloop_sim_effect_tenant).

-- 1. Events -----------------------------------------------------------------------------------------------------------------------
create table runtime.nexloop_sim_consumer_events (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 world text not null check(coalesce(runtime.nexloop_world_kind(world),'') in ('simulation','shadow')),
 event_id text not null check(event_id~'^[0-9a-f]{64}$'),
 consumer_id text not null check(consumer_id~'^[0-9a-f]{64}$'),
 sequence integer not null check(sequence>0),
 event_kind text not null check(event_kind in ('message','refuse_contact','payment_intent','visit')),
 content jsonb not null check(jsonb_typeof(content)='object'),content_digest text not null check(content_digest~'^[0-9a-f]{64}$'),
 twin_version text not null check(twin_version~'^[a-z][a-z0-9_.-]{0,39}/[0-9]+$'),twin_seed text not null check(twin_seed~'^[A-Za-z0-9_.-]{1,64}$'),
 twin_step integer not null check(twin_step between 1 and 1000000),
 sim_time timestamptz not null,
 data_mode text not null default 'simulation' check(data_mode='simulation'),
 output_kind text not null default 'simulated_input' check(output_kind='simulated_input'),
 origin text not null default 'twin' check(origin='twin'),
 recorded_by text not null check(char_length(recorded_by) between 1 and 200),
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,event_id),unique(tenant_id,world,consumer_id,sequence));
create table runtime.nexloop_sim_consumer_observations (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 world text not null check(coalesce(runtime.nexloop_world_kind(world),'') in ('simulation','shadow')),
 consumer_id text not null check(consumer_id~'^[0-9a-f]{64}$'),
 sequence integer not null check(sequence>0),
 observation_kind text not null check(observation_kind in ('agent_reply','stand_in_receipt','outreach')),
 payload jsonb not null check(jsonb_typeof(payload)='object'),
 sim_time timestamptz not null,
 data_mode text not null default 'simulation' check(data_mode='simulation'),
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,consumer_id,sequence));
do $$declare t text;begin
 foreach t in array array['runtime.nexloop_sim_consumer_events','runtime.nexloop_sim_consumer_observations'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker',t);
  -- Append-only; only the S2 whole deletion (its erasure scope) removes rows (S4 runtime.nexloop_sim_ledger_guard).
  execute format('create trigger nx041_sim_port_guard before update or delete on %s for each row execute function runtime.nexloop_sim_ledger_guard()',t);
  execute format('create trigger nx041_sim_port_no_truncate before truncate on %s for each statement execute function runtime.nexloop_sim_ledger_guard()',t);
 end loop;
end $$;

-- The port role: executes the two port entries and nothing else; its connection is deployment configuration (no credential here).
do $$begin
 if not exists(select 1 from pg_roles where rolname='nexloop_twin_port') then
  create role nexloop_twin_port login nosuperuser nocreatedb nocreaterole noinherit noreplication nobypassrls;
 end if;
end $$;
grant usage on schema authz to nexloop_twin_port;

-- A synthetic Consumer of this world (S3: generated object and persona synthetic). The caller has set the tenant.
create function runtime.nexloop_sim_consumer_known(p_tenant text,p_world text,p_consumer text) returns boolean
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select exists(select 1 from runtime.nexloop_sim_objects o where o.tenant_id=p_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=p_consumer)
  and runtime.nexloop_consumer_persona(p_tenant,p_world,p_consumer)='synthetic'
$$;
alter function runtime.nexloop_sim_consumer_known(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_sim_consumer_known(text,text,text) from public;

-- 2. act ----------------------------------------------------------------------------------------------------------------------------
-- p_event is the request part of nexloop-consumer-port-event/1: consumer_id, event_kind, content {text?, attributes?}, twin {version,
-- seed, step}. The event id is derived (world, consumer, twin version, seed, step): a retried step replays, the same step with other
-- content is refused. The world time is the server's.
create function authz.nexloop_sim_consumer_act(p_digest text,p_world text,p_event jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;v_principal text;v_consumer text:=p_event->>'consumer_id';v_content jsonb:=p_event->'content';v_twin jsonb:=p_event->'twin';
 v_id text;v_digest text;x runtime.nexloop_sim_consumer_events;
begin
 if session_user is distinct from 'nexloop_twin_port' then raise exception 'consumer port: unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 if jsonb_typeof(p_event) is distinct from 'object' or exists(select 1 from jsonb_object_keys(p_event) k where k not in ('consumer_id','event_kind','content','twin'))
  or coalesce(v_consumer,'')!~'^[0-9a-f]{64}$' or coalesce(p_event->>'event_kind','') not in ('message','refuse_contact','payment_intent','visit')
  or jsonb_typeof(v_content) is distinct from 'object' or exists(select 1 from jsonb_object_keys(v_content) k where k not in ('text','attributes'))
  or (v_content ? 'text' and (jsonb_typeof(v_content->'text')<>'string' or char_length(v_content->>'text') not between 1 and 2000))
  or (v_content ? 'attributes' and (jsonb_typeof(v_content->'attributes')<>'object' or (select count(*) from jsonb_object_keys(v_content->'attributes'))>16
     or exists(select 1 from jsonb_each(v_content->'attributes') a where a.key!~'^[a-z][a-z0-9_]{0,31}$' or jsonb_typeof(a.value) not in ('string','number','boolean')
       or (jsonb_typeof(a.value)='string' and char_length(a.value#>>'{}')>200))))
  or ((p_event->>'event_kind')='message' and not v_content ? 'text')
  or jsonb_typeof(v_twin) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(v_twin) k) is distinct from array['seed','step','version']
  or coalesce(v_twin->>'version','')!~'^[a-z][a-z0-9_.-]{0,39}/[0-9]+$' or coalesce(v_twin->>'seed','')!~'^[A-Za-z0-9_.-]{1,64}$'
  or jsonb_typeof(v_twin->'step') is distinct from 'number' or (v_twin->>'step')!~'^[0-9]{1,7}$' or (v_twin->>'step')::integer not between 1 and 1000000 then
  raise exception 'consumer port: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 if not runtime.nexloop_sim_consumer_known(v_tenant,p_world,v_consumer) then raise exception 'consumer port: consumer_unknown' using errcode='22023';end if;
 v_id:=encode(sha256(convert_to(p_world||'|'||v_consumer||'|'||(v_twin->>'version')||'|'||(v_twin->>'seed')||'|'||(v_twin->>'step'),'UTF8')),'hex');
 v_digest:=encode(sha256(convert_to((p_event->>'event_kind')||'|'||v_content::text,'UTF8')),'hex');
 v_principal:=authz.nexloop_service_identity(p_digest,p_world)->'binding'->>'principal_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 -- One event at a time per consumer: the sequence is the next one.
 perform pg_advisory_xact_lock(hashtextextended('nexloop:sim-consumer:'||p_world||':'||v_consumer,0));
 select * into x from runtime.nexloop_sim_consumer_events where tenant_id=v_tenant and world=p_world and event_id=v_id;
 if found then
  if x.content_digest<>v_digest then raise exception 'consumer port: step_reused' using errcode='22023';end if;
 else
  insert into runtime.nexloop_sim_consumer_events(tenant_id,world,event_id,consumer_id,sequence,event_kind,content,content_digest,twin_version,twin_seed,twin_step,
    sim_time,recorded_by)
   values(v_tenant,p_world,v_id,v_consumer,coalesce((select max(sequence) from runtime.nexloop_sim_consumer_events where tenant_id=v_tenant and world=p_world
     and consumer_id=v_consumer),0)+1,p_event->>'event_kind',v_content,v_digest,v_twin->>'version',v_twin->>'seed',(v_twin->>'step')::integer,
    runtime.nexloop_world_now(v_tenant,p_world),coalesce(v_principal,'service'))
   returning * into x;
 end if;
 return jsonb_build_object('schema','nexloop-consumer-port-event/1',
  'request',jsonb_build_object('consumer_id',x.consumer_id,'event_kind',x.event_kind,'content',x.content,
    'twin',jsonb_build_object('version',x.twin_version,'seed',x.twin_seed,'step',x.twin_step)),
  'frozen',jsonb_build_object('world',x.world,'event_id',x.event_id,'sequence',x.sequence,'content_digest',x.content_digest,
    'sim_time',runtime.nexloop_commercial_ts(x.sim_time),'data_mode',x.data_mode,'output_kind',x.output_kind,'origin',x.origin),
  'replayed',x.recorded_at<statement_timestamp());
end $$;

-- 3. observe ------------------------------------------------------------------------------------------------------------------------
create function authz.nexloop_sim_consumer_observe(p_digest text,p_world text,p_consumer text,p_after integer) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;
begin
 if session_user is distinct from 'nexloop_twin_port' then raise exception 'consumer port: unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 if coalesce(p_consumer,'')!~'^[0-9a-f]{64}$' or p_after is null or p_after<0 then raise exception 'consumer port: invalid' using errcode='22023';end if;
 if not runtime.nexloop_sim_consumer_known(v_tenant,p_world,p_consumer) then raise exception 'consumer port: consumer_unknown' using errcode='22023';end if;
 return coalesce((select jsonb_agg(jsonb_build_object('sequence',o.sequence,'observation_kind',o.observation_kind,'payload',o.payload,
    'sim_time',runtime.nexloop_commercial_ts(o.sim_time),'data_mode',o.data_mode) order by o.sequence)
   from (select * from runtime.nexloop_sim_consumer_observations where tenant_id=v_tenant and world=p_world and consumer_id=p_consumer and sequence>p_after
    order by sequence limit 200) o),'[]'::jsonb);
end $$;

-- The simulation driver offers an observation to a synthetic Consumer (owner-only: no application role holds it).
create function runtime.nexloop_sim_consumer_offer(p_tenant text,p_world text,p_consumer text,p_kind text,p_payload jsonb) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_seq integer;
begin
 if coalesce(runtime.nexloop_world_kind(p_world),'') not in ('simulation','shadow') then raise exception 'consumer port: real_world_refused' using errcode='42501';end if;
 if control.nexloop_sim_world_status(p_tenant,p_world) is distinct from 'active' then raise exception 'consumer port: world_unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if not runtime.nexloop_sim_consumer_known(p_tenant,p_world,p_consumer) then raise exception 'consumer port: consumer_unknown' using errcode='22023';end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop:sim-consumer-observe:'||p_world||':'||p_consumer,0));
 select coalesce(max(sequence),0)+1 into v_seq from runtime.nexloop_sim_consumer_observations where tenant_id=p_tenant and world=p_world and consumer_id=p_consumer;
 insert into runtime.nexloop_sim_consumer_observations(tenant_id,world,consumer_id,sequence,observation_kind,payload,sim_time)
  values(p_tenant,p_world,p_consumer,v_seq,p_kind,p_payload,runtime.nexloop_world_now(p_tenant,p_world));
 return v_seq;
end $$;

do $$declare f text;begin
 foreach f in array array['authz.nexloop_sim_consumer_act(text,text,jsonb)','authz.nexloop_sim_consumer_observe(text,text,text,integer)',
   'runtime.nexloop_sim_consumer_offer(text,text,text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public';
 end loop;
end $$;
grant execute on function authz.nexloop_sim_consumer_act(text,text,jsonb) to nexloop_twin_port;
grant execute on function authz.nexloop_sim_consumer_observe(text,text,text,integer) to nexloop_twin_port;
