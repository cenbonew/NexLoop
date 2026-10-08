-- Preserve upstream ontology schema version rows; pin Action bundle to exact rows.
create function authz.nexloop_schema_version_immutable() returns trigger language plpgsql set search_path=pg_catalog as $$
begin
 if to_jsonb(new) is distinct from to_jsonb(old) then
  raise exception 'published schema version is immutable' using errcode='22000';end if;
 return new;
end $$;
alter function authz.nexloop_schema_version_immutable() owner to nexloop_owner;
revoke all on function authz.nexloop_schema_version_immutable() from public;
create trigger object_schema_version_immutable before update on ontology.object_type_versions
 for each row execute function authz.nexloop_schema_version_immutable();
create trigger object_schema_authority_revision after insert or update or delete on ontology.object_type_versions
 for each row execute function authz.nexloop_authority_epoch_guard();

create function authz.nexloop_read_action_bundle(p_digest text,p_world text,p_text text,p_signature text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare bundle jsonb;r jsonb;payload jsonb;schemas jsonb:='[]'::jsonb;
begin
 bundle:=authz.nexloop_read_action_definition(p_digest,p_world,p_text,p_signature);
 for r in select distinct value from jsonb_array_elements((bundle->'definition'->'object_types')||coalesce(bundle->'definition'->'governance'->'change_scope'->'object_types','[]'::jsonb)) loop
  if r->>'tenant_id' is distinct from p_text::jsonb->>'tenant_id' or r->>'schema_type' is distinct from 'object_type' then
   raise exception 'unsupported Action schema dependency' using errcode='42501';end if;
  select definition into payload from ontology.object_type_versions where tenant_id=r->>'tenant_id' and type_name=r->>'stable_name' and version=(r->>'version')::integer;
  if not found then raise exception 'Action schema dependency missing' using errcode='42501';end if;
  schemas:=schemas||jsonb_build_array(jsonb_build_object('reference',r,'definition',payload));
 end loop;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,p_text::jsonb);
 return bundle||jsonb_build_object('schemas',schemas);
end $$;
alter function authz.nexloop_read_action_bundle(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_action_bundle(text,text,text,text) from public;
grant execute on function authz.nexloop_read_action_bundle(text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;
