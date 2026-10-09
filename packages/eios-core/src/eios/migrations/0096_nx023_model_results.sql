-- NX-023-D: model call outcome (status, token usage, cost, response digest) on the 0092/0093
-- guard, written once per recorded request in the same transaction as a `model` authorization
-- of the same v6 Run. Requests are unchanged; v2-v5 Runs carry neither field.
create or replace function authz.nexloop_runtime_activation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;result jsonb;s jsonb;req jsonb;command jsonb;b record;
 pack runtime.nexloop_context_packs;strat jsonb;existing runtime.nexloop_model_requests;prompt text;outcome jsonb;
begin
 if p ? 'request_snapshot' and (p->>'verb' is distinct from 'authorize' or p->>'operation' is distinct from 'model') then
  raise exception 'model request snapshot unexpected' using errcode='42501';end if;
 if p ? 'model_result' and (p->>'verb' is distinct from 'authorize' or p->>'operation' is distinct from 'model' or p ? 'request_snapshot') then
  raise exception 'model result unexpected' using errcode='42501';end if;
 result:=authz.nexloop_runtime_activation_command_before_model_requests_v0090(p_digest,p_world,p_text,p_signature,p_payload);
 if not p ? 'request_snapshot' and not p ? 'model_result' then return result;end if;
 command:=(p->>'command_text')::jsonb;perform set_config('eios.tenant_id',a->>'tenant_id',true);
 select * into b from runtime.nexloop_context_v6_binding(a->>'tenant_id',p_world,(command->>'run_id')::uuid);
 if not found or b.pack_text::jsonb->>'schema_version' is distinct from 'nexloop.context-pack.v6' then raise exception 'model request snapshot requires a v6 Context' using errcode='42501';end if;
 if p ? 'model_result' then
  -- The outcome of an already recorded request, written once (0089 trigger); a retry with the
  -- same outcome is idempotent, a different outcome for the same call is refused.
  outcome:=p->'model_result';
  if jsonb_typeof(outcome) is distinct from 'object' or (select array_agg(x order by x) from jsonb_object_keys(outcome) x) is distinct from array['call_sequence','cost','response_digest','result_status','usage']
   or jsonb_typeof(outcome->'call_sequence') is distinct from 'number' or outcome->>'call_sequence'!~'^[1-9][0-9]{0,4}$'
   or outcome->>'result_status' is null or outcome->>'result_status' not in ('succeeded','failed','unknown')
   or (outcome->'usage' is distinct from 'null'::jsonb and (jsonb_typeof(outcome->'usage') is distinct from 'object'
     or (select array_agg(x order by x) from jsonb_object_keys(outcome->'usage') x) is distinct from array['cache_read','cache_write','input','output','total']
     or exists(select 1 from jsonb_each(outcome->'usage') u where jsonb_typeof(u.value) is distinct from 'number' or u.value#>>'{}'!~'^[0-9]{1,12}$')))
   or (outcome->'cost' is distinct from 'null'::jsonb and (jsonb_typeof(outcome->'cost') is distinct from 'string' or outcome->>'cost'!~'^[0-9]{1,10}(\.[0-9]{1,8})?$'))
   or (outcome->'response_digest' is distinct from 'null'::jsonb and coalesce(outcome->>'response_digest','')!~'^[0-9a-f]{64}$')
   or (outcome->>'result_status'='succeeded' and (outcome->'usage'='null'::jsonb or outcome->'cost'='null'::jsonb or outcome->'response_digest'='null'::jsonb)) then
   raise exception 'model result invalid' using errcode='42501';end if;
  select * into existing from runtime.nexloop_model_requests where tenant_id=b.tenant_id and world=b.world and run_id=b.run_id and call_sequence=(outcome->>'call_sequence')::integer for update;
  if not found then raise exception 'model result without request' using errcode='42501';end if;
  if existing.result_status is not null then
   if (existing.result_status,existing.usage,existing.cost,existing.response_digest)
    is distinct from (outcome->>'result_status',nullif(outcome->'usage','null'::jsonb),(outcome->>'cost')::numeric,outcome->>'response_digest') then
    raise exception 'model result already recorded' using errcode='42501';end if;
   return result;
  end if;
  update runtime.nexloop_model_requests set result_status=outcome->>'result_status',usage=nullif(outcome->'usage','null'::jsonb),cost=(outcome->>'cost')::numeric,
   response_digest=outcome->>'response_digest',completed_at=clock_timestamp()
   where tenant_id=b.tenant_id and world=b.world and run_id=b.run_id and call_sequence=(outcome->>'call_sequence')::integer;
  return result;
 end if;
 select * into pack from runtime.nexloop_context_packs where tenant_id=b.tenant_id and world=b.world and run_id=b.run_id and context_id=b.context_id;
 select definition into strat from control.nexloop_context_strategies where tenant_id=b.tenant_id and world=b.world
  and strategy_id=split_part(substr(pack.strategy_ref,18),'@',1) and version=split_part(pack.strategy_ref,'@',2)::integer;
 s:=p->'request_snapshot';
 if not found or jsonb_typeof(s) is distinct from 'object' or (select count(*) from jsonb_object_keys(s))<>7
  or not s ?& array['call_sequence','model_provider','model_id','request_text','request_digest','tool_manifest_digest','settings_digest']
  or jsonb_typeof(s->'call_sequence') is distinct from 'number' or s->>'call_sequence'!~'^[1-9][0-9]{0,4}$'
  or jsonb_typeof(s->'request_text') is distinct from 'string' or octet_length(s->>'request_text')>262144 then raise exception 'model request snapshot invalid' using errcode='42501';end if;
 prompt:=s->>'request_text';
 -- The digest is over the exact request bytes; tool/settings digests are recomputed from them.
 if s->>'request_digest' is distinct from encode(sha256(convert_to(prompt,'UTF8')),'hex') then raise exception 'model request digest mismatch' using errcode='42501';end if;
 req:=prompt::jsonb;
 if (select count(*) from jsonb_object_keys(req))<>3 or not req ?& array['model','context','settings']
  or req->'model'->>'provider' is distinct from s->>'model_provider' or req->'model'->>'id' is distinct from s->>'model_id'
  or s->>'tool_manifest_digest' is distinct from runtime.nexloop_context_hash(runtime.nexloop_tool_manifest(req->'context'))
  or s->>'settings_digest' is distinct from runtime.nexloop_context_hash(req->'settings') then raise exception 'model request snapshot mismatch' using errcode='42501';end if;
 -- Profile allowlist: the Run command's runtime profile fixes the provider.
 if (case command->>'runtime_profile' when 'deterministic-test' then 'faux' when 'deepseek-flash' then 'deepseek' end) is distinct from s->>'model_provider' then
  raise exception 'model provider not allowed for profile' using errcode='42501';end if;
 -- The request carries the bound pack as the Run input (the Context it claims to use).
 if not exists(select 1 from jsonb_array_elements(req->'context'->'messages') m where m->>'role'='user'
   and (m->>'content'=b.pack_text or exists(select 1 from jsonb_array_elements(case when jsonb_typeof(m->'content')='array' then m->'content' else '[]'::jsonb end) c where c->>'type'='text' and c->>'text'=b.pack_text))) then
  raise exception 'model request does not carry the bound Context' using errcode='42501';end if;
 select * into existing from runtime.nexloop_model_requests where tenant_id=b.tenant_id and world=b.world and run_id=b.run_id and call_sequence=(s->>'call_sequence')::integer;
 if found then
  -- Guard retry of the same call is idempotent; a different request under the same number is not.
  if (existing.request_digest,existing.model_provider,existing.model_id,existing.tool_manifest_digest,existing.settings_digest)
   is distinct from (s->>'request_digest',s->>'model_provider',s->>'model_id',s->>'tool_manifest_digest',s->>'settings_digest') then
   raise exception 'model request call_sequence conflict' using errcode='42501';end if;
  return result;
 end if;
 insert into runtime.nexloop_model_requests(tenant_id,world,run_id,call_sequence,context_id,model_provider,model_id,tool_manifest_digest,request_digest,
  prompt_artifact_id,input_token_budget,output_token_budget,settings_digest)
  values(b.tenant_id,b.world,b.run_id,(s->>'call_sequence')::integer,b.context_id,s->>'model_provider',s->>'model_id',s->>'tool_manifest_digest',s->>'request_digest',
  substr(encode(sha256(convert_to('prompt:'||b.run_id::text||':'||(s->>'call_sequence'),'UTF8')),'hex'),1,32),(strat->>'input_token_budget')::integer,(strat->>'output_reserve')::integer,s->>'settings_digest');
 insert into runtime.nexloop_model_request_prompts values(b.tenant_id,b.world,b.run_id,(s->>'call_sequence')::integer,
  substr(encode(sha256(convert_to('prompt:'||b.run_id::text||':'||(s->>'call_sequence'),'UTF8')),'hex'),1,32),prompt);
 return result;
end $$;
