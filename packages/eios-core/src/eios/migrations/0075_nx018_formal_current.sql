-- NX-018: current formal Source READ re-checked at model/start, submit, admit and finalize.
-- Wraps the 0065 public command chain via private *_before_formal_current_v0065 aliases.
create function authz.nexloop_assert_context_proof_deadlines(c jsonb) returns void
language plpgsql security definer set search_path=pg_catalog as $$declare p jsonb;begin
 if c is null or jsonb_typeof(c) is distinct from 'object' or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context final proof expired' using errcode='42501';end if;
 for p in select value from jsonb_array_elements(coalesce(c->'property_authorities','[]'::jsonb)) loop perform authz.nexloop_assert_context_proof_deadlines(p);end loop;
 if c ? 'relation_authority' and c->'relation_authority' is distinct from 'null'::jsonb then perform authz.nexloop_assert_context_proof_deadlines(c->'relation_authority');end if;
end $$;
alter function authz.nexloop_assert_context_proof_deadlines(jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_context_proof_deadlines(jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

create function authz.nexloop_context_formal_current(d text,w text,facts jsonb,reads jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare n text;e jsonb;c jsonb;expected jsonb;r jsonb;p jsonb;control_at timestamptz;identity jsonb;
begin
 identity:=authz.nexloop_service_identity_snapshot(d,w);
 if w is distinct from 'real' or identity->'run_context' is distinct from 'null'::jsonb or identity->'binding'->>'subject_kind' is distinct from 'service'
  or jsonb_typeof(facts) is distinct from 'array' or jsonb_array_length(facts)<>4 or jsonb_typeof(reads) is distinct from 'object' or (select count(*) from jsonb_object_keys(reads))<>4 or not(reads ?& array['Consumer','Goal','PlanStep','EffectControl']) then raise exception 'context formal current required' using errcode='42501';end if;
 for n,e in select key,value from jsonb_each(reads) loop
  c:=(e->>'text')::jsonb;
  select value into expected from jsonb_array_elements(facts) where value->>'type'=n;
  if not found or (select count(*) from jsonb_array_elements(facts) where value->>'type'=n)<>1 or c->>'type_name' is distinct from n or c->>'object_id' is distinct from expected->>'id'
   or c->'fields' is distinct from (case when n='EffectControl' then '["allow_effect","budget_units","executor_principal","valid_until"]'::jsonb else '[]'::jsonb end) then raise exception 'context formal current mismatch' using errcode='42501';end if;
  r:=authz.nexloop_read_object(d,w,e->>'text',e->>'signature');
  if r is null or r->'revision' is distinct from expected->'revision' then raise exception 'context formal current revision changed' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(d,w,c);
  for p in select value from jsonb_array_elements(c->'property_authorities') loop perform authz.nexloop_assert_read_authority(d,w,p);end loop;
  if n='EffectControl' then control_at:=(r->'properties'->>'valid_until')::timestamptz;end if;
 end loop;
 for e in select value from jsonb_each(reads) loop perform authz.nexloop_assert_context_proof_deadlines((e->>'text')::jsonb);end loop;
 if control_at is null or control_at<=clock_timestamp() then raise exception 'context formal control expired' using errcode='42501';end if;
 return jsonb_build_object('control_valid_until',control_at);
end $$;
alter function authz.nexloop_context_formal_current(text,text,jsonb,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_context_formal_current(text,text,jsonb,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_identity,nexloop_action_worker;

-- Metadata lookup confers no READ: fresh signed authority must still be resolved.
create function authz.nexloop_role_formal_hint(d text,w text,r uuid) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;b authz.nexloop_role_run_bindings;cb runtime.nexloop_role_context_artifacts;
begin
 ident:=authz.nexloop_service_identity_snapshot(d,w);
 perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 select * into b from authz.nexloop_role_run_bindings where run_id=r and tenant_id=ident->'binding'->>'tenant_id' and world=w;
 if not found then raise exception 'role formal binding unavailable' using errcode='42501';end if;
 select * into cb from runtime.nexloop_role_context_artifacts where run_id=r and tenant_id=b.tenant_id and world=b.world;
 if not found or cb.source_digest is distinct from b.source_digest then raise exception 'role formal Context unavailable' using errcode='42501';end if;
 return jsonb_build_object('_source_digest',b.source_digest,'formal_facts',cb.pack_text::jsonb->'formal_facts');
end $$;
alter function authz.nexloop_role_formal_hint(text,text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_role_formal_hint(text,text,uuid) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_role_formal_hint(text,text,uuid) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

create function authz.nexloop_role_formal_current(r uuid,w text,reads jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare b authz.nexloop_role_run_bindings;cb runtime.nexloop_role_context_artifacts;rc control.nexloop_effect_run_contexts;ctx control.nexloop_effect_contexts;pb control.nexloop_effect_plan_bindings;facts jsonb;fact jsonb;expected text;
begin
 select * into b from authz.nexloop_role_run_bindings where run_id=r and world=w for share;
 if not found then raise exception 'role formal binding unavailable' using errcode='42501';end if;
 select * into cb from runtime.nexloop_role_context_artifacts where run_id=r and tenant_id=b.tenant_id and world=w for share;
 if not found or cb.source_digest is distinct from b.source_digest then raise exception 'role formal Context unavailable' using errcode='42501';end if;
 select * into rc from control.nexloop_effect_run_contexts where run_id=r for share;
 if not found or rc.context_id is distinct from cb.context_id then raise exception 'role formal chain unavailable' using errcode='42501';end if;
 select * into ctx from control.nexloop_effect_contexts where context_id=rc.context_id and tenant_id=b.tenant_id and world=w for share;
 if not found or ctx.consumer_id is distinct from b.consumer_id then raise exception 'role formal Consumer chain unavailable' using errcode='42501';end if;
 select * into pb from control.nexloop_effect_plan_bindings where context_id=rc.context_id and tenant_id=b.tenant_id and world=w for share;
 if not found or pb.step_id is distinct from b.step_id then raise exception 'role formal plan unavailable' using errcode='42501';end if;
 facts:=cb.pack_text::jsonb->'formal_facts';
 for fact in select value from jsonb_array_elements(facts) loop
  expected:=case fact->>'type' when 'Consumer' then b.consumer_id when 'Goal' then pb.goal_id when 'PlanStep' then pb.step_id when 'EffectControl' then pb.control_id else null end;
  if expected is null or fact->>'id' is distinct from expected or fact->>'provenance' is distinct from 'eios:object:'||expected then raise exception 'role formal actual reference mismatch' using errcode='42501';end if;
 end loop;
 return authz.nexloop_context_formal_current(b.source_digest,w,facts,reads);
end $$;
alter function authz.nexloop_role_formal_current(uuid,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_role_formal_current(uuid,text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;

alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) rename to nexloop_runtime_activation_command_before_formal_current_v0065;
revoke all on function authz.nexloop_runtime_activation_command_before_formal_current_v0065(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
create function authz.nexloop_runtime_activation_command(d text,w text,t text,s text,body text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c jsonb:=t::jsonb;p jsonb:=body::jsonb;env jsonb:=c->'context_role_envelope';r uuid;before jsonb;after jsonb;result jsonb;ident jsonb;
begin
 if p->>'verb' not in ('resolve','create') and env is not null and env is distinct from 'null'::jsonb then
  ident:=authz.nexloop_service_identity_snapshot(d,w);
  if c->>'tenant_id' is distinct from ident->'binding'->>'tenant_id' then raise exception 'role formal current tenant mismatch' using errcode='42501';end if;
  perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
  r:=((env->>'payload')::jsonb->>'run_id')::uuid;
  before:=authz.nexloop_role_formal_current(r,w,c->'formal_reads');
 end if;
 result:=authz.nexloop_runtime_activation_command_before_formal_current_v0065(d,w,t,s,body);
 if r is not null then
  after:=authz.nexloop_role_formal_current(r,w,c->'formal_reads');
  if before is distinct from after then raise exception 'role formal current changed' using errcode='42501';end if;
  perform authz.nexloop_role_ttl_reads(c->'formal_reads');
 end if;
 return result;
end $$;
alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_runtime_activation_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_runtime_activation_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_scheduler;

alter function authz.nexloop_effect_intent_command(text,text,text,text,text) rename to nexloop_effect_intent_command_before_formal_current_v0065;
revoke all on function authz.nexloop_effect_intent_command_before_formal_current_v0065(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
create function authz.nexloop_effect_intent_command(d text,w text,t text,s text,body text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c jsonb:=t::jsonb;p jsonb:=body::jsonb;env jsonb:=c->'role_envelope';r uuid;before jsonb;after jsonb;result jsonb;ident jsonb;
begin
 if true and env is not null and env is distinct from 'null'::jsonb then
  ident:=authz.nexloop_service_identity_snapshot(d,w);
  if c->>'tenant_id' is distinct from ident->'binding'->>'tenant_id' then raise exception 'role formal current tenant mismatch' using errcode='42501';end if;
  perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
  r:=((env->>'payload')::jsonb->>'run_id')::uuid;
  before:=authz.nexloop_role_formal_current(r,w,c->'formal_reads');
 end if;
 result:=authz.nexloop_effect_intent_command_before_formal_current_v0065(d,w,t,s,body);
 if r is not null then
  after:=authz.nexloop_role_formal_current(r,w,c->'formal_reads');
  if before is distinct from after then raise exception 'role formal current changed' using errcode='42501';end if;
  perform authz.nexloop_role_ttl_reads(c->'formal_reads');
 end if;
 return result;
end $$;
alter function authz.nexloop_effect_intent_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_intent_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_effect_intent_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

alter function authz.nexloop_effect_execution_command(text,text,text,text,text) rename to nexloop_effect_execution_command_before_formal_current_v0065;
revoke all on function authz.nexloop_effect_execution_command_before_formal_current_v0065(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
create function authz.nexloop_effect_execution_command(d text,w text,t text,s text,body text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c jsonb:=t::jsonb;p jsonb:=body::jsonb;env jsonb:=c->'role_envelope';r uuid;before jsonb;after jsonb;result jsonb;ident jsonb;
begin
 if p->>'verb' in ('admit','finalize') and env is not null and env is distinct from 'null'::jsonb then
  ident:=authz.nexloop_service_identity_snapshot(d,w);
  if c->>'tenant_id' is distinct from ident->'binding'->>'tenant_id' then raise exception 'role formal current tenant mismatch' using errcode='42501';end if;
  perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
  r:=((env->>'payload')::jsonb->>'run_id')::uuid;
  before:=authz.nexloop_role_formal_current(r,w,c->'formal_reads');
 end if;
 result:=authz.nexloop_effect_execution_command_before_formal_current_v0065(d,w,t,s,body);
 if r is not null then
  after:=authz.nexloop_role_formal_current(r,w,c->'formal_reads');
  if before is distinct from after then raise exception 'role formal current changed' using errcode='42501';end if;
  perform authz.nexloop_role_ttl_reads(c->'formal_reads');
 end if;
 return result;
end $$;
alter function authz.nexloop_effect_execution_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_execution_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_effect_execution_command(text,text,text,text,text) to nexloop_action_worker;
