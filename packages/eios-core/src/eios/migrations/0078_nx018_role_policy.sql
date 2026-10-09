-- NX-018 step 3 (phase A): Role execution ceiling and assignment scope become enforced
-- governed objects instead of metadata. A Role Run binds only through a signed Source
-- policy recipe whose current READ, Run.allowed_resources, Consumer/Goal/Step and budget
-- must fall inside both the RoleExecutionCeiling and RoleAssignmentScope (intersection;
-- policies never grant). Unknown/metadata-only ceiling_ref or scope fail closed at bind.
-- Dispatch-time re-check of policy EDIT/expiry is phase B (needs runtime_activation hooks).

create function authz.nexloop_role_policy_shape(p_kind text,p jsonb) returns void
language plpgsql immutable set search_path=pg_catalog as $$
declare k text;v jsonb;budget jsonb;
begin
 if jsonb_typeof(p) is distinct from 'object' or jsonb_typeof(p->'active') is distinct from 'boolean'
  or coalesce(p->>'valid_from','') !~ '(Z|\+00:00)$' or coalesce(p->>'valid_until','') !~ '(Z|\+00:00)$'
  or (p->>'valid_from')::timestamptz is null or (p->>'valid_until')::timestamptz is null
  or (p->>'valid_from')::timestamptz >= (p->>'valid_until')::timestamptz then raise exception 'invalid Role policy' using errcode='22023';end if;
 if p_kind='RoleExecutionCeiling' then
  if (select count(*) from jsonb_object_keys(p))<>9 or not(p ?& array['active','action_resources','consumer_ids','goal_ids','step_ids','budget','effect_units','valid_from','valid_until'])
   then raise exception 'exact Role ceiling required' using errcode='22023';end if;
  -- Nine explicit properties. Shape count is independent of optional request fields.
 elsif p_kind='RoleAssignmentScope' then
  if (select count(*) from jsonb_object_keys(p))<>7 or not(p ?& array['active','role_id','consumer_id','goal_ids','step_ids','valid_from','valid_until'])
   then raise exception 'exact Role scope required' using errcode='22023';end if;
  if coalesce(length(p->>'role_id'),0) not between 1 and 512 or coalesce(length(p->>'consumer_id'),0) not between 1 and 512
   or p->>'role_id'='*' or p->>'consumer_id'='*' then raise exception 'explicit Role scope endpoints required' using errcode='22023';end if;
 else raise exception 'Role policy kind unavailable' using errcode='22023';end if;
 for k in select unnest(case when p_kind='RoleExecutionCeiling' then array['action_resources','consumer_ids','goal_ids','step_ids'] else array['goal_ids','step_ids'] end) loop
  if jsonb_typeof(p->k) is distinct from 'array' or jsonb_array_length(p->k) not between 1 and 32
   or (select count(*) from jsonb_array_elements(p->k))<>(select count(distinct x) from jsonb_array_elements(p->k) t(x)) then raise exception 'bounded unique Role allowlist required' using errcode='22023';end if;
  for v in select value from jsonb_array_elements(p->k) loop
   if jsonb_typeof(v) is distinct from 'string' or length(v#>>'{}') not between 1 and 512 or v#>>'{}'='*'
    or (k='action_resources' and v#>>'{}' !~ '^eios:action:[A-Za-z][A-Za-z0-9_.-]*:[1-9][0-9]*$') then raise exception 'explicit Role allowlist required' using errcode='22023';end if;
  end loop;
 end loop;
 if p_kind='RoleExecutionCeiling' then
  budget:=p->'budget';
  if jsonb_typeof(budget) is distinct from 'object' or (select count(*) from jsonb_object_keys(budget))<>5
   or not(budget ?& array['maximum_model_turns','maximum_tool_calls','active_timeout_seconds','maximum_cost','currency'])
   or coalesce(budget->>'maximum_cost','') !~ '^\d+(\.\d{1,8})?$' or jsonb_typeof(budget->'maximum_cost') is distinct from 'string'
   or coalesce(budget->>'currency','') !~ '^[A-Z]{3}$' then raise exception 'exact Role budget required' using errcode='22023';end if;
  for k in select unnest(array['maximum_model_turns','maximum_tool_calls','active_timeout_seconds']) loop
   if jsonb_typeof(budget->k) is distinct from 'number' or coalesce(budget->>k,'') !~ '^[1-9][0-9]*$'
    or (budget->>k)::numeric>(case k when 'maximum_model_turns' then 64 when 'maximum_tool_calls' then 128 else 3600 end) then raise exception 'bounded Role budget required' using errcode='22023';end if;
  end loop;
  if jsonb_typeof(p->'effect_units') is distinct from 'number' or coalesce(p->>'effect_units','') !~ '^[1-9][0-9]*$' or (p->>'effect_units')::numeric>1000000 then raise exception 'bounded Role effect units required' using errcode='22023';end if;
 end if;
end $$;
alter function authz.nexloop_role_policy_shape(text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_role_policy_shape(text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

create function ontology.nexloop_role_policy_constraints() returns trigger
language plpgsql set search_path=pg_catalog as $$
begin
 if new.type_name in ('RoleExecutionCeiling','RoleAssignmentScope') then perform authz.nexloop_role_policy_shape(new.type_name,new.properties);end if;
 return new;
end $$;
alter function ontology.nexloop_role_policy_constraints() owner to nexloop_owner;
revoke all on function ontology.nexloop_role_policy_constraints() from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create trigger nexloop_role_policy_constraints before insert or update on ontology.objects for each row execute function ontology.nexloop_role_policy_constraints();

create function authz.nexloop_role_policy_management(p_digest text,p_world text,a jsonb) returns void
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare d jsonb;deadline timestamptz;
begin
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=a->>'tenant_id' and world=p_world and resource_id=a->>'resource_id' and active for share;
 if not found or not(coalesce(d->'required_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
  or not exists(select 1 from control.nexloop_action_definitions ad where ad.tenant_id=a->>'tenant_id' and ad.world=p_world and ad.resource_id=a->>'resource_id' and ad.active and coalesce(ad.capability->'required_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
  or not(coalesce(authz.nexloop_service_identity_snapshot(p_digest,p_world)->'binding'->'requested_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
 then raise exception 'current Role policy management scope required' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 deadline:=authz.nexloop_role_ttl_proof(a);
 if deadline is null or deadline<=clock_timestamp() then raise exception 'expired Role policy management proof' using errcode='42501';end if;
end $$;
alter function authz.nexloop_role_policy_management(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_role_policy_management(text,text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

alter function authz.nexloop_create_object_action(text,text,text,text,text) rename to nexloop_create_object_action_before_role_policy_v0077;
revoke all on function authz.nexloop_create_object_action_before_role_policy_v0077(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_create_object_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;policy boolean:=(p_payload::jsonb->>'type_name') in ('RoleExecutionCeiling','RoleAssignmentScope');
begin
 if policy then perform authz.nexloop_role_policy_management(p_digest,p_world,p_text::jsonb);end if;
 result:=authz.nexloop_create_object_action_before_role_policy_v0077(p_digest,p_world,p_text,p_signature,p_payload);
 if policy then perform authz.nexloop_role_policy_management(p_digest,p_world,p_text::jsonb);end if;
 return result;
end $$;
alter function authz.nexloop_create_object_action(text,text,text,text,text) owner to nexloop_owner;
-- Exactly the prior public ACL (API + domain/action workers); the wrapper only adds policy checks.
revoke all on function authz.nexloop_create_object_action(text,text,text,text,text) from public,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_create_object_action(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

alter function authz.nexloop_edit_object_action(text,text,text,text,text) rename to nexloop_edit_object_action_before_role_policy_v0077;
revoke all on function authz.nexloop_edit_object_action_before_role_policy_v0077(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_edit_object_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;policy boolean:=(p_payload::jsonb->>'type_name') in ('RoleExecutionCeiling','RoleAssignmentScope');
begin
 if policy then perform authz.nexloop_role_policy_management(p_digest,p_world,p_text::jsonb);end if;
 result:=authz.nexloop_edit_object_action_before_role_policy_v0077(p_digest,p_world,p_text,p_signature,p_payload);
 if policy then perform authz.nexloop_role_policy_management(p_digest,p_world,p_text::jsonb);end if;
 return result;
end $$;
alter function authz.nexloop_edit_object_action(text,text,text,text,text) owner to nexloop_owner;
-- Exactly the prior public ACL (API + domain/action workers); the wrapper only adds policy checks.
revoke all on function authz.nexloop_edit_object_action(text,text,text,text,text) from public,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_edit_object_action(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

create function authz.nexloop_role_policy_validate(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;ident jsonb;r authz.nexloop_run_credentials;
 ceiling jsonb;scope jsonb;env jsonb;definition ontology.objects;link ontology.objects;step ontology.objects;
 budget jsonb;field text;deadline timestamptz;v jsonb;
begin
 if p_text is null or p_payload is null or p_signature is null or octet_length(p_text)>4096 or octet_length(p_payload)>524288
  or jsonb_typeof(a) is distinct from 'object' or jsonb_typeof(p) is distinct from 'object'
  or (select count(*) from jsonb_object_keys(a))<>3 or not(a ?& array['protocol','key_id','parameters_digest'])
  or a->>'protocol' is distinct from 'nexloop-role-policy-v1'
  or (select count(*) from jsonb_object_keys(p))<>10 or not(p ?& array['run_id','role_id','link_id','consumer_id','goal_id','step_id','ceiling_id','scope_id','budget','reads'])
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') or p_world is distinct from 'real'
 then raise exception 'Role policy recipe unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active for share;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-role-policy-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then raise exception 'Role policy signature unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb then raise exception 'Source policy required' using errcode='42501';end if;
 perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 select * into r from authz.nexloop_run_credentials where run_id=(p->>'run_id')::uuid for share;
 if not found or r.source_digest is distinct from p_digest or r.world is distinct from p_world or r.status is distinct from 'active'
  or r.expires_at is null or r.expires_at<=clock_timestamp() then raise exception 'Role policy Run unavailable' using errcode='42501';end if;
 perform authz.nexloop_service_identity_snapshot(r.token_digest,p_world);
 if jsonb_typeof(p->'reads') is distinct from 'object' or (select count(*) from jsonb_object_keys(p->'reads'))<>2 or not(p->'reads' ?& array['ceiling','scope']) then raise exception 'Role policy READ unavailable' using errcode='42501';end if;
 perform 1 from ontology.objects where tenant_id=(ident->'binding'->>'tenant_id') and world=p_world and object_id in(p->>'ceiling_id',p->>'scope_id',p->>'role_id',p->>'link_id',p->>'step_id') order by object_id for share;
 env:=p->'reads'->'ceiling';ceiling:=authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');
 if ceiling->>'type_name' is distinct from 'RoleExecutionCeiling' or ceiling->>'object_id' is distinct from p->>'ceiling_id'
  or (env->>'text')::jsonb->'fields' is distinct from '["action_resources","active","budget","consumer_ids","effect_units","goal_ids","step_ids","valid_from","valid_until"]'::jsonb then raise exception 'Role ceiling READ mismatch' using errcode='42501';end if;
 env:=p->'reads'->'scope';scope:=authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');
 if scope->>'type_name' is distinct from 'RoleAssignmentScope' or scope->>'object_id' is distinct from p->>'scope_id'
  or (env->>'text')::jsonb->'fields' is distinct from '["active","consumer_id","goal_ids","role_id","step_ids","valid_from","valid_until"]'::jsonb then raise exception 'Role scope READ mismatch' using errcode='42501';end if;
 perform authz.nexloop_role_policy_shape('RoleExecutionCeiling',ceiling->'properties');perform authz.nexloop_role_policy_shape('RoleAssignmentScope',scope->'properties');
 select * into definition from ontology.objects where tenant_id=(ident->'binding'->>'tenant_id') and world=p_world and object_id=p->>'role_id' and type_name='RoleDefinition';
 if not found or definition.properties->>'ceiling_ref' is distinct from p->>'ceiling_id' then raise exception 'unknown Role ceiling' using errcode='42501';end if;
 select * into link from ontology.objects where tenant_id=(ident->'binding'->>'tenant_id') and world=p_world and object_id=p->>'link_id' and type_name='ConsumerRoleLink';
 if not found or link.properties->>'scope' is distinct from p->>'scope_id' or link.properties->>'consumer_id' is distinct from p->>'consumer_id' or link.properties->>'role_id' is distinct from p->>'role_id' then raise exception 'unknown Role scope' using errcode='42501';end if;
 select * into step from ontology.objects where tenant_id=(ident->'binding'->>'tenant_id') and world=p_world and object_id=p->>'step_id' and type_name='PlanStep';
 if not found or step.properties->>'consumer_id' is distinct from p->>'consumer_id' or step.properties->>'goal_id' is distinct from p->>'goal_id' then raise exception 'Role Goal Step mismatch' using errcode='42501';end if;
 if scope->'properties'->>'role_id' is distinct from p->>'role_id' or scope->'properties'->>'consumer_id' is distinct from p->>'consumer_id'
  or not(ceiling->'properties'->'consumer_ids' ? (p->>'consumer_id'))
  or not(ceiling->'properties'->'goal_ids' ? (p->>'goal_id')) or not(scope->'properties'->'goal_ids' ? (p->>'goal_id'))
  or not(ceiling->'properties'->'step_ids' ? (p->>'step_id')) or not(scope->'properties'->'step_ids' ? (p->>'step_id'))
  or not(ceiling->'properties'->'action_resources' @> to_jsonb(r.allowed_resources)) then raise exception 'Role ceiling or assignment exceeded' using errcode='42501';end if;
 budget:=ceiling->'properties'->'budget';
 if jsonb_typeof(p->'budget') is distinct from 'object' or (select count(*) from jsonb_object_keys(p->'budget'))<>5
  or not(p->'budget' ?& array['maximum_model_turns','maximum_tool_calls','active_timeout_seconds','maximum_cost','currency'])
  or p->'budget'->>'currency' is distinct from budget->>'currency' then raise exception 'Role budget unavailable' using errcode='42501';end if;
 for field in select unnest(array['maximum_model_turns','maximum_tool_calls','active_timeout_seconds','maximum_cost']) loop
  if p->'budget'->>field is null or (p->'budget'->>field)::numeric<0 or (p->'budget'->>field)::numeric>(budget->>field)::numeric then raise exception 'Role budget exceeded' using errcode='42501';end if;
 end loop;
 deadline:=r.expires_at;
 for v in select x from (values(ceiling),(scope)) t(x) loop
  if v->'properties'->'active' is distinct from 'true'::jsonb or (v->'properties'->>'valid_from')::timestamptz>clock_timestamp() or (v->'properties'->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'Role policy inactive' using errcode='42501';end if;
  deadline:=least(deadline,(v->'properties'->>'valid_until')::timestamptz);
 end loop;
 perform authz.nexloop_role_ttl_reads(p->'reads');
 if deadline is null or deadline<=clock_timestamp() then raise exception 'Role policy deadline expired' using errcode='42501';end if;
 return jsonb_build_object('run_id',r.run_id,'tenant_id',(ident->'binding'->>'tenant_id'),'world',p_world,'ceiling_id',ceiling->>'object_id','ceiling_revision',ceiling->'revision','scope_id',scope->>'object_id','scope_revision',scope->'revision','budget',p->'budget','effect_units',ceiling->'properties'->'effect_units','expires_at',deadline);
end $$;
alter function authz.nexloop_role_policy_validate(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_policy_validate(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

create table authz.nexloop_role_policy_bindings(
 run_id uuid primary key references authz.nexloop_run_credentials(run_id),tenant_id text not null,world text not null,
 ceiling_id text not null,ceiling_revision bigint not null,scope_id text not null,scope_revision bigint not null,
 budget jsonb not null,effect_units integer not null,expires_at timestamptz not null
);
alter table authz.nexloop_role_policy_bindings owner to nexloop_owner;
alter table authz.nexloop_role_policy_bindings enable row level security;
alter table authz.nexloop_role_policy_bindings force row level security;
create policy nexloop_role_policy_tenant on authz.nexloop_role_policy_bindings to nexloop_owner using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on authz.nexloop_role_policy_bindings from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

create function authz.nexloop_role_policy_bind(p_digest text,p_world text,p_role_text text,p_role_signature text,p_role_payload text,p_text text,p_signature text,p_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare value jsonb;again jsonb;role_value jsonb;existing authz.nexloop_role_policy_bindings;k text;
begin
 if session_user<>'nexloop_api' then raise exception 'trusted Role policy binder required' using errcode='42501';end if;
 -- Every existing role/read proof is preserved and validated, never stripped.
 role_value:=authz.nexloop_role_run_validate(p_digest,p_world,p_role_text,p_role_signature,p_role_payload);
 for k in select unnest(array['run_id','role_id','link_id','consumer_id','step_id']) loop
  if p_role_payload::jsonb->>k is distinct from p_payload::jsonb->>k then raise exception 'Role policy selection mismatch' using errcode='42501';end if;
 end loop;
 value:=authz.nexloop_role_policy_validate(p_digest,p_world,p_text,p_signature,p_payload);
 select * into existing from authz.nexloop_role_policy_bindings where run_id=(value->>'run_id')::uuid for update;
 if found then
  if to_jsonb(existing) is distinct from value then raise exception 'Role policy binding immutable' using errcode='42501';end if;
 else insert into authz.nexloop_role_policy_bindings select * from jsonb_populate_record(null::authz.nexloop_role_policy_bindings,value);end if;
 perform authz.nexloop_role_run_bind(p_digest,p_world,p_role_text,p_role_signature,p_role_payload);
 perform authz.nexloop_role_run_current(p_digest,p_world,p_role_text,p_role_signature,p_role_payload);
 again:=authz.nexloop_role_policy_validate(p_digest,p_world,p_text,p_signature,p_payload);
 perform authz.nexloop_role_ttl_reads(p_role_payload::jsonb->'reads');
 if again is distinct from value or (value->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Role policy changed at bind tail' using errcode='42501';end if;
 return value||jsonb_build_object('grants_authority',false);
end $$;
alter function authz.nexloop_role_policy_bind(text,text,text,text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_policy_bind(text,text,text,text,text,text,text,text) from public,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_role_policy_bind(text,text,text,text,text,text,text,text) to nexloop_api;

-- Row/value comparison renders timestamptz; pin UTC like the validator (candidate compared
-- in the session timezone and could never match outside UTC).
create function authz.nexloop_role_policy_current(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare value jsonb;b authz.nexloop_role_policy_bindings;
begin
 value:=authz.nexloop_role_policy_validate(p_digest,p_world,p_text,p_signature,p_payload);
 select * into b from authz.nexloop_role_policy_bindings where run_id=(value->>'run_id')::uuid for share;
 if not found or to_jsonb(b) is distinct from value or b.expires_at<=clock_timestamp() then raise exception 'current Role policy binding unavailable' using errcode='42501';end if;
 perform authz.nexloop_role_ttl_reads(p_payload::jsonb->'reads');
 return value;
end $$;
alter function authz.nexloop_role_policy_current(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_policy_current(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

create function authz.nexloop_role_policy_hint(p_run_digest text,p_world text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;r authz.nexloop_run_credentials;b authz.nexloop_role_run_bindings;policy authz.nexloop_role_policy_bindings;goal text;
begin
 ident:=authz.nexloop_service_identity_snapshot(p_run_digest,p_world);
 if ident->'run_context' is null or ident->'run_context'='null'::jsonb then raise exception 'actual Run policy required' using errcode='42501';end if;
 perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 select * into r from authz.nexloop_run_credentials where token_digest=p_run_digest;
 select * into b from authz.nexloop_role_run_bindings where run_id=r.run_id;
 if not found then return null;end if;
 select * into policy from authz.nexloop_role_policy_bindings where run_id=r.run_id;
 if not found or policy.tenant_id is distinct from ident->'binding'->>'tenant_id' or policy.world is distinct from p_world then raise exception 'Role execution ceiling unavailable' using errcode='42501';end if;
 select properties->>'goal_id' into goal from ontology.objects where tenant_id=policy.tenant_id and world=p_world and object_id=b.step_id and type_name='PlanStep';
 if not found or goal is null then raise exception 'Role policy Goal unavailable' using errcode='42501';end if;
 return jsonb_build_object('_source_digest',r.source_digest,'run_id',r.run_id,'role_id',b.role_id,'link_id',b.link_id,'consumer_id',b.consumer_id,'step_id',b.step_id,'goal_id',goal,'ceiling_id',policy.ceiling_id,'scope_id',policy.scope_id,'budget',policy.budget);
end $$;
alter function authz.nexloop_role_policy_hint(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_policy_hint(text,text) from public,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_role_policy_hint(text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

create function authz.nexloop_role_policy_run_digest(p_digest text,p_world text,p_run uuid) returns text
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;value text;
begin
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 select r.token_digest into value from authz.nexloop_run_credentials r join authz.nexloop_role_run_bindings b on b.run_id=r.run_id where r.run_id=p_run and b.tenant_id=ident->'binding'->>'tenant_id' and b.world=p_world;
 if not found then raise exception 'actual Role Run unavailable' using errcode='42501';end if;
 return value;
end $$;
alter function authz.nexloop_role_policy_run_digest(text,text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_role_policy_run_digest(text,text,uuid) from public,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_role_policy_run_digest(text,text,uuid) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

create function authz.nexloop_issue_role_policy_run(p_digest text,p_world text,p_run_text text,p_run_signature text,p_role_text text,p_role_signature text,p_role_payload text,p_policy_text text,p_policy_signature text,p_policy_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;binding jsonb;
begin
 if session_user<>'nexloop_api' then raise exception 'trusted Role Run issuer required' using errcode='42501';end if;
 result:=authz.nexloop_issue_run_credential(p_digest,p_world,p_run_text,p_run_signature);
 if result->>'run_id' is distinct from p_role_payload::jsonb->>'run_id' or result->>'run_id' is distinct from p_policy_payload::jsonb->>'run_id' then raise exception 'Role issuance binding mismatch' using errcode='42501';end if;
 binding:=authz.nexloop_role_policy_bind(p_digest,p_world,p_role_text,p_role_signature,p_role_payload,p_policy_text,p_policy_signature,p_policy_payload);
 perform authz.nexloop_role_ttl_reads(p_role_payload::jsonb->'reads');perform authz.nexloop_role_ttl_reads(p_policy_payload::jsonb->'reads');
 if (binding->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Role issuance policy expired' using errcode='42501';end if;
 return result;
end $$;
alter function authz.nexloop_issue_role_policy_run(text,text,text,text,text,text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_issue_role_policy_run(text,text,text,text,text,text,text,text,text,text) from public,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_issue_role_policy_run(text,text,text,text,text,text,text,text,text,text) to nexloop_api;

-- Fail closed: a Role Run binding requires an existing policy binding for that Run whose
-- ceiling/scope are exactly the RoleDefinition.ceiling_ref / ConsumerRoleLink.scope objects.
alter function authz.nexloop_role_run_bind(text,text,text,text,text) rename to nexloop_role_run_bind_before_role_policy_v0077;
revoke all on function authz.nexloop_role_run_bind_before_role_policy_v0077(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_role_run_bind(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare p jsonb:=p_payload::jsonb;ident jsonb;policy authz.nexloop_role_policy_bindings;definition ontology.objects;link ontology.objects;ceiling ontology.objects;scope ontology.objects;result jsonb;
begin
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 select * into policy from authz.nexloop_role_policy_bindings where run_id=(p->>'run_id')::uuid for share;
 if not found or policy.tenant_id is distinct from ident->'binding'->>'tenant_id' or policy.world is distinct from p_world then
  raise exception 'Role execution policy mandatory' using errcode='42501';end if;
 select * into definition from ontology.objects where tenant_id=policy.tenant_id and world=p_world and type_name='RoleDefinition' and object_id=p->>'role_id' for share;
 select * into link from ontology.objects where tenant_id=policy.tenant_id and world=p_world and type_name='ConsumerRoleLink' and object_id=p->>'link_id' for share;
 select * into ceiling from ontology.objects where tenant_id=policy.tenant_id and world=p_world and type_name='RoleExecutionCeiling' and object_id=definition.properties->>'ceiling_ref' for share;
 if not found or ceiling.object_id is distinct from policy.ceiling_id then raise exception 'unknown Role ceiling' using errcode='42501';end if;
 select * into scope from ontology.objects where tenant_id=policy.tenant_id and world=p_world and type_name='RoleAssignmentScope' and object_id=link.properties->>'scope' for share;
 if not found or scope.object_id is distinct from policy.scope_id then raise exception 'unknown Role scope' using errcode='42501';end if;
 result:=authz.nexloop_role_run_bind_before_role_policy_v0077(p_digest,p_world,p_text,p_signature,p_payload);
 return result;
end $$;
alter function authz.nexloop_role_run_bind(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_run_bind(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_role_run_bind(text,text,text,text,text) to nexloop_api;
