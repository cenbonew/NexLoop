-- NX-041 D10 follow-up (dispatcher 2026-10-21, stage enablement): the owner publishes the Action catalog of a simulation world. A
-- simulation Run needs its Run Action published for its world (the Run credential is issued against that world's definitions); until
-- now only the tests seeded it (a direct insert). The migration number lives only in the file name and the catalog.
-- 1. Governed owner Action sim_world.publish_catalog / publish_sim_catalog (subject human, owner only by the role manifest, from real):
--    copies the tenant's active real Action definitions on the whitelist into one registered, active sim:/shadow: world of the tenant.
--    The whitelist is the stand-in catalog (S4 control.nexloop_stand_ins, D11): only Actions whose effect has a stand-in - in that world
--    their effect can only take the stand-in path (S4), and a real effect table never holds a simulation row (0262's
--    nx041_real_effect_world CHECK). An effect without a stand-in (e.g. enterprise delivery) is never copied (fail closed, D11).
-- 2. Only the definition and its capability are copied - no credential, authority fact, grant, effect category declaration or
--    configuration; the world's own credentials come from the trusted configuration (sim_world_credentials.py).
-- 3. Repeatable: a definition already in the world with the same content is counted, not written again; one with other content is
--    refused (catalog_conflict). real and test, an unregistered or no longer active world, another tenant's world: refused.
-- 4. The rows leave with the world (S2 whole deletion: every table with a world column).

create function control.nexloop_governed_publish_sim_catalog(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_target text:=body->>'world';x record;n_copied integer:=0;n_present integer:=0;v_refs text[]:='{}';
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','world','reason'))
  or jsonb_typeof(body->'world') is distinct from 'string' or jsonb_typeof(body->'reason') is distinct from 'string'
  or char_length(btrim(body->>'reason')) not between 1 and 500 then
  raise exception 'sim world: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'sim world: owner_world_only' using errcode='22023';end if;
 if coalesce(runtime.nexloop_world_kind(v_target),'') not in ('simulation','shadow') then
  raise exception 'sim world: not_a_simulation_world' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if control.nexloop_sim_world_status(p_tenant,v_target) is distinct from 'active' then
  raise exception 'sim world: not_active' using errcode='22023';end if;
 perform pg_advisory_xact_lock(hashtextextended('nexloop:sim-catalog:'||p_tenant||':'||v_target,0));
 for x in select a.resource_id,a.definition,a.capability from control.nexloop_action_definitions a
   where a.tenant_id=p_tenant and a.world='real' and a.active
     and exists(select 1 from control.nexloop_stand_ins s where s.action_name=a.definition->>'stable_name' and s.action_version=(a.definition->>'version')::integer)
   order by a.resource_id loop
  if exists(select 1 from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world=v_target and d.resource_id=x.resource_id) then
   if not exists(select 1 from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world=v_target and d.resource_id=x.resource_id
                 and d.definition=x.definition and d.capability=x.capability and d.active) then
    raise exception 'sim world: catalog_conflict' using errcode='22023';end if;
   n_present:=n_present+1;
  else
   insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability,active)
    values(p_tenant,v_target,x.resource_id,x.definition,x.capability,true);
   n_copied:=n_copied+1;
  end if;
  v_refs:=v_refs||x.resource_id;
 end loop;
 if n_copied>0 then
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('sim-catalog:'||v_target||':'||md5(array_to_string(v_refs,',')),'nexloop.sim_world.catalog_published',p_tenant,p_principal,'sim_world',p_intent,
    jsonb_build_object('world',v_target,'actions',to_jsonb(v_refs),'reason',btrim(body->>'reason')),clock_timestamp())
   on conflict do nothing;
 end if;
 return jsonb_build_object('world',v_target,'actions',to_jsonb(v_refs),'copied',n_copied,'already_present',n_present);
end $$;
alter function control.nexloop_governed_publish_sim_catalog(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_publish_sim_catalog(text,text,text,text,text,jsonb) from public;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('sim_world.publish_catalog','publish_sim_catalog','human','control.nexloop_governed_publish_sim_catalog(text,text,text,text,text,jsonb)','NX-041');

-- 5. The authority epoch (0004, replaced): a simulation or shadow world's Action definition no longer advances the tenant's authority
--    revision. The revision guards every authorization decision of the tenant (real and test included) and the governed entry
--    re-asserts it at its commit tail, so a governed Action writing definitions - the catalog publication above - would always end
--    stale. A simulation world's definitions only ever serve that world's own credentials (bound to it alone, D10); they never enter a
--    real or test decision, and the world's whole deletion revokes those credentials. Every other row keeps advancing the revision.
--    Premise (L6 review): a simulation definition has two write paths only - this publication (insert) and the S2 whole deletion after
--    its credentials are revoked; nothing deactivates or updates one (tests/test_nx041_sim_catalog_publish_pg.py guards the writer list).
--    A path that deactivates a simulation definition must first give the world its own authority revision (review §5).
--    The database holds it (6 below); the writer-list test is a supplement - it scans function bodies only, so a migration's own DML is
--    its blind spot (as are dynamic SQL and merge), which is why 6 exists.
create or replace function authz.nexloop_authority_epoch_guard() returns trigger language plpgsql security definer
 set search_path=pg_catalog,pg_temp as $$
begin
 if tg_table_name='nexloop_action_definitions' then
  -- An update skips only when the old and the new row are both of a simulation world (defence in depth beside 0008's immutability).
  if coalesce(runtime.nexloop_world_kind((case when tg_op='DELETE' then to_jsonb(old) else to_jsonb(new) end)->>'world'),'') in ('simulation','shadow')
     and (tg_op<>'UPDATE' or coalesce(runtime.nexloop_world_kind(to_jsonb(old)->>'world'),'') in ('simulation','shadow')) then
   return null;end if;
 end if;
 update control.nexloop_tenants set authority_revision=authority_revision+1
  where tenant_id=case when tg_op='DELETE' then old.tenant_id else new.tenant_id end;
 return null;
end $$;

-- 6. The premise of 5 held by the database (L6 review A1), not only by the writer-list test: a simulation or shadow world's Action
--    definition is never updated (no deactivation path; old or new of that kind), and leaves only by the S2 whole deletion (the purge
--    opens the table for that world through control.nexloop_erasure_scope, as 0262's sim_ledger_guard). A truncate is refused while any
--    such row exists. This catches dynamic SQL, on conflict do update, merge and a later migration's DML alike (the scan test cannot).
create function control.nexloop_sim_definition_guard() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='TRUNCATE' then
  if exists(select 1 from control.nexloop_action_definitions d where runtime.nexloop_world_kind(d.world) in ('simulation','shadow')) then
   raise exception 'simulation Action definitions leave only with their world' using errcode='42501';end if;
  return null;end if;
 if tg_op='DELETE' then
  if coalesce(runtime.nexloop_world_kind(old.world),'') not in ('simulation','shadow')
     or runtime.nexloop_erasure_permits(current_user,'control.nexloop_action_definitions','DELETE',to_jsonb(old),null) then return old;end if;
  raise exception 'simulation Action definitions leave only with their world' using errcode='42501';end if;
 if coalesce(runtime.nexloop_world_kind(old.world),'') in ('simulation','shadow') or coalesce(runtime.nexloop_world_kind(new.world),'') in ('simulation','shadow') then
  raise exception 'a simulation Action definition is never updated' using errcode='42501';end if;
 return new;
end $$;
alter function control.nexloop_sim_definition_guard() owner to nexloop_owner;
revoke all on function control.nexloop_sim_definition_guard() from public;
create trigger nx041_sim_definition_guard before update or delete on control.nexloop_action_definitions
 for each row execute function control.nexloop_sim_definition_guard();
create trigger nx041_sim_definition_no_truncate before truncate on control.nexloop_action_definitions
 for each statement execute function control.nexloop_sim_definition_guard();
