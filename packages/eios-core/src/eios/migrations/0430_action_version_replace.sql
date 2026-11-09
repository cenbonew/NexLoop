-- D23 option B, batch 0 (docs/implementation/d23-option-b-design.md §2, §2a; owner 2026-11-09): trusted configuration may replace a
-- published Action version by its next version, one for one. The migration number lives only in the file name and the catalog.
--
-- catalog_publish.publish only adds the definitions a tenant lacks, and authz.nexloop_workbench_action_for (0125) refuses a capability
-- with two active definitions ('published more than once'). So when the configurator (control.nexloop_configure_manifest, session
-- user nexloop_configurator) inserts an Action whose definition names a previous_version (an exact DefinitionReference: stable name,
-- version, contract digest) of version - 1, the same insert deactivates that previous version - and only when it is a true one-for-one
-- replacement:
--   * the reference names this tenant and definition type 'action', and carries a contract digest (never null: null equal to null
--     is not a match); the previous version exists in the same tenant and world with that same non-null digest, is active, and has
--     exactly the same capability_binding (name, version and schema hash);
--   * it is the only active definition of that stable name;
--   * the new resource id is eios:action:<stable_name>:<version>.
-- Anything else with a previous_version is refused (22023 'action replacement invalid'). So is a version 2 or higher without a
-- previous_version while the name already has an active definition: it would leave two active ones. A replay of the same manifest (the row is
-- already there with the same definition) changes nothing. Rows any other path inserts (NX-040 successors, simulation catalogs,
-- tests) are untouched: the rule is the configurator's only. Published definitions stay immutable (0008): only active moves.
-- Consequence for trusted configuration: control.nexloop_configure_manifest requires every Action it lists to be stored active, so once
-- a version is replaced the manifest lists only the new one (design §2a).
create function control.nexloop_action_version_replace() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_previous integer;v_name text:=new.definition->>'stable_name';
 v_version integer:=(new.definition->>'version')::integer;v_old control.nexloop_action_definitions;n integer;
begin
 -- World real only (configure_manifest writes no other): a simulation world's definitions are never deactivated (NX-041 review 1.3).
 if session_user is distinct from 'nexloop_configurator' or new.world is distinct from 'real' then return new;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 if exists(select 1 from control.nexloop_action_definitions d where d.tenant_id=new.tenant_id and d.world=new.world and d.resource_id=new.resource_id
   and d.definition=new.definition) then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return new;  -- a replay: on conflict do nothing keeps the stored row
 end if;
 if jsonb_typeof(new.definition->'previous_version') is distinct from 'object' then
  -- C5: a later version that replaces nothing, next to an active one, would leave two active definitions (0125 refuses that).
  if coalesce(v_version,1)>=2 and exists(select 1 from control.nexloop_action_definitions d where d.tenant_id=new.tenant_id and d.world=new.world
    and d.active and d.definition->>'stable_name'=v_name) then
   raise exception 'action replacement invalid' using errcode='22023';end if;
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return new;
 end if;
 v_previous:=case when new.definition->'previous_version'->>'version'~'^[1-9][0-9]{0,8}$' then (new.definition->'previous_version'->>'version')::integer end;
 select * into v_old from control.nexloop_action_definitions d where d.tenant_id=new.tenant_id and d.world=new.world
  and d.resource_id='eios:action:'||v_name||':'||v_previous;
 select count(*) into n from control.nexloop_action_definitions d where d.tenant_id=new.tenant_id and d.world=new.world and d.active
  and d.definition->>'stable_name'=v_name;
 if v_previous is distinct from v_version-1 or new.resource_id is distinct from 'eios:action:'||v_name||':'||v_version
  or new.definition->'previous_version'->>'stable_name' is distinct from v_name
  or new.definition->'previous_version'->>'tenant_id' is distinct from new.tenant_id
  or new.definition->'previous_version'->>'definition_type' is distinct from 'action'
  or coalesce(new.definition->'previous_version'->>'contract_digest','')!~'^[0-9a-f]{64}$'
  or v_old.resource_id is null or not v_old.active or n<>1
  or v_old.definition->>'contract_digest' is null
  or v_old.definition->>'contract_digest' is distinct from new.definition->'previous_version'->>'contract_digest'
  or v_old.definition->'capability_binding' is distinct from new.definition->'capability_binding' then
  raise exception 'action replacement invalid' using errcode='22023';
 end if;
 update control.nexloop_action_definitions set active=false where tenant_id=v_old.tenant_id and world='real' and resource_id=v_old.resource_id;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return new;
end $$;
alter function control.nexloop_action_version_replace() owner to nexloop_owner;
revoke all on function control.nexloop_action_version_replace() from public;
create trigger nexloop_action_version_replace before insert on control.nexloop_action_definitions
 for each row execute function control.nexloop_action_version_replace();
