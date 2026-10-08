-- A monotonic technical Run/enrollment marker survives Worker lease changes.
-- It records permission to execute, not provider success or business facts.
create table authz.nexloop_runtime_execution_markers (
 run_id uuid primary key references authz.nexloop_runtime_run_bindings(run_id),
 tenant_id text not null,world text not null,
 command_digest text not null check(command_digest ~ '^[a-f0-9]{64}$'),
 first_authorized_at timestamptz not null
);
alter table authz.nexloop_runtime_execution_markers owner to nexloop_owner;
revoke all on authz.nexloop_runtime_execution_markers
 from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker;

alter function authz.nexloop_runtime_activation_command(text,text,text,text,text)
 rename to nexloop_runtime_activation_command_v0038;
revoke all on function authz.nexloop_runtime_activation_command_v0038(text,text,text,text,text)
 from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker;

create function authz.nexloop_runtime_activation_command(p_digest text,p_world text,p_claims text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer
set search_path=pg_catalog
set row_security=on as $$
declare v_result jsonb;p jsonb;command jsonb;binding authz.nexloop_runtime_run_bindings;
 marker authz.nexloop_runtime_execution_markers;ever_authorized boolean;
begin
 -- Original 0037/0038 checks still verify role/HMAC and the complete live EIOS
 -- source/Run/queue/task/fence/expiry chain, plus immutable command/input.
 v_result:=authz.nexloop_runtime_activation_command_v0038(p_digest,p_world,p_claims,p_signature,p_payload);
 p:=p_payload::jsonb;
 if p->>'verb' not in ('create','authorize') then return v_result;end if;
 command:=(p->>'command_text')::jsonb;
 -- Serialize marker reads/writes by enrolled Run. Any wait is followed by the
 -- complete original proof/fence/TTL checks before any marker can commit.
 perform pg_advisory_xact_lock(hashtextextended('nexloop-runtime-execution:'||(v_result->>'run_id'),0));
 v_result:=authz.nexloop_runtime_activation_command_v0038(p_digest,p_world,p_claims,p_signature,p_payload);
 select * into binding from authz.nexloop_runtime_run_bindings
  where run_id=(v_result->>'run_id')::uuid for share;
 if not found or binding.tenant_id is distinct from command->>'tenant_id'
  or binding.world is distinct from p_world or binding.command_digest is distinct from p->>'command_digest' then
  raise exception 'activation execution binding denied' using errcode='42501';end if;
 if p->>'verb'='authorize' and p->>'operation' in ('model','tool') then
  insert into authz.nexloop_runtime_execution_markers
   values(binding.run_id,binding.tenant_id,binding.world,binding.command_digest,clock_timestamp()) on conflict do nothing;
 end if;
 select * into marker from authz.nexloop_runtime_execution_markers where run_id=binding.run_id for share;
 ever_authorized:=found;
 if ever_authorized and (marker.tenant_id is distinct from binding.tenant_id or marker.world is distinct from binding.world
  or marker.command_digest is distinct from binding.command_digest) then
  raise exception 'activation execution marker denied' using errcode='42501';end if;
 -- The marker insert and all repeated authority checks share the same caller
 -- transaction. A failed final check rolls back the marker; the API commits
 -- before the Host receives its permission to actually dispatch model/tool.
 v_result:=authz.nexloop_runtime_activation_command_v0038(p_digest,p_world,p_claims,p_signature,p_payload);
 return v_result||jsonb_build_object('ever_execution_authorized',ever_authorized);
end $$;
alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_runtime_activation_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_runtime_activation_command(text,text,text,text,text)
 to nexloop_api,nexloop_scheduler,nexloop_domain_worker;
