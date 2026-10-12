-- Effect tool final recheck (temporary number 0390; ADR-028, accepted by the owner 2026-10-12). The guard's effect tool path
-- (runtime_activation._effect_tool) authorizes the Run fully before it writes or reads the effect intent (guard_before, the
-- whole chain with every Context read proof) and rechecks after it, in the same transaction, that the Run is still valid
-- after a possible lock wait. That final recheck (payload final_recheck=true on a tool authorization) is now answered by the
-- activation core, as an inspect is (ADR-027, 0171): the dispatcher's credential and queue authority, the Run credential and
-- its Run proofs (revocation, expiry, voiding, source authority change), the task lease and fencing, the Run binding and
-- not_after. It also keeps one direct Context check at this point: the Run's bound Context artifact (0052) must still be
-- available, unaltered and within its retention, so a Context that expires while the intent is written still rolls the
-- whole transaction back (without rebuilding every Context read proof). It is accepted only for a Run whose execution was
-- already authorized (its marker exists before this call), it refuses any Context envelope or model data, and its answer
-- carries no Context. Every other authorization is unchanged.
create or replace function authz.nexloop_runtime_activation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare p jsonb;r jsonb;run uuid;
begin
 begin p:=p_payload::jsonb;exception when others then p:=null;end;
 if p is not null and jsonb_typeof(p)='object' and p->>'verb' in ('resolve','authorize') and p->>'operation'='inspect' then
  -- Inspect carries only the core parameters: a Context envelope here is refused rather than ignored.
  if p ?| array['context_artifact_proof','context_catalog_envelope','context_role_envelope','context_relationship_envelopes','context_formal_reads','request_snapshot','model_result','final_recheck'] then
   raise exception 'inspect authorization carries Context' using errcode='42501';end if;
  r:=authz.nexloop_runtime_activation_command_v0051(p_digest,p_world,p_text,p_signature,p_payload);
  if r ? 'context_artifact' then raise exception 'inspect authorization returned Context' using errcode='42501';end if;
  return r;
 end if;
 if p is not null and jsonb_typeof(p)='object' and p ? 'final_recheck' then
  if p->'final_recheck' is distinct from 'true'::jsonb or p->>'verb' is distinct from 'authorize' or p->>'operation' is distinct from 'tool'
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
