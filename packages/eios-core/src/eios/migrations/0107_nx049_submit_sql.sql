-- NX-049 (temporary number): fewer repeated checks in the activation SQL of one statement.
-- Decisions, returned bindings and proofs are unchanged; every check still runs on the state
-- visible when it runs, and every check that follows a possible wait still runs in full.
--
-- 1. 0039 execution lock: the pre-lock run of the complete 0037/0038 chain only learns the
--    Run id before a possible wait. When the Run's execution lock is obtained without
--    waiting (already held by this transaction, NX-049 lock order, or free), nothing can
--    have changed between that run and the post-lock run, so only the post-lock complete
--    check runs; its verified Run id must be the locked one, else the original lock-and-
--    recheck sequence follows. A contended lock keeps the original pre-check/wait/recheck.
-- 2. Action-authority assertions get the O5b level-1 memo (0096) unchanged in kind: one
--    statement, one visible snapshot, no new advisory lock, no own write to an authority
--    input table (0096 invalidation triggers; the action chain reads the same tables), and
--    every clock condition re-evaluated on a hit (claims expiry, the live identity snapshot
--    and its directory hash). Same per-session owner-only memo table, same kill switch.
-- 3. The O5b read-assertion memo also admits accepted-message-v1 claims (see below).
-- 0001..0106 are unchanged except the v0051 body (same text apart from step 1) and the 0096
-- read-memo wrapper (same text apart from the admitted derivations).

create or replace function authz.nexloop_runtime_activation_command_v0051(p_digest text,p_world text,p_claims text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer
set search_path=pg_catalog,pg_temp
set row_security=on as $$
declare v_result jsonb;p jsonb;command jsonb;binding authz.nexloop_runtime_run_bindings;
 marker authz.nexloop_runtime_execution_markers;ever_authorized boolean;
begin
 p:=p_payload::jsonb;
 command:=(p->>'command_text')::jsonb;
 if p->>'verb' in ('create','authorize') and command->>'run_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  and pg_try_advisory_xact_lock(hashtextextended('nexloop-runtime-execution:'||(command->>'run_id'),0)) then
  -- No wait: the complete original 0037/0038 checks run once, after the lock.
  v_result:=authz.nexloop_runtime_activation_command_v0038(p_digest,p_world,p_claims,p_signature,p_payload);
  if v_result->>'run_id' is distinct from command->>'run_id' then
   perform pg_advisory_xact_lock(hashtextextended('nexloop-runtime-execution:'||(v_result->>'run_id'),0));
   v_result:=authz.nexloop_runtime_activation_command_v0038(p_digest,p_world,p_claims,p_signature,p_payload);
  end if;
 else
  -- Original 0037/0038 checks still verify role/HMAC and the complete live EIOS
  -- source/Run/queue/task/fence/expiry chain, plus immutable command/input.
  v_result:=authz.nexloop_runtime_activation_command_v0038(p_digest,p_world,p_claims,p_signature,p_payload);
  if p->>'verb' not in ('create','authorize') then return v_result;end if;
  -- Serialize marker reads/writes by enrolled Run. Any wait is followed by the
  -- complete original proof/fence/TTL checks before any marker can commit.
  perform pg_advisory_xact_lock(hashtextextended('nexloop-runtime-execution:'||(v_result->>'run_id'),0));
  v_result:=authz.nexloop_runtime_activation_command_v0038(p_digest,p_world,p_claims,p_signature,p_payload);
 end if;
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

alter function authz.nexloop_assert_action_authority(text,text,jsonb) rename to nexloop_assert_action_authority_before_action_memo_v0106;

create function authz.nexloop_assert_action_authority(p_digest text,p_world text,p_claims jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_key text;v_locks bigint;v_hit_locks bigint;v_hit_tenant text;v_hit_binding jsonb;v_identity jsonb;v_binding jsonb;v_ok boolean:=false;
begin
 if p_claims->>'directory_hash' is null or p_claims->>'expires_at' is null or not authz.nexloop_read_memo_ready() then
  return authz.nexloop_assert_action_authority_before_action_memo_v0106(p_digest,p_world,p_claims);
 end if;
 -- Own key space: an action assertion never answers a read assertion (or the reverse).
 v_key:=authz.nexloop_read_memo_key(p_digest,p_world,pg_catalog.jsonb_build_object('nexloop-memo','action-authority','claims',p_claims));
 v_locks:=authz.nexloop_read_memo_advisory_locks();
 select m.advisory_locks,m.tenant,m.binding into v_hit_locks,v_hit_tenant,v_hit_binding from pg_temp.nexloop_read_memo m where m.key=v_key;
 if found and v_hit_locks=v_locks and (p_claims->>'expires_at')::timestamptz>pg_catalog.clock_timestamp() then
  -- Every clock condition of the identity is re-evaluated by the same code; any failure means full check.
  begin
   v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
   v_ok:=v_identity->>'directory_hash' is not distinct from p_claims->>'directory_hash'
    and (p_claims->>'expires_at')::timestamptz>pg_catalog.clock_timestamp();
  exception when others then v_ok:=false;
  end;
  if v_ok then
   perform pg_catalog.set_config('eios.tenant_id',v_hit_tenant,true);  -- same session state as the full check
   return v_hit_binding;
  end if;
 end if;
 v_binding:=authz.nexloop_assert_action_authority_before_action_memo_v0106(p_digest,p_world,p_claims);
 insert into pg_temp.nexloop_read_memo(key,tenant,binding,advisory_locks)
  values(v_key,pg_catalog.current_setting('eios.tenant_id',true),v_binding,authz.nexloop_read_memo_advisory_locks())
  on conflict(key) do update set tenant=excluded.tenant,binding=excluded.binding,advisory_locks=excluded.advisory_locks;
 return v_binding;
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['authz.nexloop_assert_action_authority(text,text,jsonb)','authz.nexloop_assert_action_authority_before_action_memo_v0106(text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;

-- Read-assertion memo (0096 body; admission widened, key and invalidation unchanged).
create or replace function authz.nexloop_assert_read_authority(p_digest text,p_world text,p_claims jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_key text;v_locks bigint;v_hit_locks bigint;v_hit_tenant text;v_hit_binding jsonb;v_identity jsonb;v_binding jsonb;v_ok boolean:=false;
begin
 -- Configured, type-property-v1 and (NX-049) accepted-message-v1 claims bound every nested
 -- deadline by claims.expires_at, which every hit re-checks: the accepted-Message check itself
 -- refuses claims whose expiry exceeds the rule's valid_until or the nested Consumer READ's
 -- expiry. Its inputs (Message object, acceptance, conversation rows, rule and grant facts)
 -- are all covered by the 0096 invalidation triggers. Other derivations (purpose-message-v1
 -- reads runtime.jobs, not covered) always run the full check.
 if (p_claims ? 'derivation' and p_claims->>'derivation' not in ('type-property-v1','accepted-message-v1'))
  or p_claims->>'directory_hash' is null or p_claims->>'expires_at' is null or not authz.nexloop_read_memo_ready() then
  return authz.nexloop_assert_read_authority_before_read_memo_v0095(p_digest,p_world,p_claims);
 end if;
 v_key:=authz.nexloop_read_memo_key(p_digest,p_world,p_claims);
 v_locks:=authz.nexloop_read_memo_advisory_locks();
 -- No row type of the temp table is declared: it may be recreated within the session.
 select m.advisory_locks,m.tenant,m.binding into v_hit_locks,v_hit_tenant,v_hit_binding from pg_temp.nexloop_read_memo m where m.key=v_key;
 if found and v_hit_locks=v_locks and (p_claims->>'expires_at')::timestamptz>pg_catalog.clock_timestamp() then
  -- Every clock condition of the identity (credential, Run, source, browser session and
  -- membership deadlines) is re-evaluated by the same code; any failure means full check.
  begin
   v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
   v_ok:=v_identity->>'directory_hash' is not distinct from p_claims->>'directory_hash'
    and (p_claims->>'expires_at')::timestamptz>pg_catalog.clock_timestamp();
  exception when others then v_ok:=false;
  end;
  if v_ok then
   perform pg_catalog.set_config('eios.tenant_id',v_hit_tenant,true);  -- same session state as the full check
   return v_hit_binding;
  end if;
 end if;
 v_binding:=authz.nexloop_assert_read_authority_before_read_memo_v0095(p_digest,p_world,p_claims);
 -- Stored under the key computed before the full check: a later hit needs the visible
 -- snapshot to be unchanged since before this check began (including any wait inside it).
 insert into pg_temp.nexloop_read_memo(key,tenant,binding,advisory_locks)
  values(v_key,pg_catalog.current_setting('eios.tenant_id',true),v_binding,authz.nexloop_read_memo_advisory_locks())
  on conflict(key) do update set tenant=excluded.tenant,binding=excluded.binding,advisory_locks=excluded.advisory_locks;
 return v_binding;
end $$;
