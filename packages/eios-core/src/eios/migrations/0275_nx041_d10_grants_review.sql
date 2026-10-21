-- NX-041 D10 grants review (L2, P1): the two entries D10 granted to the simulation roles get S6c's second defence. The migration
-- number lives only in the file name and the catalog; nothing below repeats it.
-- authz.nexloop_read_action_bundle (the 0016 body) and authz.nexloop_role_run_hint (the 0063 body) refuse a simulation role
-- (nexloop_sim_api / _worker / _guard) called for a real or test world - authz.nexloop_app_role(p_world) names it
-- 'nexloop_sim_outside_its_world' there - even with a real credential's digest in hand. A real role's app_role is its own
-- session_user, so the real path is unchanged. Definer search_path gains pg_temp (the definer check).

create or replace function authz.nexloop_read_action_bundle(p_digest text,p_world text,p_text text,p_signature text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare bundle jsonb;r jsonb;payload jsonb;schemas jsonb:='[]'::jsonb;b control.nexloop_relation_action_bindings%rowtype;v_relation jsonb;
begin
 -- S6c second defence (L2 D10 grants review P1): a simulation role never reads here outside its own simulation world.
 if authz.nexloop_app_role(p_world)='nexloop_sim_outside_its_world' then raise exception 'simulation role outside its world' using errcode='42501';end if;
 bundle:=authz.nexloop_read_action_definition(p_digest,p_world,p_text,p_signature);
 for r in select distinct value from jsonb_array_elements((bundle->'definition'->'object_types')||coalesce(bundle->'definition'->'governance'->'change_scope'->'object_types','[]'::jsonb)) loop
  if r->>'tenant_id' is distinct from p_text::jsonb->>'tenant_id' or r->>'schema_type' is distinct from 'object_type' then
   raise exception 'unsupported Action schema dependency' using errcode='42501';end if;
  select definition into payload from ontology.object_type_versions where tenant_id=r->>'tenant_id' and type_name=r->>'stable_name' and version=(r->>'version')::integer;
  if not found then raise exception 'Action schema dependency missing' using errcode='42501';end if;
  schemas:=schemas||jsonb_build_array(jsonb_build_object('reference',r,'definition',payload));
 end loop;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,p_text::jsonb);
 select * into b from control.nexloop_relation_action_bindings where tenant_id=p_text::jsonb->>'tenant_id'
  and world=p_world and resource_id=p_text::jsonb->>'resource_id';
 if found then
  select definition into v_relation from ontology.relation_type_versions where tenant_id=b.tenant_id and relation_name=b.relation_name and version=b.relation_version;
  if not found then raise exception 'Relation schema dependency missing' using errcode='42501';end if;
  return bundle||jsonb_build_object('schemas',schemas,'relation_binding',to_jsonb(b),'relation_schema',v_relation);
 end if;
 return bundle||jsonb_build_object('schemas',schemas);
end $$;

create or replace function authz.nexloop_role_run_hint(p_run_digest text,p_world text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;run authz.nexloop_run_credentials;b authz.nexloop_role_run_bindings;
begin
 -- S6c second defence (L2 D10 grants review P1): a simulation role never reads here outside its own simulation world.
 if authz.nexloop_app_role(p_world)='nexloop_sim_outside_its_world' then raise exception 'simulation role outside its world' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_run_digest,p_world);
 if ident->'run_context' is null or ident->'run_context'='null'::jsonb then raise exception 'actual Run required' using errcode='42501';end if;
 perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 select * into run from authz.nexloop_run_credentials where token_digest=p_run_digest;
 select * into b from authz.nexloop_role_run_bindings where run_id=run.run_id;
 if not found then return null;end if;
 if b.tenant_id is distinct from ident->'binding'->>'tenant_id' or b.world is distinct from p_world
  or b.source_digest is distinct from run.source_digest then raise exception 'role Run unavailable' using errcode='42501';end if;
 return jsonb_build_object('_source_digest',run.source_digest,'run_id',run.run_id,'consumer_id',b.consumer_id,
  'role_id',b.role_id,'link_id',b.link_id,'step_id',b.step_id);
end $$;
