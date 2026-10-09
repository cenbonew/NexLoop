-- NX-018 step 3 phase B: dispatch-time Role policy re-check. Every Role-bearing
-- activation (except resolve/create), effect submit and effect admit/finalize must carry
-- the current signed Source policy recipe; policy EDIT (revision), deactivation, expiry or
-- Source READ revocation denies the bound Run's next call. effect_units bounds distinct
-- submissions per Run, serialized by the policy row. Runs without a Role are unaffected.

create function authz.nexloop_role_policy_claims_current(p_digest text,p_world text,c jsonb,p jsonb,p_budget boolean default false,p_effect boolean default false) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare role_env jsonb:=coalesce(c->'context_role_envelope',c->'role_envelope');env jsonb:=c->'context_policy_envelope';ident jsonb;run authz.nexloop_run_credentials;b authz.nexloop_role_policy_bindings;value jsonb;command jsonb;counted bigint;
begin
 if role_env is null or role_env='null'::jsonb then return null;end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 select * into run from authz.nexloop_run_credentials where run_id=((role_env->>'payload')::jsonb->>'run_id')::uuid;
 if not found or jsonb_typeof(env) is distinct from 'object' or (select count(*) from jsonb_object_keys(env))<>3 or not(env ?& array['text','signature','payload'])
  or (env->>'payload')::jsonb->>'run_id' is distinct from run.run_id::text then raise exception 'current Role policy mandatory' using errcode='42501';end if;
 -- Same policy row serializes new submissions; no count-before-insert race.
 if p_effect then select * into b from authz.nexloop_role_policy_bindings where run_id=run.run_id for update;
 else select * into b from authz.nexloop_role_policy_bindings where run_id=run.run_id for share;end if;
 if not found or b.tenant_id is distinct from ident->'binding'->>'tenant_id' or b.world is distinct from p_world then raise exception 'Role policy binding mandatory' using errcode='42501';end if;
 value:=authz.nexloop_role_policy_current(run.source_digest,p_world,env->>'text',env->>'signature',env->>'payload');
 if p_budget then
  command:=(p->>'command_text')::jsonb;
  if command->'budget' is distinct from b.budget then raise exception 'bound Role Run budget mismatch' using errcode='42501';end if;
 end if;
 if p_effect then
  select count(distinct intent_id) into counted from runtime.nexloop_effect_submissions where run_id=run.run_id;
  if counted>b.effect_units then raise exception 'Role effect unit ceiling exceeded' using errcode='42501';end if;
 end if;
 perform authz.nexloop_role_ttl_reads((role_env->>'payload')::jsonb->'reads');
 perform authz.nexloop_role_ttl_reads((env->>'payload')::jsonb->'reads');
 if (value->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Role policy tail expired' using errcode='42501';end if;
 return value;
end $$;
alter function authz.nexloop_role_policy_claims_current(text,text,jsonb,jsonb,boolean,boolean) owner to nexloop_owner;
revoke all on function authz.nexloop_role_policy_claims_current(text,text,jsonb,jsonb,boolean,boolean) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

-- After the inner command only time and the new submission can have changed: the full
-- validation above holds FOR SHARE on the policy/Role/Link/Step rows and on every READ
-- authority fact for this transaction, so a concurrent EDIT or revocation cannot commit
-- until it ends. Re-check the deadline and, for submit, the effect_units count.
create function authz.nexloop_role_policy_claims_tail(p_digest text,p_world text,c jsonb,before jsonb,p_effect boolean) returns void
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare b authz.nexloop_role_policy_bindings;counted bigint;
begin
 if before is null then return;end if;
 select * into b from authz.nexloop_role_policy_bindings where run_id=(before->>'run_id')::uuid for share;
 if not found or b.expires_at<=clock_timestamp() or (before->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Role policy tail expired' using errcode='42501';end if;
 if p_effect then
  select count(distinct intent_id) into counted from runtime.nexloop_effect_submissions where run_id=b.run_id;
  if counted>b.effect_units then raise exception 'Role effect unit ceiling exceeded' using errcode='42501';end if;
 end if;
end $$;
alter function authz.nexloop_role_policy_claims_tail(text,text,jsonb,jsonb,boolean) owner to nexloop_owner;
revoke all on function authz.nexloop_role_policy_claims_tail(text,text,jsonb,jsonb,boolean) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) rename to nexloop_runtime_activation_command_before_role_policy_v0081;
revoke all on function authz.nexloop_runtime_activation_command_before_role_policy_v0081(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_runtime_activation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;policy jsonb;
begin
 if p->>'verb' not in ('resolve','create') then policy:=authz.nexloop_role_policy_claims_current(p_digest,p_world,c,p,p->>'verb'='register',false);end if;
 result:=authz.nexloop_runtime_activation_command_before_role_policy_v0081(p_digest,p_world,p_text,p_signature,p_payload);
 if p->>'verb' not in ('resolve','create') then perform authz.nexloop_role_policy_claims_tail(p_digest,p_world,c,policy,false);end if;
 return result;
end $$;
alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_runtime_activation_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_runtime_activation_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_scheduler;

alter function authz.nexloop_effect_intent_command(text,text,text,text,text) rename to nexloop_effect_intent_command_before_role_policy_v0081;
revoke all on function authz.nexloop_effect_intent_command_before_role_policy_v0081(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_effect_intent_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;policy jsonb;
begin
 if true then policy:=authz.nexloop_role_policy_claims_current(p_digest,p_world,c,p,false,true);end if;
 result:=authz.nexloop_effect_intent_command_before_role_policy_v0081(p_digest,p_world,p_text,p_signature,p_payload);
 if true then perform authz.nexloop_role_policy_claims_tail(p_digest,p_world,c,policy,true);end if;
 return result;
end $$;
alter function authz.nexloop_effect_intent_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_intent_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_effect_intent_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

alter function authz.nexloop_effect_execution_command(text,text,text,text,text) rename to nexloop_effect_execution_command_before_role_policy_v0081;
revoke all on function authz.nexloop_effect_execution_command_before_role_policy_v0081(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_effect_execution_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;policy jsonb;
begin
 if p->>'verb' in ('admit','finalize') then policy:=authz.nexloop_role_policy_claims_current(p_digest,p_world,c,p,false,false);end if;
 result:=authz.nexloop_effect_execution_command_before_role_policy_v0081(p_digest,p_world,p_text,p_signature,p_payload);
 if p->>'verb' in ('admit','finalize') then perform authz.nexloop_role_policy_claims_tail(p_digest,p_world,c,policy,false);end if;
 return result;
end $$;
alter function authz.nexloop_effect_execution_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_execution_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_effect_execution_command(text,text,text,text,text) to nexloop_action_worker;
