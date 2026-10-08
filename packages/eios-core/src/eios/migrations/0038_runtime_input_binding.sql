-- Preserve 0037 bytes and its complete authority chain while enforcing prompt
-- consistency on legacy standalone enrollment of input-bearing queue tasks.
alter function authz.nexloop_runtime_activation_command(text,text,text,text,text)
 rename to nexloop_runtime_activation_command_v0037;
revoke all on function authz.nexloop_runtime_activation_command_v0037(text,text,text,text,text)
 from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker;

create function authz.nexloop_runtime_activation_command(p_digest text,p_world text,p_claims text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer
set search_path=pg_catalog
set row_security=on as $$
declare v_result jsonb; p jsonb; command jsonb; task_input jsonb;
begin
 -- Inner first verifies role, HMAC, payload, live source/Run/queue authority,
 -- sets the trusted tenant scope and locks/registers the actual task binding.
 v_result:=authz.nexloop_runtime_activation_command_v0037(p_digest,p_world,p_claims,p_signature,p_payload);
 p:=p_payload::jsonb;
 if p->>'verb'='register' then
  command:=(p->>'command_text')::jsonb;
  select normalized_input into task_input from runtime.jobs
   where tenant_id=command->>'tenant_id' and job_id=v_result->>'task_id' for share;
  if not found then raise exception 'activation input binding denied' using errcode='42501';end if;
  if task_input ? 'input' then
   if jsonb_typeof(task_input->'input') is distinct from 'string'
    or encode(sha256(convert_to(task_input->>'input','UTF8')),'hex') is distinct from p->>'input_digest' then
    raise exception 'activation input binding denied' using errcode='42501';end if;
  end if;
  -- Repeat the original register replay checks after the task lock/input check:
  -- every proof expiry, current Run/source identity and command TTL still hold.
  v_result:=authz.nexloop_runtime_activation_command_v0037(p_digest,p_world,p_claims,p_signature,p_payload);
 end if;
 return v_result;
end $$;
alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_runtime_activation_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_runtime_activation_command(text,text,text,text,text)
 to nexloop_api,nexloop_scheduler,nexloop_domain_worker;
