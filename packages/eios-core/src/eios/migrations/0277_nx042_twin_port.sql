-- NX-042 T3 (design §5, D1, D4, D6, D7; NX-041 S7, S7b): the consumer twin on the consumer port. The migration number lives only in the
-- file name and the catalog; nothing below repeats it.
-- The twin process connects as nexloop_twin_port with its simulation world's one credential (the port's, NX-041 S7); it holds no other
-- credential and calls no Action. Real and test are refused before any lookup (authz.nexloop_sim_effect_tenant).
-- 1. authz.nexloop_twin_bind(digest, world, twin, version, seed): binds a published, not retired twin version with the template
--    generator (D6: a model generator is refused, model_budget_required) and a seed to the world; binding_id = sha256(world|twin|
--    version|seed). A repeat returns the binding.
-- 2. authz.nexloop_twin_observe(digest, world, binding, consumer, step, limit): what the twin's Consumer sees before its step - the
--    world's simulation context pack assembled by the one assembler (S7b authz.nexloop_sim_context_command) and the observations
--    offered to the Consumer (S7) - fixed by reference with its sha256 to (binding, consumer, step), as S7b fixes a Run's pack; a repeat
--    returns the fixed observation.
-- 3. authz.nexloop_twin_act(digest, world, binding, consumer, step): the step's event, decided by the server from the twin version's
--    decision layer (T2 runtime.nexloop_twin_decide over seed | definition digest | consumer, instance = step): the intervals and one
--    simulated outcome per behaviour; the first true behaviour by kind (refuse_contact/unsubscribe > payment > reply/accept/message >
--    any other = visit) becomes one event written through the S7 port entry (authz.nexloop_sim_consumer_act: the world's 100 000 event
--    ceiling, replay and step_reused stay its own); no true behaviour is a silent step (no event). The step must have been observed.
--    The event's port version is twin.<definition digest prefix>/<version>; its attributes and runtime.nexloop_twin_event_provenance
--    carry the twin id, version, digest, seed, step, the calibration status at that step (uncalibrated until T5) and the observation
--    digest. The decision never reads the observation, so the same seed and twin version reproduce byte for byte.
-- 4. The S7b assembler admits the port role (a session of nexloop_twin_port; only the owner-run observe entry reaches it - the port role
--    holds no execute on it). Everything else of its body is 0269's.

create or replace function authz.nexloop_sim_context_command(p_digest text,p_world text,p_consumer text,p_limit integer) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;v_limit integer;v_now text;v_total integer;v_profile jsonb;v_persona record;v_state jsonb:='[]';v_evidence jsonb;
 v_latest runtime.nexloop_sim_consumer_events;v_insufficient jsonb:='[]';
begin
 if authz.nexloop_app_role(p_world) not in ('nexloop_api','nexloop_domain_worker') and session_user is distinct from 'nexloop_twin_port' then raise exception 'sim context: unavailable' using errcode='42501';end if;
 -- Parameters first (review 2.2): a malformed request gets the same answer whatever the credential.
 if coalesce(p_consumer,'')!~'^[0-9a-f]{64}$' or p_limit is null or p_limit not between 1 and 256 then
  raise exception 'sim context: invalid' using errcode='22023';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 perform set_config('eios.tenant_id',v_tenant,true);
 if not runtime.nexloop_sim_consumer_known(v_tenant,p_world,p_consumer) then raise exception 'sim context: consumer_unknown' using errcode='22023';end if;
 v_limit:=p_limit;
 v_now:=runtime.nexloop_commercial_ts(runtime.nexloop_world_now(v_tenant,p_world));
 -- Consumer state: the generated profile and the persona marker, both simulated.
 select o.properties into v_profile from runtime.nexloop_sim_objects o
  where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=p_consumer;
 v_state:=v_state||jsonb_build_array(jsonb_build_object('subsection','profile','ref','sim:object:'||p_consumer,
  'revision',substr(encode(sha256(convert_to(v_profile::text,'UTF8')),'hex'),1,16),'content',v_profile,
  'content_hash',encode(sha256(convert_to(v_profile::text,'UTF8')),'hex'),'evidence_kind','synthetic_profile','source','simulated','at',v_now,'tags','[]'::jsonb));
 select p.persona,p.revision into v_persona from control.nexloop_consumer_personas p where p.tenant_id=v_tenant and p.world=p_world and p.consumer_id=p_consumer;
 v_state:=v_state||jsonb_build_array(jsonb_build_object('subsection','persona','ref','sim:persona:'||p_consumer,'revision',v_persona.revision::text,
  'content',jsonb_build_object('persona',v_persona.persona),'content_hash',encode(sha256(convert_to(jsonb_build_object('persona',v_persona.persona)::text,'UTF8')),'hex'),
  'evidence_kind','synthetic_persona','source','simulated','at',v_now,'tags','[]'::jsonb));
 -- Simulated conversation: the newest events up to the limit, oldest first.
 select count(*) into v_total from runtime.nexloop_sim_consumer_events e where e.tenant_id=v_tenant and e.world=p_world and e.consumer_id=p_consumer;
 select * into v_latest from runtime.nexloop_sim_consumer_events e where e.tenant_id=v_tenant and e.world=p_world and e.consumer_id=p_consumer
  order by e.sequence desc limit 1;
 select coalesce(jsonb_agg(jsonb_build_object('subsection','simulated_conversation','ref','sim:event:'||x.event_id,'revision',x.sequence::text,
   -- content_hash is the sha256 of the content given here (its jsonb text), as for consumer_state (review 3.3).
   'content',jsonb_build_object('event_kind',x.event_kind)||x.content,
   'content_hash',encode(sha256(convert_to((jsonb_build_object('event_kind',x.event_kind)||x.content)::text,'UTF8')),'hex'),'evidence_kind','simulated_input',
   'source','simulated','at',runtime.nexloop_commercial_ts(x.sim_time),
   'tags',(case when x.sequence=v_latest.sequence then '["latest_consumer_turn"]'::jsonb else '[]'::jsonb end)
     ||(case when x.event_kind='refuse_contact' then '["contact_limit"]'::jsonb else '[]'::jsonb end)) order by x.sequence),'[]'::jsonb)
  into v_evidence from (select * from runtime.nexloop_sim_consumer_events e where e.tenant_id=v_tenant and e.world=p_world and e.consumer_id=p_consumer
   order by e.sequence desc limit v_limit) x;
 if v_total=0 then v_insufficient:=v_insufficient||jsonb_build_array(jsonb_build_object('code','no_simulated_events','section','current_event'));end if;
 if v_total>v_limit then v_insufficient:=v_insufficient||jsonb_build_array(jsonb_build_object('code','older_events_omitted','section','evidence'));end if;
 return jsonb_build_object('schema_version','nexloop.context-pack.sim.v1','world',p_world,'tenant_id',v_tenant,'consumer_id',p_consumer,
  'data_mode','simulation','world_time',v_now,
  'current_event',case when v_latest.event_id is null then null else jsonb_build_object('kind','simulated_consumer_event','event_id',v_latest.event_id,
    'sequence',v_latest.sequence,'event_kind',v_latest.event_kind,'provenance','sim:event:'||v_latest.event_id,'source','simulated') end,
  'consumer_state',v_state,'evidence',v_evidence,
  'budget_report',jsonb_build_object('event_limit',v_limit,'events_total',v_total,'events_included',jsonb_array_length(v_evidence),
    'omitted',greatest(v_total-jsonb_array_length(v_evidence),0)),
  'insufficient',v_insufficient);
end $$;

create table runtime.nexloop_twin_bindings (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 world text not null check(coalesce(runtime.nexloop_world_kind(world),'') in ('simulation','shadow')),
 binding_id text not null check(binding_id~'^[0-9a-f]{64}$'),
 twin_id text not null,twin_version integer not null,twin_digest text not null check(twin_digest~'^[0-9a-f]{64}$'),
 seed text not null check(seed~'^[A-Za-z0-9_.-]{1,64}$'),
 port_version text not null check(port_version~'^[a-z][a-z0-9_.-]{0,39}/[0-9]+$'),
 bound_by text not null check(char_length(bound_by) between 1 and 200),
 bound_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,binding_id));
create table runtime.nexloop_twin_observations (
 tenant_id text not null,world text not null check(coalesce(runtime.nexloop_world_kind(world),'') in ('simulation','shadow')),
 binding_id text not null,consumer_id text not null check(consumer_id~'^[0-9a-f]{64}$'),step integer not null check(step between 1 and 1000000),
 event_limit integer not null check(event_limit between 1 and 256),
 pack_text text not null,pack_digest text not null check(pack_digest~'^[0-9a-f]{64}$'),
 observations jsonb not null check(jsonb_typeof(observations)='array'),
 observation_digest text not null check(observation_digest~'^[0-9a-f]{64}$'),
 data_mode text not null default 'simulation' check(data_mode='simulation'),
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,binding_id,consumer_id,step),
 foreign key(tenant_id,world,binding_id) references runtime.nexloop_twin_bindings(tenant_id,world,binding_id));
create table runtime.nexloop_twin_event_provenance (
 tenant_id text not null,world text not null check(coalesce(runtime.nexloop_world_kind(world),'') in ('simulation','shadow')),
 binding_id text not null,consumer_id text not null,step integer not null,
 event_id text check(event_id~'^[0-9a-f]{64}$'),
 behaviors jsonb not null check(jsonb_typeof(behaviors)='object'),
 behavior text,event_kind text check(event_kind in ('message','refuse_contact','payment_intent','visit')),
 twin_id text not null,twin_version integer not null,twin_digest text not null,seed text not null,
 calibration_status text not null check(calibration_status in ('uncalibrated','calibrated')),
 observation_digest text not null,
 data_mode text not null default 'simulation' check(data_mode='simulation'),
 output_kind text not null default 'simulated_input' check(output_kind='simulated_input'),
 origin text not null default 'twin' check(origin='twin'),
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,binding_id,consumer_id,step),
 check((event_id is null)=(event_kind is null) and (event_id is null)=(behavior is null)),
 foreign key(tenant_id,world,binding_id,consumer_id,step) references runtime.nexloop_twin_observations(tenant_id,world,binding_id,consumer_id,step),
 foreign key(tenant_id,world,event_id) references runtime.nexloop_sim_consumer_events(tenant_id,world,event_id));
do $$declare t text;begin
 foreach t in array array['runtime.nexloop_twin_bindings','runtime.nexloop_twin_observations','runtime.nexloop_twin_event_provenance'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker,nexloop_twin_port,nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api',t);
  -- Append-only; only the S2 whole deletion (its erasure scope) removes rows (S4 runtime.nexloop_sim_ledger_guard).
  execute format('create trigger nx042_twin_port_guard before update or delete on %s for each row execute function runtime.nexloop_sim_ledger_guard()',t);
  execute format('create trigger nx042_twin_port_no_truncate before truncate on %s for each statement execute function runtime.nexloop_sim_ledger_guard()',t);
 end loop;
end $$;

-- The port event kind a behaviour stands for (§3: reply, unsubscribe, accept follow-up, payment, visit) and its priority (lower first).
create function runtime.nexloop_twin_behavior_kind(p_behavior text) returns text language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when p_behavior~'(^refuse|unsubscribe|opt_out)' then 'refuse_contact' when p_behavior~'pay' then 'payment_intent'
  when p_behavior~'(^reply|^accept|message)' then 'message' else 'visit' end
$$;
alter function runtime.nexloop_twin_behavior_kind(text) owner to nexloop_owner;

-- The binding of the caller's world, or refused (the caller has authenticated and set the tenant).
create function runtime.nexloop_twin_binding(p_tenant text,p_world text,p_binding text) returns runtime.nexloop_twin_bindings
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare b runtime.nexloop_twin_bindings;
begin
 select * into b from runtime.nexloop_twin_bindings where tenant_id=p_tenant and world=p_world and binding_id=p_binding;
 if not found then raise exception 'twin port: binding_unknown' using errcode='22023';end if;
 return b;
end $$;
alter function runtime.nexloop_twin_binding(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_twin_binding(text,text,text) from public;

-- 1. bind ---------------------------------------------------------------------------------------------------------------------------
create function authz.nexloop_twin_bind(p_digest text,p_world text,p_twin text,p_version integer,p_seed text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;t control.nexloop_consumer_twins;b runtime.nexloop_twin_bindings;v_id text;
begin
 if session_user is distinct from 'nexloop_twin_port' then raise exception 'twin port: unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 if coalesce(p_twin,'')!~'^[a-z][a-z0-9_.-]{0,63}$' or p_version is null or p_version<1 or coalesce(p_seed,'')!~'^[A-Za-z0-9_.-]{1,64}$' then
  raise exception 'twin port: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into t from control.nexloop_consumer_twins where tenant_id=v_tenant and twin_id=p_twin and version=p_version;
 if not found then raise exception 'twin port: twin_unknown' using errcode='22023';end if;
 if runtime.nexloop_consumer_twin_status(v_tenant,p_twin,p_version) is distinct from 'published' then raise exception 'twin port: twin_retired' using errcode='22023';end if;
 if t.generator_version!~'^twin-template/[0-9]+$' then raise exception 'twin port: model_budget_required' using errcode='22023';end if;
 v_id:=encode(sha256(convert_to(p_world||'|'||p_twin||'|'||p_version||'|'||p_seed,'UTF8')),'hex');
 perform pg_advisory_xact_lock(hashtextextended('nexloop:twin-bind:'||v_id,0));
 select * into b from runtime.nexloop_twin_bindings where tenant_id=v_tenant and world=p_world and binding_id=v_id;
 if not found then
  insert into runtime.nexloop_twin_bindings(tenant_id,world,binding_id,twin_id,twin_version,twin_digest,seed,port_version,bound_by)
   values(v_tenant,p_world,v_id,p_twin,p_version,t.definition_digest,p_seed,'twin.'||substr(t.definition_digest,1,16)||'/'||p_version,
    coalesce(authz.nexloop_service_identity(p_digest,p_world)->'binding'->>'principal_id','service'))
   returning * into b;
 end if;
 return jsonb_build_object('binding_id',b.binding_id,'world',b.world,'twin_id',b.twin_id,'twin_version',b.twin_version,'twin_digest',b.twin_digest,
  'seed',b.seed,'port_version',b.port_version,'calibration_status',runtime.nexloop_consumer_twin_calibration(v_tenant,b.twin_id,b.twin_version));
end $$;

-- 2. observe ------------------------------------------------------------------------------------------------------------------------
create function authz.nexloop_twin_observe(p_digest text,p_world text,p_binding text,p_consumer text,p_step integer,p_limit integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;b runtime.nexloop_twin_bindings;o runtime.nexloop_twin_observations;v_pack jsonb;v_text text;v_seen jsonb;
begin
 if session_user is distinct from 'nexloop_twin_port' then raise exception 'twin port: unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 if coalesce(p_binding,'')!~'^[0-9a-f]{64}$' or coalesce(p_consumer,'')!~'^[0-9a-f]{64}$' or p_step is null or p_step not between 1 and 1000000
  or p_limit is null or p_limit not between 1 and 256 then raise exception 'twin port: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 b:=runtime.nexloop_twin_binding(v_tenant,p_world,p_binding);
 if not runtime.nexloop_sim_consumer_known(v_tenant,p_world,p_consumer) then raise exception 'twin port: consumer_unknown' using errcode='22023';end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop:twin-observe:'||p_world||':'||p_binding||':'||p_consumer,0));
 select * into o from runtime.nexloop_twin_observations where tenant_id=v_tenant and world=p_world and binding_id=p_binding and consumer_id=p_consumer and step=p_step;
 if not found then
  -- The one assembler (S7b); the caller never gives the pack.
  v_pack:=authz.nexloop_sim_context_command(p_digest,p_world,p_consumer,p_limit);
  perform set_config('eios.tenant_id',v_tenant,true);
  v_text:=v_pack::text;
  v_seen:=authz.nexloop_sim_consumer_observe(p_digest,p_world,p_consumer,0);
  perform set_config('eios.tenant_id',v_tenant,true);
  insert into runtime.nexloop_twin_observations(tenant_id,world,binding_id,consumer_id,step,event_limit,pack_text,pack_digest,observations,observation_digest)
   values(v_tenant,p_world,p_binding,p_consumer,p_step,p_limit,v_text,encode(sha256(convert_to(v_text,'UTF8')),'hex'),v_seen,
    encode(sha256(convert_to(v_text||'|'||v_seen::text,'UTF8')),'hex'))
   returning * into o;
 elsif o.event_limit<>p_limit then raise exception 'twin port: step_observed' using errcode='22023';
 end if;
 return jsonb_build_object('binding_id',o.binding_id,'consumer_id',o.consumer_id,'step',o.step,'pack_text',o.pack_text,'pack_digest',o.pack_digest,
  'observations',o.observations,'observation_digest',o.observation_digest,'data_mode','simulation');
end $$;

-- 3. act ----------------------------------------------------------------------------------------------------------------------------
create function authz.nexloop_twin_act(p_digest text,p_world text,p_binding text,p_consumer text,p_step integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;b runtime.nexloop_twin_bindings;o runtime.nexloop_twin_observations;p runtime.nexloop_twin_event_provenance;t control.nexloop_consumer_twins;
 v_behaviors jsonb;v_choice record;v_cal text;v_event jsonb;v_result jsonb;
begin
 if session_user is distinct from 'nexloop_twin_port' then raise exception 'twin port: unavailable' using errcode='42501';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 if coalesce(p_binding,'')!~'^[0-9a-f]{64}$' or coalesce(p_consumer,'')!~'^[0-9a-f]{64}$' or p_step is null or p_step not between 1 and 1000000 then
  raise exception 'twin port: invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 b:=runtime.nexloop_twin_binding(v_tenant,p_world,p_binding);
 perform pg_advisory_xact_lock(hashtextextended('nexloop:twin-observe:'||p_world||':'||p_binding||':'||p_consumer,0));
 select * into o from runtime.nexloop_twin_observations where tenant_id=v_tenant and world=p_world and binding_id=p_binding and consumer_id=p_consumer and step=p_step;
 if not found then raise exception 'twin port: step_not_observed' using errcode='22023';end if;
 select * into p from runtime.nexloop_twin_event_provenance where tenant_id=v_tenant and world=p_world and binding_id=p_binding and consumer_id=p_consumer and step=p_step;
 if not found then
  select * into t from control.nexloop_consumer_twins where tenant_id=v_tenant and twin_id=b.twin_id and version=b.twin_version;
  if runtime.nexloop_consumer_twin_status(v_tenant,b.twin_id,b.twin_version) is distinct from 'published' then raise exception 'twin port: twin_retired' using errcode='22023';end if;
  v_cal:=runtime.nexloop_consumer_twin_calibration(v_tenant,b.twin_id,b.twin_version);
  -- The decision layer (T2): seed | definition digest | consumer; the step is the instance. It never reads the observation.
  v_behaviors:=runtime.nexloop_twin_decide(b.seed||'|'||b.twin_digest||'|'||p_consumer,p_step,t.definition->'parameters'->'behaviors');
  select x.key behavior,runtime.nexloop_twin_behavior_kind(x.key) kind into v_choice from jsonb_each(v_behaviors) x
   where (x.value->>'simulated_outcome')::boolean
   order by array_position(array['refuse_contact','payment_intent','message','visit'],runtime.nexloop_twin_behavior_kind(x.key)),x.key limit 1;
  if v_choice.behavior is not null then
   v_event:=jsonb_build_object('consumer_id',p_consumer,'event_kind',v_choice.kind,
    'content',(case when v_choice.kind='message' then jsonb_build_object('text','合成消息（孪生行为 '||v_choice.behavior||'）') else '{}'::jsonb end)
     ||jsonb_build_object('attributes',jsonb_build_object('behavior',v_choice.behavior,'twin_id',b.twin_id,'twin_version',b.twin_version,
       'twin_seed',b.seed,'twin_step',p_step,'calibration_status',v_cal,'origin','twin')),
    'twin',jsonb_build_object('version',b.port_version,'seed',b.seed,'step',p_step));
   -- The S7 port entry: the world's event ceiling, the sequence and the world time are its own.
   v_result:=authz.nexloop_sim_consumer_act(p_digest,p_world,v_event);
   perform set_config('eios.tenant_id',v_tenant,true);
  end if;
  insert into runtime.nexloop_twin_event_provenance(tenant_id,world,binding_id,consumer_id,step,event_id,behaviors,behavior,event_kind,twin_id,twin_version,
    twin_digest,seed,calibration_status,observation_digest)
   values(v_tenant,p_world,p_binding,p_consumer,p_step,v_result->'frozen'->>'event_id',v_behaviors,v_choice.behavior,v_choice.kind,b.twin_id,b.twin_version,
    b.twin_digest,b.seed,v_cal,o.observation_digest)
   returning * into p;
 end if;
 return jsonb_build_object('binding_id',p.binding_id,'consumer_id',p.consumer_id,'step',p.step,'behaviors',p.behaviors,'behavior',p.behavior,
  'event_kind',p.event_kind,'event_id',p.event_id,
  'event',(select jsonb_build_object('sequence',e.sequence,'content',e.content,'content_digest',e.content_digest,'port_version',e.twin_version,
     'sim_time',runtime.nexloop_commercial_ts(e.sim_time),'data_mode',e.data_mode,'output_kind',e.output_kind,'origin',e.origin)
    from runtime.nexloop_sim_consumer_events e where e.tenant_id=v_tenant and e.world=p_world and e.event_id=p.event_id),
  'provenance',jsonb_build_object('twin_id',p.twin_id,'twin_version',p.twin_version,'twin_digest',p.twin_digest,'seed',p.seed,'step',p.step,
    'calibration_status',p.calibration_status,'observation_digest',p.observation_digest,'data_mode',p.data_mode,'output_kind',p.output_kind,'origin',p.origin));
end $$;

do $$declare f text;begin
 foreach f in array array['authz.nexloop_twin_bind(text,text,text,integer,text)','authz.nexloop_twin_observe(text,text,text,text,integer,integer)',
   'authz.nexloop_twin_act(text,text,text,text,integer)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public';
  execute 'grant execute on function '||f||' to nexloop_twin_port';
 end loop;
end $$;
