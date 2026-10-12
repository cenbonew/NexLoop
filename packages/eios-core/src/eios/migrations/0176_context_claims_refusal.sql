-- Context envelopes travel in the signed claims, not the payload. The inspect (0171,
-- ADR-027) and final recheck (0173, ADR-028) branches refused a Context envelope only when it appeared in the payload; the guard
-- puts them in the claims (runtime_activation._signed), so that guard against a Context-carrying inspect or final recheck never
-- fired. Both branches now refuse an envelope in either place. Everything else is unchanged.
create or replace function authz.nexloop_runtime_activation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare p jsonb;c jsonb;r jsonb;run uuid;
begin
 begin p:=p_payload::jsonb;exception when others then p:=null;end;
 begin c:=p_text::jsonb;exception when others then c:=null;end;
 if p is not null and jsonb_typeof(p)='object' and p->>'verb' in ('resolve','authorize') and p->>'operation'='inspect' then
  -- Inspect carries only the core parameters: a Context envelope here is refused rather than ignored.
  if (jsonb_typeof(c)='object' and c ?| array['context_artifact_proof','context_catalog_envelope','context_role_envelope','context_relationship_envelopes','context_formal_reads','formal_reads','context_policy_envelope']) or p ?| array['context_artifact_proof','context_catalog_envelope','context_role_envelope','context_relationship_envelopes','context_formal_reads','request_snapshot','model_result','final_recheck'] then
   raise exception 'inspect authorization carries Context' using errcode='42501';end if;
  r:=authz.nexloop_runtime_activation_command_v0051(p_digest,p_world,p_text,p_signature,p_payload);
  if r ? 'context_artifact' then raise exception 'inspect authorization returned Context' using errcode='42501';end if;
  return r;
 end if;
 if p is not null and jsonb_typeof(p)='object' and p ? 'final_recheck' then
  if p->'final_recheck' is distinct from 'true'::jsonb or p->>'verb' is distinct from 'authorize' or p->>'operation' is distinct from 'tool'
   or (jsonb_typeof(c)='object' and c ?| array['context_artifact_proof','context_catalog_envelope','context_role_envelope','context_relationship_envelopes','context_formal_reads','formal_reads','context_policy_envelope'])
   or p ?| array['context_artifact_proof','context_catalog_envelope','context_role_envelope','context_relationship_envelopes','context_formal_reads','request_snapshot','model_result'] then
   raise exception 'effect final recheck invalid' using errcode='42501';end if;
  begin run:=((p->>'command_text')::jsonb->>'run_id')::uuid;exception when others then run:=null;end;
  if run is null or not exists(select 1 from authz.nexloop_runtime_execution_markers m where m.run_id=run) then
   raise exception 'effect final recheck without authorized execution' using errcode='42501';end if;
  r:=authz.nexloop_runtime_activation_command_v0051(p_digest,p_world,p_text,p_signature,p_payload);
  if exists(select 1 from runtime.nexloop_context_artifact_bindings b where b.run_id=run and not exists(
    select 1 from runtime.nexloop_local_artifacts a where a.tenant_id=b.tenant_id and a.world=b.world and a.artifact_id=b.artifact_id
     and a.status='available' and a.sha256=b.pack_digest and a.retention_until>clock_timestamp())) then
   raise exception 'context artifact unavailable' using errcode='42501';end if;
  if r ? 'context_artifact' or r->'ever_execution_authorized' is distinct from 'true'::jsonb then
   raise exception 'effect final recheck refused' using errcode='42501';end if;
  return r;
 end if;
 return authz.nexloop_runtime_activation_command_before_inspect_scope_v0360(p_digest,p_world,p_text,p_signature,p_payload);
end $$;
