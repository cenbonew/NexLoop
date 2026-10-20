-- NX-041 sim context by reference and the simulation manifest (L6 nx041-host-sim-context-review 4.3: source and audit). The
-- migration number lives only in the file name and the catalog; nothing below repeats it.
-- 1. By reference: authz.nexloop_sim_context_register(digest, world, run_id, consumer, limit) - the caller names the Run, the Consumer
--    and the limit only; in the same transaction SQL assembles the pack with authz.nexloop_sim_context_command (S7b, the only
--    assembler), takes its jsonb text as the Run's input text, fixes it with its sha256 to the Run (runtime.nexloop_sim_context_bindings)
--    and returns it. The Run must be a Run credential of that world issued from the caller's own credential. A repeat returns the
--    fixed pack; nothing is assembled twice.
-- 2. The Run registration (authz.nexloop_runtime_run_bindings, 0037) of a sim:/shadow: Run accepts only that fixed input: its
--    input_digest must be the bound pack's digest, so a pack the caller assembled itself is refused. The trigger fires always
--    (replica included); real and test rows pass untouched (the real path is unchanged).
-- 3. Manifest: runtime.nexloop_sim_context_sources - for every bound Run, each simulated source read (section, ref, revision,
--    content_hash) and, on the binding, the pack digest, the world time, the event count and limit: what the model was given and from
--    which events. Append-only; both tables have a world column and leave with the world (S2 whole deletion).

create table runtime.nexloop_sim_context_bindings (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 world text not null check(coalesce(runtime.nexloop_world_kind(world),'') in ('simulation','shadow')),
 run_id uuid not null references authz.nexloop_run_credentials(run_id),
 consumer_id text not null check(consumer_id~'^[0-9a-f]{64}$'),
 event_limit integer not null check(event_limit between 1 and 256),
 pack_text text not null,
 pack_digest text not null check(pack_digest~'^[0-9a-f]{64}$'),
 world_time text not null,
 events_included integer not null check(events_included>=0),
 registered_by text not null check(char_length(registered_by) between 1 and 200),
 registered_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,run_id),
 check(pack_digest=encode(sha256(convert_to(pack_text,'UTF8')),'hex')));
create table runtime.nexloop_sim_context_sources (
 tenant_id text not null,world text not null check(coalesce(runtime.nexloop_world_kind(world),'') in ('simulation','shadow')),
 run_id uuid not null,ordinal integer not null check(ordinal>=1),
 section text not null check(section in ('consumer_state','evidence','current_event')),
 ref text not null check(ref~'^sim:(object|persona|event):[0-9a-f]{64}$'),
 revision text not null,content_hash text check(content_hash~'^[0-9a-f]{64}$'),
 primary key(tenant_id,world,run_id,ordinal),
 foreign key(tenant_id,world,run_id) references runtime.nexloop_sim_context_bindings(tenant_id,world,run_id));
create index nexloop_sim_context_bindings_run on runtime.nexloop_sim_context_bindings(run_id);
do $$declare t text;begin
 foreach t in array array['runtime.nexloop_sim_context_bindings','runtime.nexloop_sim_context_sources'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_stand_in_worker,nexloop_twin_port,nexloop_sim_worker,nexloop_sim_guard,nexloop_sim_api',t);
  -- Append-only; only the S2 whole deletion (its erasure scope) removes rows (S4 runtime.nexloop_sim_ledger_guard).
  execute format('create trigger nx041_sim_context_guard before update or delete on %s for each row execute function runtime.nexloop_sim_ledger_guard()',t);
  execute format('create trigger nx041_sim_context_no_truncate before truncate on %s for each statement execute function runtime.nexloop_sim_ledger_guard()',t);
 end loop;
end $$;

-- 1. Register by reference --------------------------------------------------------------------------------------------------------
create function authz.nexloop_sim_context_register(p_digest text,p_world text,p_run uuid,p_consumer text,p_limit integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text;r authz.nexloop_run_credentials;b runtime.nexloop_sim_context_bindings;v_pack jsonb;v_text text;v_principal text;
begin
 if authz.nexloop_app_role(p_world) is distinct from 'nexloop_api' then raise exception 'sim context: unavailable' using errcode='42501';end if;
 if p_run is null or coalesce(p_consumer,'')!~'^[0-9a-f]{64}$' or p_limit is null or p_limit not between 1 and 256 then
  raise exception 'sim context: invalid' using errcode='22023';end if;
 v_tenant:=authz.nexloop_sim_effect_tenant(p_digest,p_world);
 perform set_config('eios.tenant_id',v_tenant,true);
 -- The Run: a Run credential of this world issued from the caller's own credential (never another world's or another source's).
 select * into r from authz.nexloop_run_credentials where run_id=p_run for share;
 if not found or r.world is distinct from p_world or r.source_digest is distinct from p_digest or r.status<>'active' then
  raise exception 'sim context: run_unavailable' using errcode='42501';end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop:sim-context:'||p_run::text,0));
 select * into b from runtime.nexloop_sim_context_bindings where tenant_id=v_tenant and world=p_world and run_id=p_run;
 if found then
  if b.consumer_id<>p_consumer or b.event_limit<>p_limit then raise exception 'sim context: run_bound' using errcode='22023';end if;
  return jsonb_build_object('run_id',p_run,'pack_text',b.pack_text,'pack_digest',b.pack_digest,'registered',false);
 end if;
 -- The only assembler (S7b): the caller never gives the pack.
 v_pack:=authz.nexloop_sim_context_command(p_digest,p_world,p_consumer,p_limit);
 perform set_config('eios.tenant_id',v_tenant,true);
 v_text:=v_pack::text;
 v_principal:=authz.nexloop_service_identity(p_digest,p_world)->'binding'->>'principal_id';
 insert into runtime.nexloop_sim_context_bindings(tenant_id,world,run_id,consumer_id,event_limit,pack_text,pack_digest,world_time,events_included,registered_by)
  values(v_tenant,p_world,p_run,p_consumer,p_limit,v_text,encode(sha256(convert_to(v_text,'UTF8')),'hex'),v_pack->>'world_time',
   (v_pack->'budget_report'->>'events_included')::integer,coalesce(v_principal,'service'))
  returning * into b;
 -- Manifest: every simulated source the pack carries, in pack order.
 insert into runtime.nexloop_sim_context_sources(tenant_id,world,run_id,ordinal,section,ref,revision,content_hash)
  select v_tenant,p_world,p_run,row_number() over (order by x.o,x.i),x.section,x.ref,x.revision,x.content_hash from (
   select 1 o,i,'consumer_state' section,e->>'ref' ref,e->>'revision' revision,e->>'content_hash' content_hash
    from jsonb_array_elements(v_pack->'consumer_state') with ordinality s(e,i)
   union all
   select 2,i,'evidence',e->>'ref',e->>'revision',e->>'content_hash' from jsonb_array_elements(v_pack->'evidence') with ordinality s(e,i)
   union all
   select 3,1,'current_event',v_pack->'current_event'->>'provenance',v_pack->'current_event'->>'sequence',null
    where jsonb_typeof(v_pack->'current_event')='object') x;
 return jsonb_build_object('run_id',p_run,'pack_text',b.pack_text,'pack_digest',b.pack_digest,'registered',true);
end $$;
alter function authz.nexloop_sim_context_register(text,text,uuid,text,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_sim_context_register(text,text,uuid,text,integer) from public;
grant execute on function authz.nexloop_sim_context_register(text,text,uuid,text,integer) to nexloop_sim_api;

-- 2. A simulation Run registers only its fixed pack ------------------------------------------------------------------------------
create function authz.nexloop_sim_run_input_fixed() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_ok boolean;
begin
 if coalesce(runtime.nexloop_world_kind(new.world),'') not in ('simulation','shadow') then return new;end if;  -- real and test: unchanged
 perform set_config('eios.tenant_id',new.tenant_id,true);
 select exists(select 1 from runtime.nexloop_sim_context_bindings b where b.tenant_id=new.tenant_id and b.world=new.world and b.run_id=new.run_id
   and b.pack_digest=new.input_digest) into v_ok;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 if not v_ok then raise exception 'sim context: input_not_registered' using errcode='42501';end if;
 return new;
end $$;
alter function authz.nexloop_sim_run_input_fixed() owner to nexloop_owner;
revoke all on function authz.nexloop_sim_run_input_fixed() from public;
create trigger nx041_sim_run_input_fixed before insert or update of input_digest,world,run_id on authz.nexloop_runtime_run_bindings
 for each row execute function authz.nexloop_sim_run_input_fixed();
alter table authz.nexloop_runtime_run_bindings enable always trigger nx041_sim_run_input_fixed;
