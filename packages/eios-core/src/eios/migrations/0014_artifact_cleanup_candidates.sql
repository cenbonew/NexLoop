-- Authenticated metadata discovery only; every deletion still needs its own permit.
create index nexloop_artifact_cleanup_cursor on runtime.nexloop_local_artifacts
 (tenant_id,world,principal_id,artifact_id)
 where status in ('pending','available','deleting');
create function authz.nexloop_artifact_cleanup_candidates(p_digest text,p_world text,p_permit text,p_payload text)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_input jsonb:=p_payload::jsonb;v_claims jsonb;v_limit integer;v_after text;v_ids jsonb;
begin
 v_limit:=(v_input->>'limit')::integer;v_after:=v_input->>'after';
 if jsonb_typeof(v_input->'limit') is distinct from 'number' or v_limit is null
  or v_limit not between 1 and 100 or jsonb_typeof(v_input->'after') is distinct from 'string'
  or (v_after<>'' and v_after !~ '^[a-f0-9]{32}$') then
  raise exception 'invalid artifact cleanup cursor' using errcode='22023';end if;
 v_claims:=authz.nexloop_consume_artifact_permit(p_digest,p_world,p_permit,'read',p_payload);
 select coalesce(jsonb_agg(candidate.artifact_id order by candidate.artifact_id collate "C"),'[]'::jsonb)
 into v_ids from (
  select a.artifact_id from runtime.nexloop_local_artifacts a
  where a.tenant_id=v_claims->>'tenant_id' and a.world=p_world
   and a.principal_id=v_claims->>'principal_id'
   and a.artifact_id collate "C">v_after collate "C"
   and a.retention_until<=clock_timestamp()
   and (a.status='available' or (a.status in ('pending','deleting') and a.lease_until<=clock_timestamp()))
   and not exists(select 1 from runtime.invocations i where i.tenant_id=a.tenant_id and i.artifact_refs::text like '%'||a.artifact_id||'%')
   and not exists(select 1 from runtime.jobs j where j.tenant_id=a.tenant_id and to_jsonb(j)::text like '%'||a.artifact_id||'%')
  order by a.artifact_id collate "C" limit v_limit
 ) candidate;
 return v_ids;
end $$;
alter function authz.nexloop_artifact_cleanup_candidates(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_artifact_cleanup_candidates(text,text,text,text) from public;
grant execute on function authz.nexloop_artifact_cleanup_candidates(text,text,text,text)
 to nexloop_api,nexloop_domain_worker;
