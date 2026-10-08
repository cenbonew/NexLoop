-- Append-only Runtime declaration binding for governed effect admission.
-- Runtime declarations are checked against the governed Run context. Existing
-- non-runtime callers retain the original API when runtime_refs is absent.
alter function authz.nexloop_effect_intent_command(text,text,text,text,text)
 rename to nexloop_effect_intent_command_v0041;
revoke all on function authz.nexloop_effect_intent_command_v0041(text,text,text,text,text)
 from public,nexloop_api,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker;
create function authz.nexloop_effect_intent_command(p_digest text,p_world text,p_claims text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c jsonb:=p_claims::jsonb;p jsonb:=p_payload::jsonb;refs jsonb;k bytea;
 outcome jsonb;r authz.nexloop_run_credentials;ctx control.nexloop_effect_contexts;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker')
  or p_claims is null or p_payload is null or octet_length(p_claims)>1048576 or octet_length(p_payload)>131072
  or jsonb_typeof(c) is distinct from 'object' or jsonb_typeof(p) is distinct from 'object'
 then raise exception 'runtime effect context unavailable' using errcode='42501';end if;
 select sk.key_material into k from authz.nexloop_authority_signing_keys sk where sk.key_id=c->>'key_id' and sk.active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-intent-v1:'||p_claims,'UTF8'),k,'sha256'),'hex')
  or c->>'protocol' is distinct from 'nexloop-effect-intent-v1'
  or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp()
  or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or c->>'resource_id' is distinct from 'eios:action:nexloop.service.request:'||(p->>'action_version')
 then raise exception 'runtime effect context unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if p ? 'runtime_refs' then
  refs:=p->'runtime_refs';
  if jsonb_typeof(refs) is distinct from 'object' then
   raise exception 'runtime effect context unavailable' using errcode='42501';end if;
  if (select count(*) from jsonb_object_keys(refs))<>2
   or jsonb_typeof(refs->'consumer_ref') is distinct from 'string'
   or jsonb_typeof(refs->'goal_version_ref') is distinct from 'string'
   or length(refs->>'consumer_ref')>128 or length(refs->>'goal_version_ref')>256
  then raise exception 'runtime effect context unavailable' using errcode='42501';end if;
 end if;
 -- The original function acquires ledger -> context -> object locks. Do not
 -- introduce a context lock ahead of the ledger. All writes remain uncommitted;
 -- a declaration mismatch rolls back intent, outbox and quota together.
 outcome:=authz.nexloop_effect_intent_command_v0041(p_digest,p_world,p_claims,p_signature,p_payload);
 if p ? 'runtime_refs' then
  select * into r from authz.nexloop_run_credentials rc where rc.token_digest=p_digest for share;
  if not found then raise exception 'runtime effect context unavailable' using errcode='42501';end if;
  select ec.* into ctx from control.nexloop_effect_run_contexts rl
   join control.nexloop_effect_contexts ec on ec.context_id=rl.context_id
   where rl.run_id=r.run_id and rl.valid_until>clock_timestamp()
    and ec.tenant_id=c->>'tenant_id' and ec.world=p_world and ec.valid_until>clock_timestamp()
   for share of rl,ec;
  if not found or refs->>'consumer_ref' is distinct from 'consumer:'||ctx.consumer_id
   or refs->>'goal_version_ref' is distinct from ctx.goal_version_ref
  then raise exception 'runtime effect context unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_effect_plan(ctx.context_id,c->>'tenant_id',p_world);
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if (c->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'runtime effect context unavailable' using errcode='42501';end if;
 return outcome;
end $$;
alter function authz.nexloop_effect_intent_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_intent_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_effect_intent_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;
