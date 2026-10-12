-- Inspect authorization scope (temporary number 0360; draft, ADR-027 proposed, not accepted). An inspect only reads the
-- Run's own state on the Host and returns its outcome and receipt IDs; it causes no effect and carries no Context. Its
-- guard resolve and authorization are answered by the activation core (0037-0039, nexloop_runtime_activation_command_v0051), which
-- still rechecks at every call: the dispatcher's service credential and its queue action authority, the Run credential's
-- identity and its Run proofs (revoked, expired or voided credentials, tenant authority), the task lease and fencing,
-- the Run binding (command and input digests), and not_after. The layers added since 0052 (Context artifact, catalog,
-- role, role deadline, formal current, relationships, role policy, v6 binding, model requests, reply fallback) re-verify
-- the Run's Context read proofs; they keep applying unchanged to start, resume, cancel, model and tool, every effect and
-- the resolve of every other operation. An inspect answer never carries a Context artifact (the core returns none; the guard asserts it).
alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) rename to nexloop_runtime_activation_command_before_inspect_scope_v0360;
revoke all on function authz.nexloop_runtime_activation_command_before_inspect_scope_v0360(text,text,text,text,text) from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker;

create function authz.nexloop_runtime_activation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare p jsonb;r jsonb;
begin
 begin p:=p_payload::jsonb;exception when others then p:=null;end;
 if p is not null and jsonb_typeof(p)='object' and p->>'verb' in ('resolve','authorize') and p->>'operation'='inspect' then
  -- Inspect carries only the core parameters: a Context envelope here is refused rather than ignored.
  if p ?| array['context_artifact_proof','context_catalog_envelope','context_role_envelope','context_relationship_envelopes','context_formal_reads','request_snapshot','model_result'] then
   raise exception 'inspect authorization carries Context' using errcode='42501';end if;
  r:=authz.nexloop_runtime_activation_command_v0051(p_digest,p_world,p_text,p_signature,p_payload);
  if r ? 'context_artifact' then raise exception 'inspect authorization returned Context' using errcode='42501';end if;
  return r;
 end if;
 return authz.nexloop_runtime_activation_command_before_inspect_scope_v0360(p_digest,p_world,p_text,p_signature,p_payload);
end $$;
alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_runtime_activation_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_runtime_activation_command(text,text,text,text,text) to nexloop_api,nexloop_scheduler,nexloop_domain_worker;
