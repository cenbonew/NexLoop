-- NX-041 D10 (owner decision, as recommended): a simulation or shadow world holds three service credentials, one per use - api (the
-- event intake), worker (the runtime worker and its guard) and agent (the Agent side) - each bound to that world alone; deleting the
-- world revokes all three (S2, unchanged: it revokes every active credential of the world). The migration number lives only in the
-- file name and the catalog; nothing below repeats it.
-- 1. authz.nexloop_service_credentials.sim_use: the use of a simulation credential (api | worker | agent); null for real and test, never
--    null for a sim:/shadow: credential (CHECK); existing simulation credentials were the one credential of their world: api.
-- 2. One active credential per world and use: the partial unique index (concurrent inserts) and the trigger (a readable refusal); the
--    use never changes once registered, like the world. Every other S2 rule stays: worlds immutable, never reactivated, a principal
--    never shared with a real or test credential, the world registered and active.
-- 3. The simulation Run path (S7b wiring order: Run credential, sim_context_register, Run registration with the fixed pack): the
--    simulation intake nexloop_sim_api issues Run credentials (0034 body plus an app_role check: a sim role in its own world only)
--    and accepts and registers Runs (queue_command / runtime_activation_command, which already read app_role, S6c).

alter table authz.nexloop_service_credentials add column sim_use text check(sim_use in ('api','worker','agent'));
alter table authz.nexloop_service_credentials disable trigger nx041_sim_world_credential;
update authz.nexloop_service_credentials set sim_use='api' where cardinality(worlds)=1 and worlds[1]~'^(sim|shadow):' and sim_use is null;
alter table authz.nexloop_service_credentials enable trigger nx041_sim_world_credential;
alter table authz.nexloop_service_credentials add constraint nexloop_service_credentials_sim_use
 check((sim_use is not null)=(cardinality(worlds)=1 and worlds[1]~'^(sim|shadow):')) not valid;
alter table authz.nexloop_service_credentials validate constraint nexloop_service_credentials_sim_use;
drop index authz.nexloop_service_credentials_one_active_sim;
create unique index nexloop_service_credentials_one_active_sim_use on authz.nexloop_service_credentials((worlds[1]),sim_use)
 where status='active' and sim_use is not null;

-- The S2 trigger body (0260), with the use rule in place of the one-credential rule and the use as immutable as the world.
create or replace function authz.nexloop_sim_world_credential_check() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare v_sim text[];v_principals text[];
begin
 v_sim:=array(select w from unnest(new.worlds) w where runtime.nexloop_world_kind(w) in ('simulation','shadow'));
 v_principals:=array_remove(array[new.binding->>'principal_id',new.binding->>'subject_principal_id'],null);
 if tg_op='UPDATE' then
  if (old.worlds is distinct from new.worlds or old.sim_use is distinct from new.sim_use)
     and (exists(select 1 from unnest(old.worlds||new.worlds) w where runtime.nexloop_world_kind(w) in ('simulation','shadow'))) then
   raise exception 'a simulation world credential is registered, never changed' using errcode='42501';end if;
  if (new.tenant_id,new.binding) is not distinct from (old.tenant_id,old.binding) then
   if cardinality(v_sim)=0 or new.status<>'active' or old.status='active' then return new;end if;
   raise exception 'a revoked simulation world credential is never reactivated' using errcode='42501';
  end if;
  if cardinality(v_sim)>0 and new.status='active' and old.status<>'active' then
   raise exception 'a revoked simulation world credential is never reactivated' using errcode='42501';end if;
  -- A changed principal binding (review 2.2) passes the same checks as a new credential below.
 end if;
 if cardinality(v_sim)=0 then
  -- A real or test credential never shares a principal with a simulation world credential.
  if exists(select 1 from authz.nexloop_service_credentials c where c.tenant_id=new.tenant_id
     and exists(select 1 from unnest(c.worlds) w where runtime.nexloop_world_kind(w) in ('simulation','shadow'))
     and array[c.binding->>'principal_id',c.binding->>'subject_principal_id'] && v_principals) then
   raise exception 'a simulation world principal never holds a real or test credential' using errcode='42501';end if;
  return new;
 end if;
 if new.worlds<>v_sim or cardinality(new.worlds)<>1 then
  raise exception 'a simulation world credential holds exactly its one world' using errcode='42501';end if;
 if control.nexloop_sim_world_status(new.tenant_id,new.worlds[1]) is distinct from 'active' then
  raise exception 'simulation world not registered or not active' using errcode='42501';end if;
 if new.status<>'active' then return new;end if;
 -- D10 (owner, as recommended): one active credential per world and use (api, worker, agent).
 if new.sim_use is null then raise exception 'a simulation world credential names its use (api, worker, agent)' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_service_credentials c where c.worlds=new.worlds and c.sim_use=new.sim_use and c.status='active' and c.token_digest<>new.token_digest) then
  raise exception 'a simulation world has one active credential per use' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_service_credentials c where c.tenant_id=new.tenant_id
    and exists(select 1 from unnest(c.worlds) w where runtime.nexloop_world_kind(w) in ('real','test'))
    and array[c.binding->>'principal_id',c.binding->>'subject_principal_id'] && v_principals) then
  raise exception 'a real or test principal is never carried into a simulation world' using errcode='42501';end if;
 return new;
end $$;

-- 3. The simulation intake's Run path ----------------------------------------------------------------------------------------------
create or replace function authz.nexloop_issue_run_credential(p_digest text,p_world text,p_text text,p_signature text)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_text::jsonb;proof jsonb;k bytea;v jsonb;raw text;ts timestamptz;expiry timestamptz;resources text[];
begin
 -- D10 wiring: the simulation intake (nexloop_sim_api) issues Run credentials of its own simulation world only (S6c app_role).
 if authz.nexloop_app_role(p_world) is distinct from 'nexloop_api' then raise exception 'Run issuance denied' using errcode='42501';end if;
 if octet_length(p_text)>1048576 then raise exception 'Run issuance denied' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active;
 if not found or c->>'protocol' is distinct from 'nexloop-run-credential-v1' or c->>'audience' is distinct from 'nexloop-agent-host'
  or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-run-credential-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (c->>'ttl_seconds')::integer not between 1 and 300
  or jsonb_typeof(c->'proofs') is distinct from 'array' or jsonb_array_length(c->'proofs') not between 1 and 32
 then raise exception 'Run issuance denied' using errcode='42501';end if;
 perform 1 from authz.nexloop_service_credentials where token_digest=p_digest for share;
 if not found then raise exception 'nested Run issuance denied' using errcode='42501';end if;
 v:=authz.nexloop_root_identity_snapshot(p_digest,p_world);
 for proof in select value from jsonb_array_elements(c->'proofs') order by value->>'resource_id' loop
  perform authz.nexloop_assert_action_authority(p_digest,p_world,proof);
 end loop;
 select array_agg(value->>'resource_id' order by value->>'resource_id') into resources from jsonb_array_elements(c->'proofs');
 if cardinality(resources)<>(select count(distinct value->>'resource_id') from jsonb_array_elements(c->'proofs')) then
  raise exception 'duplicate Run resource' using errcode='42501';end if;
 ts:=clock_timestamp();expiry:=least((v->>'expires_at')::timestamptz,ts+make_interval(secs=>(c->>'ttl_seconds')::integer));
 raw:=encode(extensions.gen_random_bytes(48),'hex');
 insert into authz.nexloop_run_credentials values(encode(sha256(convert_to(raw,'UTF8')),'hex'),
  (c->>'run_id')::uuid,p_digest,v->>'directory_hash',p_world,'nexloop-agent-host',resources,'active',ts,expiry);
 return jsonb_build_object('run_id',c->>'run_id','token',raw,'expires_at',expiry);
end $$;
grant execute on function authz.nexloop_issue_run_credential(text,text,text,text) to nexloop_sim_api;
-- Issuing a Run credential reads the Run Action's published definition (by the credential's own world).
grant execute on function authz.nexloop_read_action_bundle(text,text,text,text) to nexloop_sim_api;
-- Accepting a Run reads whether it is a Role Run (by its own Run digest).
grant execute on function authz.nexloop_role_run_hint(text,text) to nexloop_sim_api;
-- A simulation process opens its backend like a real one: the migration revision check reads control.schema_migrations (versions and
-- checksums only, as granted to the real roles in 0002). No other table of the application schemas.
grant usage on schema control to nexloop_sim_api,nexloop_sim_worker,nexloop_sim_guard;
grant select on control.schema_migrations to nexloop_sim_api,nexloop_sim_worker,nexloop_sim_guard;
grant execute on function authz.nexloop_queue_command(text,text,text,text,text) to nexloop_sim_api;
grant execute on function authz.nexloop_runtime_activation_command(text,text,text,text,text) to nexloop_sim_api;
-- The simulation worker creates activations, which read the Run Actions published definitions (the credentials own world).
grant execute on function authz.nexloop_read_action_bundle(text,text,text,text) to nexloop_sim_worker;
-- and whether the Run is a Role Run (by its own Run digest).
grant execute on function authz.nexloop_role_run_hint(text,text) to nexloop_sim_worker;
-- The simulation guard authorizes the Run (its Actions published definitions, the credentials own world)
grant execute on function authz.nexloop_read_action_bundle(text,text,text,text) to nexloop_sim_guard;
-- and reads whether it is a Role Run (by its own Run digest).
grant execute on function authz.nexloop_role_run_hint(text,text) to nexloop_sim_guard;
