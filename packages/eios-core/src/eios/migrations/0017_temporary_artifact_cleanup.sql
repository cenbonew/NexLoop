-- Maintenance-only authorization guard. Final Artifact names are never candidates.
create function authz.nexloop_lock_temporary_artifact_cleanup(p_digest text,p_world text,p_permit text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare input jsonb:=p_payload::jsonb;binding jsonb;permit authz.nexloop_artifact_permits%rowtype;claims jsonb;
begin
 if jsonb_typeof(input->'limit') is distinct from 'number' or (input->>'limit')::integer not between 1 and 100
  or jsonb_typeof(input->'after') is distinct from 'string' or (input->>'after'<>'' and input->>'after' !~ '^\.tmp-[a-f0-9]{32}$')
  or jsonb_typeof(input->'older_than') is distinct from 'string' or (input->>'older_than')::timestamptz>clock_timestamp()-interval '60 seconds' then
  raise exception 'invalid temporary cleanup page' using errcode='22023';end if;
 binding:=authz.nexloop_service_identity(p_digest,p_world)->'binding';
 perform set_config('eios.tenant_id',binding->>'tenant_id',true);
 select * into permit from authz.nexloop_artifact_permits where tenant_id=binding->>'tenant_id' and permit_id=p_permit for update;
 if not found then raise exception 'cleanup permit unavailable' using errcode='42501';end if;
 if permit.used_count=0 then
  claims:=authz.nexloop_consume_artifact_permit(p_digest,p_world,p_permit,'delete',p_payload);
 else
  claims:=permit.claims;
  if permit.used_count<>1 or claims->>'operation' is distinct from 'delete'
   or claims->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
   raise exception 'cleanup permit binding rejected' using errcode='42501';end if;
  perform authz.nexloop_assert_artifact_authority(p_digest,p_world,claims);
 end if;
 return jsonb_build_object('tenant_id',claims->>'tenant_id','world',p_world);
end $$;
alter function authz.nexloop_lock_temporary_artifact_cleanup(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_lock_temporary_artifact_cleanup(text,text,text,text) from public;
grant execute on function authz.nexloop_lock_temporary_artifact_cleanup(text,text,text,text) to nexloop_domain_worker;
