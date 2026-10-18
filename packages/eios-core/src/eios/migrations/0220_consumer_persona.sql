-- Consumer persona, minimal step of ADR-034, and the owner's erasure of a synthetic Consumer (temporary number 0220; scheduler
-- 2026-10-18, option A: the AT-050 second restore drill erases a synthetic customer on stage; ADR-034 approved by the owner
-- 2026-10-18, this brings its first step forward). Nothing here changes the NX-029 erasure of a real Consumer.
-- 1. control.nexloop_consumer_personas (current value and revision) and control.nexloop_consumer_persona_events (append-only
--    history: previous and new value, who, which request, reason). No row means real (ADR-034: unmarked Consumers are real).
-- 2. runtime.nexloop_consumer_persona(tenant, world, consumer) -> real | simulated_agent | synthetic.
-- 3. Governed human Action consumer.set_persona / set_consumer_persona (0124 registry; workbench owner only): persona in
--    (real, simulated_agent, synthetic), reason 1..500; the same value again changes nothing (changed false, same revision);
--    setting real again is the undo. Refused for a Consumer that is not a Consumer object of this tenant and world.
-- 4. Governed human Action consumer.erase_synthetic / erase_synthetic_consumer (owner only): refuses with the fixed text
--    'consumer erasure: persona_real' (errcode 22023) unless the Consumer's persona is simulated_agent or synthetic, then runs
--    the unchanged NX-029 handler control.nexloop_governed_erase_consumer (same request id, same replay). The plain
--    consumer.erase stays as it is (real customers' erasure requests) and so does the deletion replay after a restore (0135
--    calls the NX-029 handler directly; a replayed erasure is not checked again).
-- 5. Workbench observe verb erasure_precheck (owner only, read only): the Consumer, its persona and revision, its erasure
--    request (state, phase, counts, times; never the reason) and what is still held: conversations, messages (and how many
--    still carry a body), Claims (and how many are not yet erased), data exports.

-- 1. Persona -------------------------------------------------------------------------------------------------------------------
create table control.nexloop_consumer_personas (
 tenant_id text not null,world text not null,consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),
 persona text not null check(persona in ('real','simulated_agent','synthetic')),revision bigint not null check(revision>0),
 updated_by text not null,updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,consumer_id)
);
create table control.nexloop_consumer_persona_events (
 event_id bigint generated always as identity primary key,
 tenant_id text not null,world text not null,consumer_id text not null check(consumer_id~'^[a-f0-9]{64}$'),
 previous text not null check(previous in ('real','simulated_agent','synthetic')),persona text not null check(persona in ('real','simulated_agent','synthetic')),
 revision bigint not null check(revision>0),set_by text not null,request_intent text not null,reason text not null check(char_length(reason) between 1 and 500),
 recorded_at timestamptz not null default clock_timestamp(),unique(tenant_id,world,consumer_id,revision)
);
do $tables$
declare t text;
begin
 foreach t in array array['control.nexloop_consumer_personas','control.nexloop_consumer_persona_events'] loop
  execute 'alter table '||t||' owner to nexloop_owner';execute 'alter table '||t||' enable row level security';
  execute 'alter table '||t||' force row level security';execute 'revoke all on '||t||' from public';
 end loop;
end $tables$;
create policy consumer_persona_tenant on control.nexloop_consumer_personas to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy consumer_persona_event_tenant on control.nexloop_consumer_persona_events to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create function control.nexloop_consumer_persona_events_append_only() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin raise exception 'consumer persona events are append-only' using errcode='42501';end $$;
alter function control.nexloop_consumer_persona_events_append_only() owner to nexloop_owner;
create trigger consumer_persona_events_append_only before update or delete on control.nexloop_consumer_persona_events
 for each row execute function control.nexloop_consumer_persona_events_append_only();

-- 2. Current value -----------------------------------------------------------------------------------------------------------
create function runtime.nexloop_consumer_persona(p_tenant text,p_world text,p_consumer text) returns text
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select coalesce((select persona from control.nexloop_consumer_personas where tenant_id=p_tenant and world=p_world and consumer_id=p_consumer),'real')
$$;
alter function runtime.nexloop_consumer_persona(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_consumer_persona(text,text,text) from public;

-- 3. set_consumer_persona ------------------------------------------------------------------------------------------------------
create function control.nexloop_governed_set_consumer_persona(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_consumer text:=body->>'consumer_id';v_persona text:=body->>'persona';cur control.nexloop_consumer_personas;v_previous text;v_revision bigint;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','consumer_id','persona','reason'))
  or coalesce(v_consumer,'')!~'^[a-f0-9]{64}$' or coalesce(v_persona,'') not in ('real','simulated_agent','synthetic')
  or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'consumer persona invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=v_consumer) then
  raise exception 'consumer unavailable' using errcode='42501';end if;
 select * into cur from control.nexloop_consumer_personas where tenant_id=p_tenant and world=p_world and consumer_id=v_consumer for update;
 v_previous:=coalesce(cur.persona,'real');
 if v_previous=v_persona then
  return jsonb_build_object('consumer_id',v_consumer,'persona',v_persona,'revision',coalesce(cur.revision,0),'changed',false);end if;
 v_revision:=coalesce(cur.revision,0)+1;
 insert into control.nexloop_consumer_personas(tenant_id,world,consumer_id,persona,revision,updated_by)
  values(p_tenant,p_world,v_consumer,v_persona,v_revision,p_principal)
  on conflict(tenant_id,world,consumer_id) do update set persona=excluded.persona,revision=excluded.revision,updated_by=excluded.updated_by,updated_at=clock_timestamp();
 insert into control.nexloop_consumer_persona_events(tenant_id,world,consumer_id,previous,persona,revision,set_by,request_intent,reason)
  values(p_tenant,p_world,v_consumer,v_previous,v_persona,v_revision,p_principal,p_intent,body->>'reason');
 return jsonb_build_object('consumer_id',v_consumer,'persona',v_persona,'previous',v_previous,'revision',v_revision,'changed',true);
end $$;

-- 4. erase_synthetic_consumer --------------------------------------------------------------------------------------------------
create function control.nexloop_governed_erase_synthetic_consumer(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_consumer text:=body->>'consumer_id';v_persona text;
begin
 if exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','consumer_id','reason'))
  or coalesce(v_consumer,'')!~'^[a-f0-9]{64}$' or length(coalesce(body->>'reason','')) not between 1 and 500 then
  raise exception 'consumer erasure invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if not exists(select 1 from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=v_consumer)
  and not exists(select 1 from control.nexloop_consumer_erasures where tenant_id=p_tenant and world=p_world and consumer_id=v_consumer) then
  raise exception 'consumer unavailable' using errcode='42501';end if;
 v_persona:=runtime.nexloop_consumer_persona(p_tenant,p_world,v_consumer);
 if v_persona='real' then raise exception 'consumer erasure: persona_real' using errcode='22023';end if;
 return control.nexloop_governed_erase_consumer(p_tenant,p_world,p_principal,p_subject,p_intent,
  jsonb_build_object('operation','erase_consumer','request_id',body->>'request_id','consumer_id',v_consumer,'reason',body->>'reason'))
  ||jsonb_build_object('persona',v_persona);
end $$;

do $owners$
declare f text;
begin
 foreach f in array array['control.nexloop_governed_set_consumer_persona(text,text,text,text,text,jsonb)',
   'control.nexloop_governed_erase_synthetic_consumer(text,text,text,text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('consumer.set_persona','set_consumer_persona','human','control.nexloop_governed_set_consumer_persona(text,text,text,text,text,jsonb)','NX-029'),
 ('consumer.erase_synthetic','erase_synthetic_consumer','human','control.nexloop_governed_erase_synthetic_consumer(text,text,text,text,text,jsonb)','NX-029');

-- 5. The owner's erasure precheck (workbench observe; 0210 body kept for every other verb) ----------------------------------
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_persona_v0220;
revoke all on function authz.nexloop_workbench_observe_read_before_persona_v0220(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;
 v_action text:='eios:action:nexloop.workbench.read:1';v_consumer text;p control.nexloop_consumer_personas;e control.nexloop_consumer_erasures;
begin
 if c->>'verb' is distinct from 'erasure_precheck' then
  return authz.nexloop_workbench_observe_read_before_persona_v0220(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0133 verification, for this verb (as 0210).
 if octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-workbench-observe-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is distinct from v_action or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-observe-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench observe unavailable' using errcode='42501';end if;
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->(w->>'role') ? v_action)
  or w->>'role'<>'owner' then
  raise exception 'workbench observe forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 v_consumer:=c->>'consumer_id';
 if coalesce(v_consumer,'')!~'^[a-f0-9]{64}$' or (select count(*) from jsonb_object_keys(c))<>2 then
  raise exception 'workbench observe invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into p from control.nexloop_consumer_personas where tenant_id=v_tenant and world=p_world and consumer_id=v_consumer;
 select * into e from control.nexloop_consumer_erasures where tenant_id=v_tenant and world=p_world and consumer_id=v_consumer;
 return jsonb_build_object('consumer_id',v_consumer,
  'consumer_object',exists(select 1 from ontology.objects where tenant_id=v_tenant and world=p_world and type_name='Consumer' and object_id=v_consumer),
  'persona',coalesce(p.persona,'real'),'persona_revision',coalesce(p.revision,0),
  'erasure',case when e.request_id is null then null else jsonb_build_object('request_id',e.request_id,'source',e.source,'state',e.state,'phase',e.phase,
   'counts',e.counts,'created_at',e.created_at,'updated_at',e.updated_at,'completed_at',e.completed_at) end,
  'held',jsonb_build_object(
   'conversations',(select count(*) from runtime.nexloop_conversations v where v.tenant_id=v_tenant and v.world=p_world and v.consumer_id=v_consumer),
   'messages',(select count(*) from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations v on v.tenant_id=m.tenant_id and v.world=m.world
     and v.conversation_id=m.conversation_id where v.tenant_id=v_tenant and v.world=p_world and v.consumer_id=v_consumer),
   'messages_with_body',(select count(*) from runtime.nexloop_conversation_messages m join runtime.nexloop_conversations v on v.tenant_id=m.tenant_id and v.world=m.world
     and v.conversation_id=m.conversation_id where v.tenant_id=v_tenant and v.world=p_world and v.consumer_id=v_consumer
     and coalesce(m.record->>'erased','false')<>'true' and coalesce(m.record->>'body','')<>''),
   'claims',(select count(*) from ontology.nexloop_claims x where x.tenant_id=v_tenant and x.world=p_world and x.consumer_id=v_consumer),
   'claims_not_erased',(select count(*) from ontology.nexloop_claims x where x.tenant_id=v_tenant and x.world=p_world and x.consumer_id=v_consumer
     and coalesce(x.value->>'type','')<>'erased'),
   'data_exports',(select count(*) from control.nexloop_data_exports d where d.tenant_id=v_tenant and d.world=p_world and d.consumer_id=v_consumer)));
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
