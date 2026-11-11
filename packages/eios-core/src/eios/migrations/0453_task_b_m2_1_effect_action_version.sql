-- Option B M2 (ADR-046 §3; docs/planning-drafts/b-effect-action-follows-schema-version-20261111.md §2.2-§2.4, §4 row 2b, step M2.1;
-- L6 M1 verdict preconditions M2-a and M2-b). Temporary number; the dispatcher renumbers. A' (0446/0449) stays until M4.
-- 1. authz.nexloop_effect_resource_slot: the slot of `eios:action:<allow-listed stable name>:<version>` (any positive version).
-- 2. authz.nexloop_effect_bind_action_resource: the effect Action resource a bind or configure must name for a Consumer version -
--    authz.nexloop_effect_action_version (0451); until A' is removed (M4), a Consumer version no admission covers falls back to the
--    allow-list :1, which A' proves at intent time (as before option B); two covering admissions are still refused. Like the
--    registrar's choice, an admitted :N is chosen only after register followed every :1 grant to it (no window after an admission).
--    authz.nexloop_effect_action_resource_for: the same for the caller's verb payload (Python chooses before signing, §2.4 step 4).
-- 3. authz.nexloop_effect_context_command (0446, copied; tests/support/task_b_m2_edits.py): the proofs may name any version of the
--    effect Action, and the command recomputes the selection and requires both proofs to name it (M2-a); configure likewise for the
--    control's Consumer; the context stores the selected resource; a step's rebinding keeps its context's version.
-- 4. The effect intent commands (0446 v0040, 0183 v0041/v0052, copied): any version of an allow-listed effect Action is accepted at
--    the envelope, and the intent must name exactly its context's action_resource.
-- 5. Run issuance (message Run v0049/v0190, reply fallback v0110), the effect catalog (v0401, hint) and the local delivery
--    authority (0175: the intent's own version) use the selected versions;
--    authz.nexloop_effect_consumer_action_resources gives the relay the versions for a Consumer before it issues the Run.
--    Context packs still name :1 (contract v7) until v8 (M2.2); the runtime takes the version from the Run, never from the pack.
--    H1 (a) - admission in the publish transaction - is M2.3.
-- 6. Retirement (M2-b): a version an unexpired context names is not retired (code referenced_by_unexpired_context). Contexts are
--    immutable and signed for their resource; re-selecting would rewrite a bound context. They expire within minutes, so the owner
--    retries the retirement after the last one expires.

-- 1. -------------------------------------------------------------------------------------------------------------------------
create function authz.nexloop_effect_resource_slot(p_resource text) returns text
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select ea.slot_identity from control.nexloop_effect_actions ea
  where p_resource ~ '^eios:action:[a-z][a-z0-9_.]*:[1-9][0-9]{0,3}$' and ea.stable_name=split_part(p_resource,':',3)
$$;
alter function authz.nexloop_effect_resource_slot(text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_resource_slot(text) from public;

-- 2. -------------------------------------------------------------------------------------------------------------------------
create function authz.nexloop_effect_bind_action_resource(p_tenant text,p_world text,p_slot text,p_consumer_version integer) returns text
 language plpgsql volatile security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_name text;v integer;v_base text;
begin
 select ea.stable_name into v_name from control.nexloop_effect_actions ea where ea.slot_identity=p_slot;
 if v_name is null then return null;end if;
 -- L6 F2: the active admissions of this effect Action are read under a share lock until the caller's transaction ends (bind,
 -- issuance), so a retirement (for update) waits for it, and a bind after a retirement sees the retirement.
 perform set_config('eios.tenant_id',p_tenant,true);
 perform 1 from control.nexloop_effect_action_admissions a where a.tenant_id=p_tenant and a.world=p_world and a.stable_name=v_name
  and a.retired_at is null for share;
 begin
  v:=authz.nexloop_effect_action_version(p_tenant,p_world,p_slot,p_consumer_version);
 exception when insufficient_privilege then
  perform set_config('eios.tenant_id',p_tenant,true);
  -- Transitional (A', until M4): no admission covers this Consumer version - the allow-list :1, proven by A' at intent time.
  if exists(select 1 from control.nexloop_effect_action_admissions a join control.nexloop_action_definitions d
    on d.tenant_id=a.tenant_id and d.world=a.world and d.active and d.resource_id=a.resource_id
    where a.tenant_id=p_tenant and a.world=p_world and a.stable_name=v_name and a.retired_at is null
     and exists(select 1 from jsonb_array_elements(d.definition->'governance'->'change_scope'->'object_types') r
      where r->>'tenant_id'=p_tenant and r->>'stable_name'='Consumer' and (r->>'version')::integer=p_consumer_version)) then
   raise exception 'effect Action version unavailable' using errcode='42501';end if;
  select ea.version into v from control.nexloop_effect_actions ea where ea.slot_identity=p_slot;
 end;
 -- Transitional (A', until M4), like the registrar's choice (0446): an admitted successor is chosen only once register has
 -- followed every grant of the allow-list version to it (each principal granted :1 is granted :N); before that the allow-list :1,
 -- proven by A' - so no relay fails between an admission and register.
 perform set_config('eios.tenant_id',p_tenant,true);
 select ea.resource_id into v_base from control.nexloop_effect_actions ea where ea.slot_identity=p_slot;
 if v_base is distinct from 'eios:action:'||v_name||':'||v and exists(select 1 from authz.nexloop_authority_facts f
   where f.tenant_id=p_tenant and f.fact_kind='grants' and f.entity_key[2]=v_base
    and not exists(select 1 from authz.nexloop_authority_facts g where g.tenant_id=p_tenant and g.fact_kind='grants'
     and g.entity_key=array[f.entity_key[1],'eios:action:'||v_name||':'||v])) then
  return v_base;end if;
 return 'eios:action:'||v_name||':'||v;
end $$;
alter function authz.nexloop_effect_bind_action_resource(text,text,text,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_bind_action_resource(text,text,text,integer) from public;

create function authz.nexloop_effect_action_resource_for(p_digest text,p_world text,p_verb text,p_payload jsonb,p_action text) returns text
 language plpgsql volatile security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant text:=ident->'binding'->>'tenant_id';v_consumer text;v integer;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or p_verb not in ('configure','bind') or jsonb_typeof(p_payload) is distinct from 'object' then
  raise exception 'effect registration unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 if p_verb='configure' then
  select o.properties->>'consumer_id' into v_consumer from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='EffectControl' and o.object_id=p_payload->>'control_id';
 else
  select o.properties->>'consumer_id' into v_consumer from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='PlanStep' and o.object_id=p_payload->>'step_id';
 end if;
 select o.schema_version into v from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=v_consumer;
 if v is null then return null;end if;
 return authz.nexloop_effect_bind_action_resource(v_tenant,p_world,(select ea.slot_identity from control.nexloop_effect_actions ea where ea.stable_name=p_action),v);
end $$;
alter function authz.nexloop_effect_action_resource_for(text,text,text,jsonb,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_action_resource_for(text,text,text,jsonb,text) from public;
grant execute on function authz.nexloop_effect_action_resource_for(text,text,text,jsonb,text) to nexloop_api,nexloop_domain_worker;

-- 3. -------------------------------------------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION authz.nexloop_effect_context_command(p_digest text, p_world text, p_claims text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare c jsonb:=p_claims::jsonb;p jsonb:=p_payload::jsonb;k bytea;identity jsonb;executor_identity jsonb;run_identity jsonb;
 pub control.nexloop_action_definitions;l control.nexloop_effect_control_ledger;ctx control.nexloop_effect_contexts;
 goal ontology.objects;step ontology.objects;consumer ontology.objects;ctl ontology.objects;r ontology.objects;
 run_row authz.nexloop_run_credentials;claim runtime.nexloop_action_claims;field_name text;schema_body jsonb;context_uuid uuid;limit_at timestamptz;existing control.nexloop_effect_plan_bindings;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_claims)>1048576 or octet_length(p_payload)>65536 then raise exception 'effect registration unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-context-v1:'||p_claims,'UTF8'),k,'sha256'),'hex')
  or c->>'protocol' is distinct from 'nexloop-effect-context-v1' or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp() or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or p->>'verb' is null or p->>'verb' not in ('configure','bind')
  or c->>'resource_id' !~ ('^eios:action:'||(case p->>'verb' when 'configure' then 'nexloop\.effect\.configure_control' else 'nexloop\.plan\.bind_effect_context' end)||':[1-9][0-9]{0,3}$')
 then raise exception 'effect registration unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if identity->'run_context' is not null and identity->'run_context'<>'null'::jsonb then raise exception 'independent authority required' using errcode='42501';end if;
 select * into pub from control.nexloop_action_definitions where tenant_id=c->>'tenant_id' and world=p_world and resource_id=c->>'resource_id' and active for share;
 if not found or pub.definition is distinct from c->'definition' or pub.capability is distinct from c->'capability'
  or pub.definition->'governance'->>'approval_mode' is distinct from 'none' or pub.definition->'governance'->>'risk_level' is distinct from 'low'
  or jsonb_array_length(pub.definition->'governance'->'policy_refs') is distinct from 0 or jsonb_array_length(coalesce(pub.definition->'preconditions','[]'::jsonb))<>0
 then raise exception 'published registrar unavailable' using errcode='42501';end if;
 if pub.definition->'input_schema' is distinct from authz.nexloop_effect_registrar_schema(p->>'verb')
  or jsonb_typeof(p) is distinct from 'object'
  or (select count(*) from jsonb_object_keys(p)) is distinct from jsonb_array_length(pub.definition->'input_schema'->'required')
  or c->'definition_reference'->>'stable_name' is distinct from pub.definition->>'stable_name'
  or c->'definition_reference'->>'contract_digest' is distinct from pub.definition->>'contract_digest'
  or c->'definition_reference'->>'tenant_id' is distinct from c->>'tenant_id'
  or c->'definition_reference'->>'version' is distinct from pub.definition->>'version'
  or pub.resource_id is distinct from 'eios:action:'||(pub.definition->>'stable_name')||':'||(pub.definition->>'version')
  or c->'definition_reference'->>'definition_type' is distinct from 'action'
 then raise exception 'registrar input schema unavailable' using errcode='42501';end if;
 for field_name in select jsonb_array_elements_text(pub.definition->'input_schema'->'required') loop
  if field_name='verb' then continue;end if;
  if field_name in ('control_id','step_id') then
   if jsonb_typeof(p->field_name) is distinct from 'string' or (p->>field_name) !~ '^[a-f0-9]{64}$' then raise exception 'registrar input invalid' using errcode='22023';end if;
  else
   if jsonb_typeof(p->field_name) is distinct from 'number' or (p->>field_name)::numeric<>trunc((p->>field_name)::numeric)
    or (p->>field_name)::numeric not between 1 and 9223372036854775807 then raise exception 'registrar revision invalid' using errcode='22023';end if;
  end if;
 end loop;
 perform 1 from ontology.object_type_versions t where t.tenant_id=c->>'tenant_id' and exists(select 1 from jsonb_array_elements(c->'schemas') s
  where s->>'type_name'=t.type_name and (s->>'version')::integer=t.version) order by t.type_name,t.version for share;
 for schema_body in select * from jsonb_array_elements(c->'schemas') loop
  if schema_body->'only_edit_via_actions' is distinct from 'true'::jsonb or not exists(select 1 from ontology.object_type_versions t
   where t.tenant_id=c->>'tenant_id' and t.type_name=schema_body->>'type_name' and t.version=(schema_body->>'version')::integer and t.definition=schema_body)
  then raise exception 'registrar schema stale' using errcode='42501';end if;
 end loop;
 -- Real Action reserve/finalize is supplied by the private assembly on this same
 -- connection. This endpoint cannot act under an unrelated/terminal claim.
 select * into claim from runtime.nexloop_action_claims where tenant_id=c->>'tenant_id' and world=p_world
  and action_name=pub.definition->>'stable_name' and intent_id=c->>'claim_id' for update;
 if not found or claim.principal_id is distinct from c->>'principal_id'
  or claim.claim->>'state' is null or claim.claim->>'state' not in ('active','terminal')
  or claim.claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or claim.claim->'binding'->'action_reference' is distinct from c->'definition_reference'
  or claim.claim->'binding'->'capability_binding' is distinct from c->'capability_binding'
  or claim.claim->>'state'='active' and (claim.claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or claim.claim->>'state'='terminal' and claim.claim->'terminal_outcome'->>'status' is distinct from 'succeeded'
 then raise exception 'registrar Action claim unavailable' using errcode='42501';end if;
 if c->'executor_proof'->>'tenant_id' is distinct from c->>'tenant_id' or c->'executor_proof'->>'world' is distinct from p_world
  or p->>'verb'='bind' and (c->'run_proof'->>'tenant_id' is distinct from c->>'tenant_id' or c->'run_proof'->>'world' is distinct from p_world)
  or c->'executor_proof'->>'expires_at' is null
  or (c->'executor_proof'->>'expires_at')::timestamptz<=clock_timestamp()
  or (c->'executor_proof'->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or p->>'verb'='bind' and (c->'run_proof'->>'expires_at' is null or (c->'run_proof'->>'expires_at')::timestamptz<=clock_timestamp()
   or (c->'run_proof'->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds')
 then raise exception 'registrar proof unavailable' using errcode='42501';end if;
 executor_identity:=authz.nexloop_service_identity_snapshot(c->>'executor_digest',p_world);
 perform authz.nexloop_assert_action_authority(c->>'executor_digest',p_world,c->'executor_proof');
 if authz.nexloop_effect_resource_slot(c->'executor_proof'->>'resource_id') is null
  or executor_identity->'run_context' is not null and executor_identity->'run_context'<>'null'::jsonb then raise exception 'executor unavailable' using errcode='42501';end if;
 if p->>'verb'='configure' then
  select * into ctl from ontology.objects where tenant_id=c->>'tenant_id' and world=p_world and type_name='EffectControl' and object_id=p->>'control_id' for share;
  if not found or ctl.nexloop_revision is distinct from (p->>'control_revision')::bigint
   or ctl.properties->>'owner_principal' is distinct from c->>'principal_id'
   or ctl.properties->>'executor_principal' is distinct from executor_identity->'binding'->>'subject_principal_id'
   or ctl.properties->'allow_effect' is distinct from 'true'::jsonb or jsonb_typeof(ctl.properties->'budget_units') is distinct from 'number'
   or (ctl.properties->>'budget_units')::bigint<0 or ctl.properties->>'valid_until' is null
   or (ctl.properties->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'control owner unavailable' using errcode='42501';end if;
  if not exists(select 1 from jsonb_array_elements(pub.definition->'governance'->'change_scope'->'object_types') t
   where t->>'tenant_id'=c->>'tenant_id' and t->>'stable_name'='EffectControl' and (t->>'version')::integer=ctl.schema_version)
  then raise exception 'control schema scope unavailable' using errcode='42501';end if;
  if not exists(select 1 from jsonb_array_elements(c->'schemas') t where t->>'type_name'='EffectControl' and (t->>'version')::integer=ctl.schema_version)
  then raise exception 'control schema snapshot unavailable' using errcode='42501';end if;
  select * into consumer from ontology.objects where tenant_id=c->>'tenant_id' and world=p_world and type_name='Consumer' and object_id=ctl.properties->>'consumer_id' for share;
  if not found then raise exception 'Consumer unavailable' using errcode='42501';end if;
  if c->'executor_proof'->>'resource_id' is distinct from authz.nexloop_effect_bind_action_resource(c->>'tenant_id',p_world,authz.nexloop_effect_resource_slot(c->'executor_proof'->>'resource_id'),consumer.schema_version) then
   raise exception 'effect Action version unavailable' using errcode='42501';end if;
  insert into control.nexloop_effect_control_ledger values(ctl.object_id,c->>'tenant_id',p_world,consumer.object_id,c->>'principal_id',p_digest,
   ctl.properties->>'executor_principal',c->>'executor_digest',ctl.nexloop_revision,(ctl.properties->>'budget_units')::bigint,0,(ctl.properties->>'valid_until')::timestamptz)
  on conflict(control_id) do update set control_revision=excluded.control_revision,budget_units=excluded.budget_units,valid_until=excluded.valid_until
   where nexloop_effect_control_ledger.owner_principal=excluded.owner_principal and nexloop_effect_control_ledger.owner_digest=excluded.owner_digest
    and nexloop_effect_control_ledger.executor_principal=excluded.executor_principal and nexloop_effect_control_ledger.executor_digest=excluded.executor_digest
    and nexloop_effect_control_ledger.consumer_id=excluded.consumer_id and nexloop_effect_control_ledger.tenant_id=excluded.tenant_id and nexloop_effect_control_ledger.world=excluded.world
  returning * into l;
  if not found then raise exception 'control identity conflict' using errcode='42501';end if;
  context_uuid:=null;
 else
  run_identity:=authz.nexloop_service_identity_snapshot(c->>'run_digest',p_world);
  perform authz.nexloop_assert_action_authority(c->>'run_digest',p_world,c->'run_proof');
  if run_identity->'run_context' is null or run_identity->'run_context'='null'::jsonb
   or authz.nexloop_effect_resource_slot(c->'run_proof'->>'resource_id') is null then raise exception 'Run required' using errcode='42501';end if;
  select * into run_row from authz.nexloop_run_credentials where token_digest=c->>'run_digest' for share;
  if not found then raise exception 'Run required' using errcode='42501';end if;
  -- Lookup Step is not authority until actual planner proof plus schema bundle
  -- and all real linked object revisions are checked below.
  select * into step from ontology.objects where tenant_id=c->>'tenant_id' and world=p_world and type_name='PlanStep' and object_id=p->>'step_id';
  if not found then raise exception 'Step unavailable' using errcode='42501';end if;
  select * into l from control.nexloop_effect_control_ledger where control_id=step.properties->>'control_id' and tenant_id=c->>'tenant_id' and world=p_world for update;
  if not found then raise exception 'configured control required' using errcode='42501';end if;
  for r in select * from ontology.objects where tenant_id=c->>'tenant_id' and world=p_world
   and object_id in(step.object_id,step.properties->>'goal_id',l.control_id,l.consumer_id) order by object_id for share loop
   if r.object_id=step.object_id then step:=r;elsif r.object_id=step.properties->>'goal_id' then goal:=r;
   elsif r.object_id=l.control_id then ctl:=r;elsif r.object_id=l.consumer_id then consumer:=r;end if;
  end loop;
  if goal.object_id is null or ctl.object_id is null or consumer.object_id is null or goal.type_name<>'Goal' or ctl.type_name<>'EffectControl' or consumer.type_name<>'Consumer'
   or step.nexloop_revision is distinct from (p->>'step_revision')::bigint or goal.nexloop_revision is distinct from (p->>'goal_revision')::bigint
   or consumer.nexloop_revision is distinct from (p->>'consumer_revision')::bigint or ctl.nexloop_revision is distinct from (p->>'control_revision')::bigint
   or l.control_revision is distinct from ctl.nexloop_revision or l.valid_until<=clock_timestamp()
   or step.properties->>'consumer_id' is distinct from consumer.object_id or goal.properties->>'consumer_id' is distinct from consumer.object_id
   or jsonb_typeof(step.properties->'submitter_principals') is distinct from 'array'
   or jsonb_array_length(step.properties->'submitter_principals') not between 1 and 32
   or not(step.properties->'submitter_principals' ? (run_identity->'binding'->>'subject_principal_id'))
   or exists(select 1 from jsonb_array_elements(step.properties->'submitter_principals') v where jsonb_typeof(v)<>'string' or length(v#>>'{}') not between 1 and 200)
   or step.properties->'submitter_principals' is distinct from (select jsonb_agg(v order by v collate "C") from (select distinct value#>>'{}' v from jsonb_array_elements(step.properties->'submitter_principals')) allowed)
   or c->>'principal_id'=run_identity->'binding'->>'subject_principal_id' or c->>'principal_id'=l.owner_principal
   or l.executor_principal is distinct from executor_identity->'binding'->>'subject_principal_id' or l.executor_digest is distinct from c->>'executor_digest'
   or not exists(select 1 from control.nexloop_effect_actions ea where ea.stable_name=step.properties->>'action_name'
    and authz.nexloop_effect_bind_action_resource(c->>'tenant_id',p_world,ea.slot_identity,consumer.schema_version)=c->'run_proof'->>'resource_id'
    and c->'run_proof'->>'resource_id'=c->'executor_proof'->>'resource_id')
   or step.properties->>'state' is distinct from 'ready'
   or goal.properties->>'state' is distinct from 'active' or ctl.properties->'allow_effect' is distinct from 'true'::jsonb
  then raise exception 'plan authority unavailable' using errcode='42501';end if;
  -- Current published registrar must explicitly govern every formal type/version.
  for r in select * from ontology.objects where tenant_id=c->>'tenant_id' and world=p_world and object_id in(step.object_id,goal.object_id,ctl.object_id,consumer.object_id) loop
   if not exists(select 1 from jsonb_array_elements(pub.definition->'governance'->'change_scope'->'object_types') t
    where t->>'tenant_id'=c->>'tenant_id' and t->>'stable_name'=r.type_name and (t->>'version')::integer=r.schema_version)
   then raise exception 'registrar schema scope unavailable' using errcode='42501';end if;
   if not exists(select 1 from jsonb_array_elements(c->'schemas') t where t->>'type_name'=r.type_name and (t->>'version')::integer=r.schema_version)
   then raise exception 'registrar schema snapshot unavailable' using errcode='42501';end if;
  end loop;
  limit_at:=least(run_row.expires_at,l.valid_until,(goal.properties->>'valid_until')::timestamptz);
  if limit_at is null or limit_at<=clock_timestamp() then raise exception 'plan expired' using errcode='42501';end if;
  select * into existing from control.nexloop_effect_plan_bindings where tenant_id=c->>'tenant_id' and world=p_world and step_id=step.object_id for update;
  if found then
   context_uuid:=existing.context_id;
   if (select coalesce(x.action_resource,c->'executor_proof'->>'resource_id') from control.nexloop_effect_contexts x where x.context_id=existing.context_id)
    is distinct from c->'executor_proof'->>'resource_id' then raise exception 'effect context conflict' using errcode='P0001';end if;
   if existing.goal_id<>goal.object_id or existing.goal_revision<>goal.nexloop_revision or existing.step_revision<>step.nexloop_revision
    or existing.control_id<>ctl.object_id or existing.control_revision<>ctl.nexloop_revision or existing.submitter_principals is distinct from step.properties->'submitter_principals'
   then raise exception 'effect context conflict' using errcode='P0001';end if;
  else
   context_uuid:=gen_random_uuid();
   insert into control.nexloop_effect_contexts values(context_uuid,c->>'tenant_id',p_world,consumer.object_id,'goal:'||goal.object_id,
    'goal:'||goal.object_id||':revision:'||goal.nexloop_revision||':step:'||step.nexloop_revision||':control:'||ctl.nexloop_revision,
    'step:'||step.object_id,(select ea.slot_identity from control.nexloop_effect_actions ea where ea.stable_name=step.properties->>'action_name'),consumer.nexloop_revision,ctl.nexloop_revision,true,least(l.valid_until,(goal.properties->>'valid_until')::timestamptz),
    l.budget_units,0,jsonb_build_array(jsonb_build_object('resource_ref','consumer:'||consumer.object_id,'revision',consumer.nexloop_revision)),l.executor_principal,c->'executor_proof'->>'resource_id');
   insert into control.nexloop_effect_plan_bindings values(context_uuid,c->>'tenant_id',p_world,goal.object_id,goal.nexloop_revision,step.object_id,step.nexloop_revision,ctl.object_id,ctl.nexloop_revision,step.properties->'submitter_principals');
  end if;
  insert into control.nexloop_effect_run_contexts values(run_row.run_id,context_uuid,limit_at) on conflict(run_id,slot_identity) do nothing;
  if not exists(select 1 from control.nexloop_effect_run_contexts where run_id=run_row.run_id and context_id=context_uuid and valid_until>clock_timestamp()) then raise exception 'Run context conflict' using errcode='42501';end if;
  perform authz.nexloop_assert_effect_plan(context_uuid,c->>'tenant_id',p_world);
  perform authz.nexloop_assert_action_authority(c->>'run_digest',p_world,c->'run_proof');
 end if;
 perform authz.nexloop_assert_action_authority(c->>'executor_digest',p_world,c->'executor_proof');
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if l.valid_until<=clock_timestamp() or (ctl.properties->>'valid_until')::timestamptz<=clock_timestamp()
  or (c->>'expires_at')::timestamptz<=clock_timestamp() or (c->'executor_proof'->>'expires_at')::timestamptz<=clock_timestamp()
  or p->>'verb'='bind' and (c->'run_proof'->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'registrar expired' using errcode='42501';end if;
 return jsonb_build_object('context_ref',context_uuid,'control_id',coalesce(ctl.object_id,l.control_id),'scope','effect_context','business_action_success',false);
end $function$;

-- 4. -------------------------------------------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION authz.nexloop_effect_intent_command_v0040(p_digest text, p_world text, p_claims text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare c jsonb:=p_claims::jsonb;p jsonb:=p_payload::jsonb;k bytea;identity jsonb;run_row authz.nexloop_run_credentials;
 ctx control.nexloop_effect_contexts;link control.nexloop_effect_run_contexts;consumer ontology.objects;
 published control.nexloop_action_definitions;stored runtime.nexloop_effect_intents;
 semantic jsonb;expected jsonb;digest text;now_at timestamptz;new_intent uuid;new_receipt uuid;v_action text;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_claims)>1048576 or octet_length(p_payload)>131072 then
  raise exception 'effect authority unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-intent-v1:'||p_claims,'UTF8'),k,'sha256'),'hex')
  or c->>'protocol' is distinct from 'nexloop-effect-intent-v1' or authz.nexloop_effect_resource_slot(c->>'resource_id') is null or split_part(c->>'resource_id',':',4) is distinct from p->>'action_version'
  or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp() or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or p->>'verb' is null or p->>'verb' not in ('submit','find') then raise exception 'effect authority unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if identity->'run_context' is null or identity->'run_context'='null'::jsonb then raise exception 'effect Run required' using errcode='42501';end if;
 select * into run_row from authz.nexloop_run_credentials where token_digest=p_digest for share;
 if not found then raise exception 'effect Run required' using errcode='42501';end if;
 select * into link from control.nexloop_effect_run_contexts where run_id=run_row.run_id and slot_identity=authz.nexloop_effect_resource_slot(c->>'resource_id') for share;
 if not found or link.valid_until<=clock_timestamp() then raise exception 'effect context unavailable' using errcode='42501';end if;
 select * into ctx from control.nexloop_effect_contexts where context_id=link.context_id
  and tenant_id=c->>'tenant_id' and world=p_world for update;
 if not found or not ctx.allow_effect or ctx.valid_until<=clock_timestamp() then raise exception 'effect controls unavailable' using errcode='42501';end if;
 select ea.stable_name into v_action from control.nexloop_effect_actions ea where ea.slot_identity=ctx.slot_identity and c->>'resource_id'=coalesce(ctx.action_resource,ea.resource_id);
 if v_action is null then raise exception 'effect Action outside context' using errcode='42501';end if;
 select * into consumer from ontology.objects where tenant_id=ctx.tenant_id and world=p_world and type_name='Consumer' and object_id=ctx.consumer_id for share;
 if not found or consumer.nexloop_revision is distinct from ctx.consumer_revision then raise exception 'effect consumer stale' using errcode='42501';end if;
 select * into published from control.nexloop_action_definitions where tenant_id=ctx.tenant_id and world=p_world and resource_id=c->>'resource_id' and active for share;
 if not found or published.definition is distinct from c->'definition' or published.capability is distinct from c->'capability'
  or published.definition->'governance'->>'approval_mode' is distinct from 'none'
  or published.definition->'governance'->>'risk_level' is distinct from 'low'
  or jsonb_array_length(published.definition->'governance'->'policy_refs') is distinct from 0
  or jsonb_array_length(coalesce(published.definition->'preconditions','[]'::jsonb))<>0 then
  raise exception 'effect published Action unavailable' using errcode='42501';end if;
 if not exists(select 1 from jsonb_array_elements(published.definition->'governance'->'change_scope'->'object_types') r
  where r->>'tenant_id'=ctx.tenant_id and r->>'stable_name'='Consumer' and (r->>'version')::integer=consumer.schema_version)
  and authz.nexloop_effect_scope_successor_proof(ctx.context_id,ctx.tenant_id,p_world,published,consumer.schema_version) is null then
  raise exception 'effect Consumer outside Action' using errcode='42501';end if;
 for expected in select * from jsonb_array_elements(ctx.expected_versions) loop
  if jsonb_typeof(expected) is distinct from 'object' or expected->>'resource_ref' is distinct from 'consumer:'||ctx.consumer_id
   or jsonb_typeof(expected->'revision') is distinct from 'number' or (expected->>'revision')::bigint is distinct from consumer.nexloop_revision
   or (select count(*) from jsonb_object_keys(expected))<>2 then
   raise exception 'effect expected versions unsupported or stale' using errcode='42501';end if;
 end loop;
 if p->>'verb'='find' then
  select * into stored from runtime.nexloop_effect_intents where intent_id=(p->>'intent_id')::uuid
   and tenant_id=ctx.tenant_id and world=p_world and consumer_id=ctx.consumer_id and goal_identity=ctx.goal_identity
   and plan_step_identity=ctx.plan_step_identity and slot_identity=ctx.slot_identity and action_name=v_action;
  if not found then raise exception 'effect receipt unavailable' using errcode='42501';end if;
 else
  if jsonb_typeof(p->'parameters') is distinct from 'object' or p->>'parameters_text' is null
   or (p->>'parameters_text')::jsonb is distinct from p->'parameters'
   or p->>'provider_payload_digest' is distinct from encode(sha256(convert_to(p->>'parameters_text','UTF8')),'hex') then
   raise exception 'effect parameters unavailable' using errcode='22023';end if;
  if authz.nexloop_effect_parameters_valid(published.definition->'input_schema',p->'parameters') is distinct from true then
   raise exception 'effect parameters schema rejected' using errcode='22023';end if;
  semantic:=jsonb_build_object('consumer_id',ctx.consumer_id,'goal_identity',ctx.goal_identity,'goal_version_ref',ctx.goal_version_ref,
   'plan_step_identity',ctx.plan_step_identity,'slot_identity',ctx.slot_identity,'expected_versions',ctx.expected_versions,
   'consumer_revision',ctx.consumer_revision,'action_name',v_action,'action_version',(p->>'action_version')::integer,
   'action_contract_digest',published.definition->>'contract_digest','capability',published.capability,'parameters',p->'parameters');
  digest:=encode(sha256(convert_to(semantic::text,'UTF8')),'hex');
  select * into stored from runtime.nexloop_effect_intents where tenant_id=ctx.tenant_id and world=p_world and consumer_id=ctx.consumer_id
   and goal_identity=ctx.goal_identity and plan_step_identity=ctx.plan_step_identity and slot_identity=ctx.slot_identity and action_name=v_action for update;
  if found then
   if stored.business_digest is distinct from digest or stored.provider_payload_digest is distinct from p->>'provider_payload_digest' then
    raise exception 'effect_payload_conflict' using errcode='P0001';end if;
  else
   if ctx.reserved_units>=ctx.budget_units then raise exception 'effect budget unavailable' using errcode='42501';end if;
   new_intent:=pg_catalog.gen_random_uuid();new_receipt:=pg_catalog.gen_random_uuid();
   insert into runtime.nexloop_effect_intents values(new_intent,new_receipt,ctx.context_id,ctx.tenant_id,p_world,ctx.consumer_id,ctx.goal_identity,
    ctx.plan_step_identity,ctx.slot_identity,v_action,(p->>'action_version')::integer,digest,p->>'provider_payload_digest',semantic,
    published.definition,published.capability,ctx.executor_principal,ctx.control_revision,clock_timestamp(),'accepted') returning * into stored;
   insert into runtime.nexloop_effect_outbox(intent_id,tenant_id,world,available_at,created_at) values(stored.intent_id,ctx.tenant_id,p_world,clock_timestamp(),clock_timestamp());
   update control.nexloop_effect_contexts set reserved_units=reserved_units+1 where context_id=ctx.context_id;
  end if;
  insert into runtime.nexloop_effect_submissions values(stored.intent_id,run_row.run_id,c->>'principal_id',run_row.source_digest,clock_timestamp()) on conflict do nothing;
 end if;
 -- Repeat all current authority/controls after lock waits and before ACK. Writes
 -- roll back together if a final proof or time check fails.
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 perform authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ctx.valid_until<=clock_timestamp() or link.valid_until<=clock_timestamp() or run_row.expires_at<=clock_timestamp()
  or (c->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect authority expired' using errcode='42501';end if;
 return jsonb_build_object('intent_id',stored.intent_id,'receipt_id',stored.receipt_id,'state',stored.state,
  'payload_digest',stored.business_digest,'provider_payload_digest',stored.provider_payload_digest,'scope','effect_intent','business_action_success',false);
end $function$;

CREATE OR REPLACE FUNCTION authz.nexloop_effect_intent_command_v0041(p_digest text, p_world text, p_claims text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare c jsonb:=p_claims::jsonb;p jsonb:=p_payload::jsonb;r authz.nexloop_run_credentials;link control.nexloop_effect_run_contexts;
 plan jsonb;outcome jsonb;added integer;k bytea;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_claims)>1048576 or octet_length(p_payload)>131072 then raise exception 'effect authority unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-intent-v1:'||p_claims,'UTF8'),k,'sha256'),'hex')
  or c->>'protocol' is distinct from 'nexloop-effect-intent-v1' or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp() or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or (authz.nexloop_effect_resource_slot(c->>'resource_id') is null or split_part(c->>'resource_id',':',4) is distinct from p->>'action_version')
 then raise exception 'effect authority unavailable' using errcode='42501';end if;
 -- Verify the original full signed chain before trusting any caller envelope or
 -- looking up an owner's linkage. Inner admission remains unchanged and repeats.
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 select * into r from authz.nexloop_run_credentials where token_digest=p_digest for share;
 select * into link from control.nexloop_effect_run_contexts where run_id=r.run_id and slot_identity=authz.nexloop_effect_resource_slot(c->>'resource_id');
 plan:=authz.nexloop_assert_effect_plan(link.context_id,c->>'tenant_id',p_world);
 outcome:=authz.nexloop_effect_intent_command_v0040(p_digest,p_world,p_claims,p_signature,p_payload);
 if p->>'verb'='submit' then
  insert into runtime.nexloop_effect_control_reservations values((outcome->>'intent_id')::uuid,c->>'tenant_id',plan->>'control_id',clock_timestamp()) on conflict do nothing;
  get diagnostics added=row_count;
  if added=1 then
   update control.nexloop_effect_control_ledger set reserved_units=reserved_units+1 where control_id=plan->>'control_id' and reserved_units<budget_units;
   if not found then raise exception 'shared effect budget exhausted' using errcode='42501';end if;
  end if;
 end if;
 perform authz.nexloop_assert_effect_plan(link.context_id,c->>'tenant_id',p_world);
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 return outcome;
end $function$;

CREATE OR REPLACE FUNCTION authz.nexloop_effect_intent_command_v0052(p_digest text, p_world text, p_claims text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
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
  or (authz.nexloop_effect_resource_slot(c->>'resource_id') is null or split_part(c->>'resource_id',':',4) is distinct from p->>'action_version')
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
   where rl.run_id=r.run_id and rl.slot_identity=authz.nexloop_effect_resource_slot(c->>'resource_id') and rl.valid_until>clock_timestamp()
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
end $function$;


-- 5. Run issuance and the effect catalog (M2.1): the message Run and the reply fallback Run carry the versions selected for the
--    Consumer (allowed_actions, source proofs, run credentials); the catalog accepts any version of an allow-listed effect Action and
--    compares an offering's delivery Action by stable name.
CREATE OR REPLACE FUNCTION authz.nexloop_message_run_issuance_command_before_takeover_v0049(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
 SET "TimeZone" TO 'UTC'
AS $function$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;v_verb text:=c->>'verb';v_tenant text:=a->>'tenant_id';
 v_principal text:=a->>'principal_id';v_key bytea;v_def control.nexloop_action_definitions;v_source_def control.nexloop_action_definitions;v_name text;
 v_lease runtime.nexloop_message_relay_leases;v_out runtime.nexloop_message_outbox;v_conv runtime.nexloop_conversations;
 v_route runtime.nexloop_message_routes;v_issued authz.nexloop_message_run_issuances;v_root jsonb;v_proof jsonb;
 v_assignment ontology.objects;v_goal ontology.objects;v_step ontology.objects;v_control ontology.objects;v_consumer ontology.objects;
 v_props jsonb;v_ctl control.nexloop_effect_control_ledger;v_proof2 jsonb;v_resources text[];v_seconds int;v_ts timestamptz;v_expiry timestamptz;
begin
 if v_tenant is null or v_principal is null or session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>262144
 or a->>'protocol' is distinct from 'nexloop-message-run-issuance-v1' or a->>'action_resource' is distinct from 'eios:action:nexloop.conversation.route:1'
 or a->>'operation' is distinct from 'execute' or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
 or v_verb is null or v_verb not in('claim','issue','own_route','release','lookup') then raise exception 'message relay unavailable' using errcode='42501';end if;
 select key_material into v_key from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-message-run-issuance-v1:'||p_text,'UTF8'),v_key,'sha256'),'hex')
 or a->>'expires_at' is null or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then raise exception 'message relay unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',v_tenant,true);
 if not exists(select 1 from authz.nexloop_service_credentials s where s.token_digest=p_digest and s.tenant_id=v_tenant and s.status='active' and s.binding->>'subject_kind'='service') then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_def from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'action_resource' and active for share;
 if not found or v_def.definition is distinct from a->'definition' or v_def.capability is distinct from a->'capability'
 or v_def.definition->'governance'->>'approval_mode' is distinct from 'none'
 or v_def.definition->'governance'->>'risk_level' is distinct from 'low'
 or jsonb_array_length(v_def.definition->'governance'->'policy_refs')<>0 then raise exception 'message relay unavailable' using errcode='42501';end if;
 if v_verb='claim' then
  if jsonb_typeof(c->'lease_seconds') is distinct from 'number' or c->>'lease_seconds'!~'^[0-9]{1,3}$'
   or c->>'consumer_id' is null or c->>'consumer_id'!~'^[a-f0-9]{64}$' then raise exception 'message relay unavailable' using errcode='42501';end if;
  v_seconds=(c->>'lease_seconds')::int;
  if v_seconds not between 3 and 300 then raise exception 'message relay unavailable' using errcode='42501';end if;
  -- Actual registered owner, canonical formal Consumer and Message/Conversation.
  select b.* into v_out from runtime.nexloop_message_outbox b join runtime.nexloop_conversations cv
   on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id
   join control.nexloop_consumer_owners ow on ow.tenant_id=cv.tenant_id and ow.world=cv.world and ow.principal_id=cv.principal_id and ow.consumer_id=cv.consumer_id
   join ontology.objects own on own.tenant_id=ow.tenant_id and own.world=ow.world and own.object_id=ow.ownership_id and own.type_name='ConsumerOwnership'
    and own.properties=jsonb_build_object('consumer_id',ow.consumer_id,'principal_id',ow.principal_id)
   join ontology.objects co on co.tenant_id=cv.tenant_id and co.world=cv.world and co.object_id=cv.consumer_id and co.type_name='Consumer'
   join ontology.objects formal on formal.tenant_id=cv.tenant_id and formal.world=cv.world and formal.object_id=cv.conversation_id and formal.type_name='Conversation'
    and formal.properties=jsonb_build_object('consumer_id',cv.consumer_id,'owner_principal',cv.principal_id)
   where b.tenant_id=v_tenant and b.world=p_world and b.status='pending' and cv.consumer_id=c->>'consumer_id'
    and not exists(select 1 from runtime.nexloop_message_relay_leases l where l.tenant_id=v_tenant and l.world=p_world and l.message_id=b.message_id and l.lease_until>clock_timestamp())
   order by b.conversation_id,b.sequence limit 1;
  if not found then perform authz.nexloop_assert_action_authority(p_digest,p_world,a);return null;end if;
  if not pg_try_advisory_xact_lock(hashtextextended(v_tenant||':'||p_world||':message-route:'||v_out.message_id,0)) then return null;end if;
  select * into v_route from runtime.nexloop_message_routes where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id for update;
  select * into v_out from runtime.nexloop_message_outbox where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id for update;
  if not found or v_out.status is distinct from 'pending' then perform authz.nexloop_assert_action_authority(p_digest,p_world,a);return null;end if;
  perform 1 from runtime.nexloop_message_relay_leases l where l.tenant_id=v_tenant and l.world=p_world and l.message_id=v_out.message_id and l.lease_until>clock_timestamp();
  if found then perform authz.nexloop_assert_action_authority(p_digest,p_world,a);return null;end if;
  insert into runtime.nexloop_message_relay_leases values(v_tenant,p_world,v_out.message_id,c->>'consumer_id',v_principal,p_digest,1,clock_timestamp()+make_interval(secs=>v_seconds))
   on conflict(tenant_id,world,message_id) do update set principal_id=excluded.principal_id,credential_digest=excluded.credential_digest,
    fence=runtime.nexloop_message_relay_leases.fence+1,lease_until=excluded.lease_until returning * into v_lease;
 else
  if c->>'message_id' is null or c->>'message_id'!~'^[a-f0-9]{64}$' then raise exception 'message relay unavailable' using errcode='42501';end if;
  perform pg_advisory_xact_lock(hashtextextended(v_tenant||':'||p_world||':message-route:'||(c->>'message_id'),0));
  select * into v_route from runtime.nexloop_message_routes where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id' for update;
  select * into v_out from runtime.nexloop_message_outbox where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id' for update;
  if not found then raise exception 'message relay unavailable' using errcode='42501';end if;
  select * into v_lease from runtime.nexloop_message_relay_leases where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id' for update;
  if not found or v_lease.principal_id is distinct from v_principal or v_lease.credential_digest is distinct from p_digest
   or jsonb_typeof(c->'fence') is distinct from 'number' or c->>'fence'!~'^[0-9]{1,18}$' or v_lease.fence<>(c->>'fence')::bigint or v_lease.lease_until<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
  select * into v_out from runtime.nexloop_message_outbox where tenant_id=v_tenant and world=p_world and message_id=v_lease.message_id for update;
  if not found then raise exception 'message relay unavailable' using errcode='42501';end if;
 end if;
 select * into v_conv from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and conversation_id=v_out.conversation_id for share;
 if not found or v_conv.consumer_id is distinct from v_lease.consumer_id then raise exception 'message relay unavailable' using errcode='42501';end if;
 -- Canonical formal Message must correspond exactly to committed outbox data.
 if not exists(select 1 from ontology.objects mo where mo.tenant_id=v_tenant and mo.world=p_world and mo.object_id=v_out.message_id and mo.type_name='Message'
  and mo.properties=jsonb_build_object('conversation_id',v_out.conversation_id,'sequence',v_out.sequence,'actor',v_out.record->>'actor','body',v_out.record->>'body','accepted_at',v_out.record->>'accepted_at')) then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_route from runtime.nexloop_message_routes where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id for update;
 if found and v_route.bound_by is distinct from v_principal then raise exception 'message relay unavailable' using errcode='42501';end if;
 if v_verb in('claim','own_route') then
  if v_route.message_id is not null then
   if v_route.lease_until>clock_timestamp() then
    if v_verb is distinct from 'own_route' or v_route.lease_credential is distinct from p_digest or v_route.lease_until is distinct from v_lease.lease_until then
     raise exception 'message relay unavailable' using errcode='42501';end if;
   else
   update runtime.nexloop_message_routes set fence=fence+1,lease_credential=p_digest,lease_until=v_lease.lease_until
    where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id returning * into v_route;
   end if;
  elsif v_verb='own_route' then raise exception 'message relay unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  if v_lease.lease_until<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
  return jsonb_build_object('message_id',v_out.message_id,'tenant_id',v_tenant,'consumer_id',v_conv.consumer_id,'fence',v_lease.fence,
   'route_fence',v_route.fence,'source_event_id',v_out.event_id,'body',v_out.record->>'body');
 elsif v_verb='release' then
  if v_out.status is distinct from 'delivered' or v_route.task_id is null then raise exception 'message relay unavailable' using errcode='42501';end if;
  update runtime.nexloop_message_relay_leases set lease_until=clock_timestamp() where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  return jsonb_build_object('status','queued');
 end if;
 select * into v_issued from authz.nexloop_message_run_issuances where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id for update;
 if v_verb='lookup' then
  perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  if v_issued.message_id is null then return null;end if;
  return jsonb_build_object('run_id',v_issued.run_id,'request_id',v_issued.request_id,'expires_at',v_issued.expires_at,
   'state',case when v_issued.expires_at<=clock_timestamp() then 'requires_governed_replan' else 'issued' end);
 end if;
 if jsonb_typeof(c->'run_id') is distinct from 'string' or c->>'run_id'!~'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
  or jsonb_typeof(c->'source_digest') is distinct from 'string' or c->>'source_digest'!~'^[a-f0-9]{64}$'
  or jsonb_typeof(c->'request_id') is distinct from 'string' or c->>'assignment_id' is null or c->>'assignment_id'!~'^[a-f0-9]{64}$' or jsonb_typeof(c->'assignment_revision') is distinct from 'number' or c->>'assignment_revision' is distinct from '1'
  or c->>'assignment_digest' is null or c->>'assignment_digest'!~'^[a-f0-9]{64}$' or c->>'token_digest' is null or c->>'token_digest'!~'^[a-f0-9]{64}$'
  or c->>'issuance_nonce' is null or c->>'issuance_nonce'!~'^[a-f0-9]{64}$' or jsonb_typeof(c->'ttl_seconds') is distinct from 'number' or c->>'ttl_seconds'!~'^[0-9]{1,3}$'
  or jsonb_typeof(c->'source_proofs') is distinct from 'array' or jsonb_array_length(c->'source_proofs') not in (1,2) then raise exception 'message relay unavailable' using errcode='42501';end if;
 v_seconds=(c->>'ttl_seconds')::int;
 if v_seconds not between 1 and 300 then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_assignment from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=c->>'assignment_id' and type_name='MessageAssignment' for share;
 if not found or v_assignment.nexloop_revision<>(c->>'assignment_revision')::bigint then raise exception 'message relay unavailable' using errcode='42501';end if;
 v_props=v_assignment.properties;
 if jsonb_typeof(v_props) is distinct from 'object' or (select count(*) from jsonb_object_keys(v_props))<>15
  or not(v_props ?& array['message_id','consumer_id','goal_id','goal_revision','step_id','step_revision','control_id','control_revision','consumer_revision','source_principal','executor_principal','recipe_digest','allowed_actions','state','valid_until']) then raise exception 'message relay unavailable' using errcode='42501';end if;
 for v_name in select unnest(array['message_id','consumer_id','goal_id','step_id','control_id','source_principal','executor_principal','recipe_digest','state','valid_until']) loop
  if jsonb_typeof(v_props->v_name) is distinct from 'string' or length(v_props->>v_name) not between 1 and 512 then raise exception 'message relay unavailable' using errcode='42501';end if;
 end loop;
 for v_name in select unnest(array['goal_revision','step_revision','control_revision','consumer_revision']) loop
  if jsonb_typeof(v_props->v_name) is distinct from 'number' or v_props->>v_name!~'^[1-9][0-9]{0,17}$' then raise exception 'message relay unavailable' using errcode='42501';end if;
 end loop;
 for v_name in select unnest(array['message_id','consumer_id','goal_id','step_id','control_id','recipe_digest']) loop
  if v_props->>v_name!~'^[a-f0-9]{64}$' then raise exception 'message relay unavailable' using errcode='42501';end if;
 end loop;
 if jsonb_typeof(c->'assignment_text') is distinct from 'string' or (c->>'assignment_text')::jsonb is distinct from v_props
  or c->>'assignment_digest' is distinct from encode(sha256(convert_to(c->>'assignment_text','UTF8')),'hex')
  or c->>'request_id' is distinct from 'message-'||encode(sha256(convert_to('["'||v_tenant||'","real","'||v_out.message_id||'"]','UTF8')),'hex') then raise exception 'message relay unavailable' using errcode='42501';end if;
 if v_props->>'message_id' is distinct from v_out.message_id or v_props->>'consumer_id' is distinct from v_conv.consumer_id or v_props->>'state' is distinct from 'active'
 or jsonb_typeof(v_props->'allowed_actions') is distinct from 'array' or jsonb_array_length(v_props->'allowed_actions') not between 1 and 2
 or jsonb_array_length(c->'source_proofs')<>jsonb_array_length(v_props->'allowed_actions') or (v_props->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_consumer from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_conv.consumer_id and type_name='Consumer' for share;
 if not found or v_consumer.nexloop_revision<>(v_props->>'consumer_revision')::bigint then raise exception 'message relay unavailable' using errcode='42501';end if;
 if v_props->'allowed_actions' not in (jsonb_build_array(authz.nexloop_effect_bind_action_resource(v_tenant,p_world,'service.request:primary',v_consumer.schema_version)),
   jsonb_build_array(authz.nexloop_effect_bind_action_resource(v_tenant,p_world,'service.request:primary',v_consumer.schema_version),authz.nexloop_effect_bind_action_resource(v_tenant,p_world,'enterprise.deliver:primary',v_consumer.schema_version))) then
  raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_goal from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_props->>'goal_id' and type_name='Goal' for share;
 if not found or jsonb_typeof(v_goal.properties->'valid_until') is distinct from 'string' or jsonb_typeof(v_goal.properties->'consumer_id') is distinct from 'string' or v_goal.nexloop_revision<>(v_props->>'goal_revision')::bigint or v_goal.properties->>'consumer_id' is distinct from v_conv.consumer_id or v_goal.properties->>'state' is distinct from 'active' or (v_goal.properties->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_step from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_props->>'step_id' and type_name='PlanStep' for share;
 if not found or v_step.nexloop_revision<>(v_props->>'step_revision')::bigint or v_step.properties->>'consumer_id' is distinct from v_conv.consumer_id
 or v_step.properties->>'goal_id' is distinct from v_goal.object_id or v_step.properties->>'control_id' is distinct from v_props->>'control_id'
 or v_step.properties->>'state' is distinct from 'ready' or v_step.properties->>'action_name' is distinct from 'nexloop.service.request'
 or v_step.properties->'submitter_principals' is distinct from jsonb_build_array(v_props->>'source_principal') then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_control from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_props->>'control_id' and type_name='EffectControl' for share;
 if not found or jsonb_typeof(v_control.properties->'valid_until') is distinct from 'string' or jsonb_typeof(v_control.properties->'executor_principal') is distinct from 'string' or v_control.nexloop_revision<>(v_props->>'control_revision')::bigint or v_control.properties->>'consumer_id' is distinct from v_conv.consumer_id or v_control.properties->'allow_effect' is distinct from 'true'::jsonb
 or v_control.properties->>'executor_principal' is distinct from v_props->>'executor_principal' or (v_control.properties->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_ctl from control.nexloop_effect_control_ledger where tenant_id=v_tenant and world=p_world and control_id=v_control.object_id for share;
 if not found or v_ctl.valid_until is null or v_ctl.control_revision is null or v_ctl.consumer_id is distinct from v_conv.consumer_id or v_ctl.control_revision<>v_control.nexloop_revision or v_ctl.executor_principal is distinct from v_props->>'executor_principal' or v_ctl.valid_until<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
 perform 1 from authz.nexloop_service_credentials where token_digest=c->>'source_digest' for share;
 if not found then raise exception 'message relay unavailable' using errcode='42501';end if;
 v_root=authz.nexloop_root_identity_snapshot(c->>'source_digest',p_world);
 if jsonb_typeof(v_root->'expires_at') is distinct from 'string' or v_root->>'directory_hash' is null or v_root->'binding'->>'tenant_id' is distinct from v_tenant or v_root->'binding'->>'subject_principal_id' is distinct from v_props->>'source_principal' then raise exception 'message relay unavailable' using errcode='42501';end if;
 select * into v_source_def from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=v_props->'allowed_actions'->>0 and active for share;
 if not found or v_source_def.definition is distinct from c->'source_definition' or v_source_def.capability is distinct from c->'source_capability' then raise exception 'message relay unavailable' using errcode='42501';end if;
 v_proof=c->'source_proofs'->0;
 if v_proof->>'resource_id' is distinct from v_props->'allowed_actions'->>0 then raise exception 'message relay unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(c->>'source_digest',p_world,v_proof);
 -- ADR-030: the enterprise slot needs the Source's own proof for the enterprise Action.
 select array_agg(x order by o) into v_resources from jsonb_array_elements_text(v_props->'allowed_actions') with ordinality t(x,o);
 if jsonb_array_length(v_props->'allowed_actions')=2 then
  v_proof2=c->'source_proofs'->1;
  if v_proof2->>'resource_id' is distinct from v_props->'allowed_actions'->>1 then raise exception 'message relay unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_action_authority(c->>'source_digest',p_world,v_proof2);
 end if;
 if v_issued.message_id is not null then
  if v_issued.run_id is distinct from (c->>'run_id')::uuid or v_issued.run_digest is distinct from c->>'token_digest' or v_issued.request_id is distinct from c->>'request_id'
   or v_issued.issuance_nonce is distinct from c->>'issuance_nonce' or v_issued.assignment_id is distinct from v_assignment.object_id or v_issued.assignment_digest is distinct from c->>'assignment_digest'
   or v_issued.issuer_principal is distinct from v_principal
   or not exists(select 1 from authz.nexloop_run_credentials r where r.run_id=v_issued.run_id and r.source_digest=c->>'source_digest' and r.source_directory_hash=v_root->>'directory_hash' and r.allowed_resources=v_resources) then raise exception 'message relay conflict' using errcode='23505';end if;
 else
  v_ts=clock_timestamp();v_expiry=least((v_root->>'expires_at')::timestamptz,v_ts+make_interval(secs=>v_seconds),(v_props->>'valid_until')::timestamptz,(v_goal.properties->>'valid_until')::timestamptz,v_ctl.valid_until);
  insert into authz.nexloop_run_credentials values(c->>'token_digest',(c->>'run_id')::uuid,c->>'source_digest',v_root->>'directory_hash',p_world,'nexloop-agent-host',v_resources,'active',v_ts,v_expiry);
  insert into authz.nexloop_message_run_issuances values(v_tenant,p_world,v_out.message_id,(c->>'run_id')::uuid,c->>'request_id',c->>'issuance_nonce',v_assignment.object_id,v_assignment.nexloop_revision,c->>'assignment_digest',v_principal,c->>'token_digest',v_ts,v_expiry) returning * into v_issued;
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform authz.nexloop_assert_action_authority(c->>'source_digest',p_world,v_proof);
 if v_proof2 is not null then perform authz.nexloop_assert_action_authority(c->>'source_digest',p_world,v_proof2);end if;
 if v_lease.lease_until<=clock_timestamp() or v_ctl.valid_until<=clock_timestamp() or (v_props->>'valid_until')::timestamptz<=clock_timestamp() or (v_goal.properties->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'message relay unavailable' using errcode='42501';end if;
 return jsonb_build_object('run_id',v_issued.run_id,'request_id',v_issued.request_id,'expires_at',v_issued.expires_at,
  'state',case when v_issued.expires_at<=clock_timestamp() then 'requires_governed_replan' else 'issued' end);
end $function$;

CREATE OR REPLACE FUNCTION authz.nexloop_message_run_issuance_command_before_relay_handoff_v0190(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
 SET "TimeZone" TO 'UTC'
AS $function$
declare c jsonb:=p_payload::jsonb;v_verb text:=c->>'verb';v_tenant text:=p_text::jsonb->>'tenant_id';v_principal text:=p_text::jsonb->>'principal_id';
 v_result jsonb;v_consumer text;v_claim runtime.nexloop_relay_claims;e control.nexloop_relay_enrollments;
begin
 if v_verb in ('claim_next','profile') then return authz.nexloop_relay_multi_command(p_digest,p_world,p_text,p_signature,p_payload);end if;
 v_result:=authz.nexloop_message_run_issuance_command_before_relay_multi_v0160(p_digest,p_world,p_text,p_signature,p_payload);
 if v_verb='issue' and exists(select 1 from jsonb_array_elements_text((c->>'assignment_text')::jsonb->'allowed_actions') x where authz.nexloop_effect_resource_slot(x)='enterprise.deliver:primary') then
  perform set_config('eios.tenant_id',v_tenant,true);
  select * into v_claim from runtime.nexloop_relay_claims where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id';
  if not found then raise exception 'enterprise slot unavailable' using errcode='42501';end if;
  e:=control.nexloop_relay_enrollment_current(v_tenant,p_world,v_claim.consumer_id);
  if control.nexloop_relay_enterprise_problem(v_tenant,p_world,v_claim.consumer_id,e) is not null then
   raise exception 'enterprise slot unavailable' using errcode='42501';end if;
 end if;
 if v_verb='issue' then
  -- After the existing checks (authenticated by now); a refusal rolls the whole issuance back.
  perform set_config('eios.tenant_id',v_tenant,true);
  select cv.consumer_id into v_consumer from runtime.nexloop_message_outbox b join runtime.nexloop_conversations cv
   on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id where b.tenant_id=v_tenant and b.world=p_world and b.message_id=c->>'message_id';
  if runtime.nexloop_consumer_erasing(v_tenant,p_world,v_consumer) then raise exception 'consumer erasing: no message Run' using errcode='42501';end if;
  select * into v_claim from runtime.nexloop_relay_claims where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id';
  if found then
   e:=control.nexloop_relay_enrollment_current(v_tenant,p_world,v_claim.consumer_id);
   perform set_config('eios.tenant_id',v_tenant,true);
   if e.state is distinct from 'active' or e.enrollment_revision is distinct from v_claim.enrollment_revision or e.relay_principal is distinct from v_principal
    or v_claim.consumer_id is distinct from v_consumer or (c->>'assignment_text')::jsonb->>'control_id' is distinct from e.control_id then
    raise exception 'relay enrollment changed: no message Run' using errcode='42501';end if;
  end if;
 end if;
 return v_result;
end $function$;

CREATE OR REPLACE FUNCTION authz.nexloop_reply_fallback_command_before_takeover_v0110(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_tenant text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-reply-fallback-v1','eios:action:nexloop.reply.fallback:1',array['nexloop_api']);
 c jsonb:=p_payload::jsonb;a jsonb:=p_text::jsonb;v_principal text:=a->>'principal_id';v_out runtime.nexloop_message_outbox;v_conv runtime.nexloop_conversations;
 v_issued authz.nexloop_message_fallback_issuances;v_item runtime.nexloop_work_feed;v_assignment ontology.objects;v_goal ontology.objects;v_step ontology.objects;
 v_control ontology.objects;v_consumer ontology.objects;v_ctl control.nexloop_effect_control_ledger;v_source_def control.nexloop_action_definitions;
 v_props jsonb;v_root jsonb;v_proof jsonb;v_name text;v_seconds int;v_ts timestamptz;v_expiry timestamptz;
begin
 perform set_config('eios.tenant_id',v_tenant,true);
 if c->>'message_id' is null or c->>'message_id'!~'^[a-f0-9]{64}$' or c->>'verb' not in ('issue','lookup') then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_out from runtime.nexloop_message_outbox where tenant_id=v_tenant and world=p_world and message_id=c->>'message_id' for share;
 if not found then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_conv from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and conversation_id=v_out.conversation_id for share;
 if not found then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 -- Serializes against settlement (it deletes this row when a bound reply is accepted) and against a second issuance.
 select * into v_item from runtime.nexloop_work_feed where tenant_id=v_tenant and world=p_world and feed='reply-due' and item_key='reply:'||v_out.message_id for update;
 select * into v_issued from authz.nexloop_message_fallback_issuances where tenant_id=v_tenant and world=p_world and message_id=v_out.message_id for update;
 if c->>'verb'='lookup' then
  if v_issued.message_id is null then return null;end if;
  return jsonb_build_object('run_id',v_issued.run_id,'request_id',v_issued.request_id,'expires_at',v_issued.expires_at);
 end if;
 if v_issued.message_id is null then
  if v_item.item_key is null or v_item.available_at>clock_timestamp() then raise exception 'reply fallback not due' using errcode='40001';end if;
  if exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=v_tenant and o.world=p_world and o.trigger_message_id=v_out.message_id
    and o.delivery_state in ('provider_accepted','delivered')) then raise exception 'reply already settled' using errcode='40001';end if;
 end if;
 -- Same Run-issuance inputs and MessageAssignment checks as 0049 'issue'.
 if jsonb_typeof(c->'run_id') is distinct from 'string' or c->>'run_id'!~'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
  or c->>'source_digest' is null or c->>'source_digest'!~'^[a-f0-9]{64}$' or c->>'assignment_id' is null or c->>'assignment_id'!~'^[a-f0-9]{64}$'
  or c->>'assignment_revision' is distinct from '1' or c->>'assignment_digest' is null or c->>'assignment_digest'!~'^[a-f0-9]{64}$'
  or c->>'token_digest' is null or c->>'token_digest'!~'^[a-f0-9]{64}$' or c->>'issuance_nonce' is null or c->>'issuance_nonce'!~'^[a-f0-9]{64}$'
  or jsonb_typeof(c->'ttl_seconds') is distinct from 'number' or c->>'ttl_seconds'!~'^[0-9]{1,3}$'
  or jsonb_typeof(c->'source_proofs') is distinct from 'array' or jsonb_array_length(c->'source_proofs')<>1
  or c->>'request_id' is distinct from 'reply-fallback-'||encode(sha256(convert_to('["'||v_tenant||'","real","'||v_out.message_id||'"]','UTF8')),'hex') then
  raise exception 'reply fallback unavailable' using errcode='42501';end if;
 v_seconds=(c->>'ttl_seconds')::int;
 if v_seconds not between 1 and 300 then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_assignment from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=c->>'assignment_id' and type_name='MessageAssignment' for share;
 if not found or v_assignment.nexloop_revision<>1 then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 v_props=v_assignment.properties;
 if jsonb_typeof(v_props) is distinct from 'object' or (select count(*) from jsonb_object_keys(v_props))<>15
  or not(v_props ?& array['message_id','consumer_id','goal_id','goal_revision','step_id','step_revision','control_id','control_revision','consumer_revision','source_principal','executor_principal','recipe_digest','allowed_actions','state','valid_until']) then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 for v_name in select unnest(array['message_id','consumer_id','goal_id','step_id','control_id','recipe_digest']) loop
  if jsonb_typeof(v_props->v_name) is distinct from 'string' or v_props->>v_name!~'^[a-f0-9]{64}$' then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 end loop;
 for v_name in select unnest(array['goal_revision','step_revision','control_revision','consumer_revision']) loop
  if jsonb_typeof(v_props->v_name) is distinct from 'number' or v_props->>v_name!~'^[1-9][0-9]{0,17}$' then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 end loop;
 if jsonb_typeof(c->'assignment_text') is distinct from 'string' or (c->>'assignment_text')::jsonb is distinct from v_props
  or c->>'assignment_digest' is distinct from encode(sha256(convert_to(c->>'assignment_text','UTF8')),'hex')
  or v_props->>'message_id' is distinct from v_out.message_id or v_props->>'consumer_id' is distinct from v_conv.consumer_id or v_props->>'state' is distinct from 'active'
  or jsonb_typeof(v_props->'allowed_actions') is distinct from 'array' or jsonb_array_length(v_props->'allowed_actions')<>1 or (v_props->>'valid_until')::timestamptz<=clock_timestamp()
  or v_props->>'source_principal' is distinct from authz.nexloop_root_identity_snapshot(c->>'source_digest',p_world)->'binding'->>'subject_principal_id' then
  raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_consumer from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_conv.consumer_id and type_name='Consumer' for share;
 if not found or v_consumer.nexloop_revision<>(v_props->>'consumer_revision')::bigint then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 if v_props->'allowed_actions' is distinct from jsonb_build_array(authz.nexloop_effect_bind_action_resource(v_tenant,p_world,'service.request:primary',v_consumer.schema_version)) then
  raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_goal from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_props->>'goal_id' and type_name='Goal' for share;
 if not found or v_goal.nexloop_revision<>(v_props->>'goal_revision')::bigint or v_goal.properties->>'consumer_id' is distinct from v_conv.consumer_id
  or v_goal.properties->>'state' is distinct from 'active' or (v_goal.properties->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_step from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_props->>'step_id' and type_name='PlanStep' for share;
 if not found or v_step.nexloop_revision<>(v_props->>'step_revision')::bigint or v_step.properties->>'consumer_id' is distinct from v_conv.consumer_id
  or v_step.properties->>'goal_id' is distinct from v_goal.object_id or v_step.properties->>'control_id' is distinct from v_props->>'control_id'
  or v_step.properties->>'state' is distinct from 'ready' or v_step.properties->>'action_name' is distinct from 'nexloop.service.request'
  or v_step.properties->'submitter_principals' is distinct from jsonb_build_array(v_props->>'source_principal') then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_control from ontology.objects where tenant_id=v_tenant and world=p_world and object_id=v_props->>'control_id' and type_name='EffectControl' for share;
 if not found or v_control.nexloop_revision<>(v_props->>'control_revision')::bigint or v_control.properties->>'consumer_id' is distinct from v_conv.consumer_id
  or v_control.properties->'allow_effect' is distinct from 'true'::jsonb or v_control.properties->>'executor_principal' is distinct from v_props->>'executor_principal'
  or (v_control.properties->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_ctl from control.nexloop_effect_control_ledger where tenant_id=v_tenant and world=p_world and control_id=v_control.object_id for share;
 if not found or v_ctl.valid_until is null or v_ctl.consumer_id is distinct from v_conv.consumer_id or v_ctl.control_revision<>v_control.nexloop_revision
  or v_ctl.executor_principal is distinct from v_props->>'executor_principal' or v_ctl.valid_until<=clock_timestamp() then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 perform 1 from authz.nexloop_service_credentials where token_digest=c->>'source_digest' for share;
 if not found then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 v_root=authz.nexloop_root_identity_snapshot(c->>'source_digest',p_world);
 if jsonb_typeof(v_root->'expires_at') is distinct from 'string' or v_root->>'directory_hash' is null or v_root->'binding'->>'tenant_id' is distinct from v_tenant then
  raise exception 'reply fallback unavailable' using errcode='42501';end if;
 select * into v_source_def from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=v_props->'allowed_actions'->>0 and active for share;
 if not found or v_source_def.definition is distinct from c->'source_definition' or v_source_def.capability is distinct from c->'source_capability' then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 v_proof=c->'source_proofs'->0;
 if v_proof->>'resource_id' is distinct from v_props->'allowed_actions'->>0 then raise exception 'reply fallback unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(c->>'source_digest',p_world,v_proof);
 if v_issued.message_id is not null then
  -- Replay of the same issuance only; never a second fallback Run for this message.
  if v_issued.run_id is distinct from (c->>'run_id')::uuid or v_issued.run_digest is distinct from c->>'token_digest' or v_issued.issuance_nonce is distinct from c->>'issuance_nonce'
   or v_issued.assignment_id is distinct from v_assignment.object_id or v_issued.assignment_digest is distinct from c->>'assignment_digest' or v_issued.issuer_principal is distinct from v_principal then
   raise exception 'reply fallback already issued' using errcode='23505';end if;
 else
  v_ts=clock_timestamp();v_expiry=least((v_root->>'expires_at')::timestamptz,v_ts+make_interval(secs=>v_seconds),(v_props->>'valid_until')::timestamptz,(v_goal.properties->>'valid_until')::timestamptz,v_ctl.valid_until);
  insert into authz.nexloop_run_credentials values(c->>'token_digest',(c->>'run_id')::uuid,c->>'source_digest',v_root->>'directory_hash',p_world,'nexloop-agent-host',array[v_props->'allowed_actions'->>0],'active',v_ts,v_expiry);
  insert into authz.nexloop_message_fallback_issuances values(v_tenant,p_world,v_out.message_id,(c->>'run_id')::uuid,c->>'request_id',c->>'issuance_nonce',v_assignment.object_id,
   v_assignment.nexloop_revision,c->>'assignment_digest',v_principal,c->>'token_digest',v_ts,v_expiry) returning * into v_issued;
 end if;
 perform authz.nexloop_assert_action_authority(c->>'source_digest',p_world,v_proof);
 return jsonb_build_object('run_id',v_issued.run_id,'request_id',v_issued.request_id,'expires_at',v_issued.expires_at,'replay',v_issued.issued_at<transaction_timestamp());
end $$;

CREATE OR REPLACE FUNCTION authz.nexloop_effect_catalog_metadata_v0401(p_run uuid, p_tenant text, p_world text, p_resource text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare r authz.nexloop_run_credentials;link control.nexloop_effect_run_contexts;reply_link control.nexloop_effect_run_contexts;item jsonb;ctx control.nexloop_effect_contexts;
 b runtime.nexloop_context_artifact_bindings;identity jsonb;candidate ontology.objects;offered ontology.objects;n integer:=0;supply jsonb;
begin
 if p_run is null or p_tenant is null or p_world is distinct from 'real'
  or authz.nexloop_effect_resource_slot(p_resource) is null then raise exception 'effect catalog unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from authz.nexloop_run_credentials rc where rc.run_id=p_run and rc.world=p_world;
 if not found or r.status is distinct from 'active' or r.expires_at<=clock_timestamp() then raise exception 'effect catalog Run unavailable' using errcode='42501';end if;
 identity:=authz.nexloop_service_identity_snapshot(r.source_digest,p_world);
 if identity->'binding'->>'tenant_id' is distinct from p_tenant or identity->'run_context' is distinct from 'null'::jsonb then raise exception 'effect catalog Source unavailable' using errcode='42501';end if;
 select * into link from control.nexloop_effect_run_contexts er where er.run_id=r.run_id and er.slot_identity=authz.nexloop_effect_resource_slot(p_resource);
 if not found or link.valid_until<=clock_timestamp() then raise exception 'effect catalog Plan unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_effect_plan(link.context_id,p_tenant,p_world);
 select * into ctx from control.nexloop_effect_contexts ec where ec.context_id=link.context_id and ec.tenant_id=p_tenant and ec.world=p_world;
 if not found then raise exception 'effect catalog Plan unavailable' using errcode='42501';end if;
 if authz.nexloop_is_message_run(r.run_id) then
  select * into b from runtime.nexloop_run_binding(p_tenant,p_world,r.run_id,false);
  -- The Run's context artifact is bound to its reply context, whichever Action is claimed.
  select * into reply_link from control.nexloop_effect_run_contexts er where er.run_id=r.run_id and er.slot_identity='service.request:primary';
  if not found or b.source_digest is distinct from r.source_digest or b.context_id is distinct from reply_link.context_id
   or (b.pack_text::jsonb->>'schema_version' is null or b.pack_text::jsonb->>'schema_version' not in ('nexloop.context-pack.v2','nexloop.context-pack.v4','nexloop.context-pack.v6'))
   or substr(b.command_binding->>'consumer_ref',10) is distinct from ctx.consumer_id then raise exception 'effect catalog mandatory' using errcode='42501';end if;
  if authz.nexloop_effect_resource_slot(p_resource)='service.request:primary' then
   supply:=b.pack_text::jsonb->'supply';
  else
   select e.value into item from jsonb_array_elements(coalesce(b.pack_text::jsonb->'effect_supplies','[]'::jsonb)) e where split_part('eios:action:'||(e.value->>'action'),':',3)=split_part(p_resource,':',3);
   if item is null then raise exception 'effect catalog mandatory' using errcode='42501';end if;
   supply:=item-'action';
  end if;
 else
  -- No guessed default or silent first row: multiple governed grants require
  -- explicit future selection; this minimal entry remains closed on ambiguity.
  for candidate in select o.* from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world and o.type_name='ConsumerServiceOffering'
   and o.properties->>'consumer_id'=ctx.consumer_id and o.properties->>'source_principal'=identity->'binding'->>'subject_principal_id'
   and o.properties->'active'='true'::jsonb
   and exists(select 1 from ontology.objects so where so.object_id=o.properties->>'offering_id' and so.tenant_id=p_tenant and so.world=p_world
    and so.type_name='ServiceOffering' and split_part('eios:action:'||(so.properties->>'delivery_action'),':',3)=split_part(p_resource,':',3))
   order by o.object_id limit 2 loop n:=n+1;end loop;
  if n<>1 then raise exception 'effect catalog registration unavailable' using errcode='42501';end if;
  select * into offered from ontology.objects o where o.object_id=candidate.properties->>'offering_id' and o.tenant_id=p_tenant and o.world=p_world and o.type_name='ServiceOffering';
  if not found then raise exception 'effect catalog unavailable' using errcode='42501';end if;
  supply:=jsonb_build_object('offering_id',offered.object_id,'offering_revision',offered.nexloop_revision,'binding_id',candidate.object_id,
   'binding_revision',candidate.nexloop_revision,'provenance','eios:object:'||offered.object_id,'properties',offered.properties);
 end if;
 return jsonb_build_object('_source_digest',r.source_digest,'supply',supply,'consumer_id',ctx.consumer_id);
end $function$;

CREATE OR REPLACE FUNCTION authz.nexloop_effect_catalog_hint(p_digest text, p_world text, p_text text, p_signature text, p_payload text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET row_security TO 'on'
AS $function$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;r authz.nexloop_run_credentials;b runtime.nexloop_context_artifact_bindings;identity jsonb;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker','nexloop_action_worker') or p_text is null or p_payload is null
  or octet_length(p_text)>1048576 or octet_length(p_payload)>131072
  or c->>'protocol' is distinct from 'nexloop-effect-intent-v1' or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp() or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or authz.nexloop_effect_resource_slot(c->>'action_resource') is null or c->>'action_resource' is distinct from c->>'resource_id' then raise exception 'effect catalog unavailable' using errcode='42501';end if;
 select sk.key_material into k from authz.nexloop_authority_signing_keys sk where sk.key_id=c->>'key_id' and sk.active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-effect-intent-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then raise exception 'effect catalog unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if identity->'run_context' is distinct from 'null'::jsonb and identity->'run_context' is not null then
  select * into r from authz.nexloop_run_credentials rc where rc.token_digest=p_digest and rc.world=p_world;
 else raise exception 'effect catalog Run required' using errcode='42501';end if;
 if r.run_id is null then raise exception 'effect catalog Run required' using errcode='42501';end if;
 identity:=authz.nexloop_effect_catalog_metadata_v0401(r.run_id,c->>'tenant_id',p_world,c->>'action_resource');
 if split_part('eios:action:'||(identity->'supply'->'properties'->>'delivery_action'),':',3) is distinct from split_part(c->>'action_resource',':',3) then
  raise exception 'outside catalog terms' using errcode='22023';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if (c->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'effect catalog expired' using errcode='42501';end if;
 return identity;
end $function$;

CREATE OR REPLACE FUNCTION authz.nexloop_local_delivery_authority(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;ident jsonb;tenant text;principal text;
 verb text:=p->>'verb';target text;terminal_read boolean:=false;i runtime.nexloop_effect_intents;o runtime.nexloop_effect_outbox;
 a runtime.nexloop_effect_attempts;r authz.nexloop_run_credentials;ac runtime.nexloop_action_claims;
 pb runtime.nexloop_effect_provider_bindings;published control.nexloop_action_definitions;plan jsonb;result jsonb;source_identity jsonb;
 link control.nexloop_effect_run_contexts;ctx control.nexloop_effect_contexts;consumer ontology.objects;binding control.nexloop_effect_plan_bindings;reserved jsonb;command jsonb;claim_wire jsonb;
begin
 if session_user<>'nexloop_action_worker' or p_world is distinct from 'real' or p_text is null or p_payload is null
  or octet_length(p_text)>1048576 or octet_length(p_payload)>524288
  or jsonb_typeof(c) is distinct from 'object' or jsonb_typeof(p) is distinct from 'object'
  or verb is null or verb not in ('resolve','deliver','query','recover_claim','recover_reserve','recover_deliver')
  or c->>'protocol' is distinct from 'nexloop-local-json-delivery-v1'
  or c->>'operation' is distinct from 'execute'
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp()
  or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or jsonb_typeof(p->'intent_id') is distinct from 'string'
  or jsonb_typeof(p->'provider_profile_digest') is distinct from 'string'
  or p->>'provider_profile_digest' !~ '^[a-f0-9]{64}$'
 then raise exception 'local delivery denied' using errcode='42501';end if;
 select sk.key_material into k from authz.nexloop_authority_signing_keys sk where sk.key_id=c->>'key_id' and sk.active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-local-json-delivery-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
 then raise exception 'local delivery denied' using errcode='42501';end if;
 target:=case when verb in ('deliver','recover_reserve','recover_deliver') then c->>'resource_id' else 'eios:action:nexloop.service.query:1' end;
 if verb in ('deliver','recover_reserve','recover_deliver') and authz.nexloop_effect_resource_slot(target) is distinct from 'service.request:primary' then
  raise exception 'local delivery denied' using errcode='42501';end if;
 if c->>'resource_id' is distinct from target or c->>'action_resource' is distinct from target then raise exception 'local delivery denied' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb or ident->'binding'->>'subject_kind' is distinct from 'service'
 then raise exception 'local delivery denied' using errcode='42501';end if;
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';
 select d.* into published from control.nexloop_action_definitions d where d.tenant_id=tenant and d.world=p_world and d.resource_id=target and d.active for share;
 if not found or published.definition is distinct from c->'definition' or published.capability is distinct from c->'capability'
  or published.definition->'governance'->>'approval_mode' is distinct from 'none'
  or published.definition->'governance'->>'risk_level' is distinct from 'low'
  or jsonb_array_length(published.definition->'governance'->'policy_refs') is distinct from 0
  or jsonb_array_length(coalesce(published.definition->'preconditions','[]'::jsonb))<>0
 then raise exception 'local delivery denied' using errcode='42501';end if;
 select ei.* into i from runtime.nexloop_effect_intents ei where ei.intent_id=(p->>'intent_id')::uuid and ei.tenant_id=tenant and ei.world=p_world;
 if not found or i.executor_principal is distinct from principal or i.action_name is distinct from 'nexloop.service.request'
  or verb in ('deliver','recover_reserve','recover_deliver') and target is distinct from 'eios:action:'||i.action_name||':'||(i.action_definition->>'version')
 then raise exception 'local delivery denied' using errcode='42501';end if;
 if verb='recover_claim' then
  -- Exact existing intent only, never a generic scanner or a new provider attempt.
  select ea.* into a from runtime.nexloop_effect_attempts ea where ea.intent_id=i.intent_id and ea.tenant_id=tenant and ea.world=p_world;
  select eb.* into pb from runtime.nexloop_effect_provider_bindings eb where eb.intent_id=i.intent_id and eb.tenant_id=tenant and eb.world=p_world;
  if a.intent_id is null or pb.intent_id is null or pb.provider_profile_digest is distinct from p->>'provider_profile_digest'
   or a.state not in ('dispatching','unknown','provider_accepted') or i.governed_claim_finalized
   or jsonb_typeof(p->'execution_claim') is distinct from 'object'
  then raise exception 'local recovery denied' using errcode='42501';end if;
  -- A genuine Governor denies IN_PROGRESS. Do not acquire a long technical
  -- lease while waiting for that original Action claim to expire.
  select ca.* into ac from runtime.nexloop_action_claims ca where ca.tenant_id=tenant and ca.world=p_world
   and ca.action_name=i.action_name and ca.intent_id=i.intent_id::text;
  if not found or ac.principal_id is distinct from principal or ac.claim->>'state' is distinct from 'active'
   or ac.claim->>'lease_expires_at' is null or (ac.claim->>'lease_expires_at')::timestamptz>clock_timestamp()
  then raise exception 'local recovery claim not expired' using errcode='42501';end if;
  claim_wire:=p->'execution_claim';
  if (claim_wire->>'payload')::jsonb is distinct from jsonb_build_object('verb','claim','intent_id',i.intent_id,'lease_seconds',30)
  then raise exception 'local recovery denied' using errcode='42501';end if;
  result:=authz.nexloop_effect_execution_command(p_digest,p_world,claim_wire->>'text',claim_wire->>'signature',claim_wire->>'payload');
  select eb.* into pb from runtime.nexloop_effect_provider_bindings eb where eb.intent_id=i.intent_id and eb.tenant_id=tenant and eb.world=p_world for update;
  perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
  if pb.provider_profile_digest is distinct from p->>'provider_profile_digest' or (c->>'expires_at')::timestamptz<=clock_timestamp()
  then raise exception 'local recovery denied' using errcode='42501';end if;
  return result;
 end if;
 -- Match 0042's control -> intent -> outbox -> attempt -> claim lock order.
 if verb in ('deliver','recover_reserve','recover_deliver') then
  if published.definition is distinct from i.action_definition or published.capability is distinct from i.capability then raise exception 'local delivery denied' using errcode='42501';end if;
  plan:=authz.nexloop_assert_effect_plan(i.context_id,tenant,p_world);
  if not exists(select 1 from runtime.nexloop_effect_control_reservations cr where cr.intent_id=i.intent_id and cr.tenant_id=tenant and cr.control_id=plan->>'control_id')
  then raise exception 'local delivery denied' using errcode='42501';end if;
 end if;
 select ei.* into i from runtime.nexloop_effect_intents ei where ei.intent_id=i.intent_id and ei.tenant_id=tenant and ei.world=p_world for update;
 select eo.* into o from runtime.nexloop_effect_outbox eo where eo.intent_id=i.intent_id and eo.tenant_id=tenant and eo.world=p_world for update;
 if not found then raise exception 'local delivery denied' using errcode='42501';end if;
 terminal_read:=verb='query' and i.governed_claim_finalized and i.state in ('fulfilled','confirmed');
 if not terminal_read and (o.state is distinct from 'leased' or o.lease_credential is distinct from p_digest or o.lease_principal is distinct from principal
  or o.lease_until is null or o.lease_until<=clock_timestamp()) then raise exception 'local delivery denied' using errcode='42501';end if;
 select ea.* into a from runtime.nexloop_effect_attempts ea where ea.intent_id=i.intent_id and ea.tenant_id=tenant and ea.world=p_world for update;
 if not found or a.executor_principal is distinct from principal or a.origin_run_id is distinct from i.origin_run_id
  or a.provider_payload_digest is distinct from i.provider_payload_digest then raise exception 'local delivery denied' using errcode='42501';end if;
 select eb.* into pb from runtime.nexloop_effect_provider_bindings eb where eb.intent_id=i.intent_id and eb.tenant_id=tenant and eb.world=p_world for update;
 if not found or pb.provider_profile_digest is distinct from p->>'provider_profile_digest' then raise exception 'local delivery denied' using errcode='42501';end if;
 if terminal_read then
  -- Narrow historical provider GET, no source grant/rebuild or generic lookup.
  select ca.* into ac from runtime.nexloop_action_claims ca where ca.tenant_id=tenant and ca.world=p_world and ca.action_name=i.action_name and ca.intent_id=i.intent_id::text for update;
  if not found or ac.principal_id is distinct from principal or ac.claim->>'state' is distinct from 'terminal'
   or ac.claim->'terminal_outcome'->>'status' is distinct from 'succeeded'
   or ac.claim->'terminal_outcome'->>'outcome_id' is distinct from i.receipt_id::text
   or a.state is distinct from 'fulfilled' or o.state is distinct from 'fulfilled'
  then raise exception 'local delivery denied' using errcode='42501';end if;
 end if;
 select rc.* into r from authz.nexloop_run_credentials rc where rc.run_id=i.origin_run_id;
 if not found then raise exception 'local delivery denied' using errcode='42501';end if;
 if verb in ('deliver','recover_reserve','recover_deliver') then
  select ca.* into ac from runtime.nexloop_action_claims ca where ca.tenant_id=tenant and ca.world=p_world and ca.action_name=i.action_name and ca.intent_id=i.intent_id::text for update;
  if not found or ac.principal_id is distinct from principal or (verb<>'recover_reserve' and ac.claim->>'state' is distinct from 'active')
   or ac.claim->>'lease_expires_at' is null or (verb<>'recover_reserve' and (ac.claim->>'lease_expires_at')::timestamptz<=clock_timestamp())
   or (ac.claim->>'claim_revision')::bigint is distinct from a.action_claim_revision
   or ac.claim->>'fencing_token' is distinct from a.action_fencing_token then raise exception 'local delivery denied' using errcode='42501';end if;
  if jsonb_typeof(p->'effect_fence') is distinct from 'number' or p->>'effect_fence' !~ '^[1-9][0-9]{0,18}$'
   or jsonb_typeof(p->'attempt_revision') is distinct from 'number' or p->>'attempt_revision' !~ '^[1-9][0-9]{0,18}$'
   or (p->>'effect_fence')::bigint is distinct from o.fence or (p->>'attempt_revision')::bigint is distinct from a.attempt_revision
   or (verb='deliver' and (a.state is distinct from 'dispatching' or a.effect_fence is distinct from o.fence))
   or (verb in ('recover_reserve','recover_deliver') and a.state not in ('dispatching','unknown','provider_accepted'))
   or (verb='recover_deliver' and a.effect_fence is distinct from o.fence) or r.status is distinct from 'active'
   or r.expires_at<=clock_timestamp() or jsonb_typeof(p->'origin_proof') is distinct from 'object'
   or p->'origin_proof'->>'resource_id' is distinct from target
   or p->'origin_proof'->>'expires_at' is null or (p->'origin_proof'->>'expires_at')::timestamptz<=clock_timestamp()
   or (p->'origin_proof'->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
   or p->'origin_proof'->>'tenant_id' is distinct from tenant or p->'origin_proof'->>'world' is distinct from p_world
   or p->>'payload_digest' is distinct from i.provider_payload_digest
   or jsonb_typeof(p->'parameters_text') is distinct from 'string'
   or (p->>'parameters_text')::jsonb is distinct from i.frozen_request->'parameters'
   or encode(sha256(convert_to(p->>'parameters_text','UTF8')),'hex') is distinct from i.provider_payload_digest
  then raise exception 'local delivery denied' using errcode='42501';end if;
  perform authz.nexloop_assert_action_authority(r.token_digest,p_world,p->'origin_proof');
  source_identity:=authz.nexloop_service_identity_snapshot(r.token_digest,p_world);
  select rl.* into link from control.nexloop_effect_run_contexts rl where rl.run_id=r.run_id and rl.context_id=i.context_id for update;
  if not found or link.valid_until<=clock_timestamp() then raise exception 'local delivery denied' using errcode='42501';end if;
  select cx.* into ctx from control.nexloop_effect_contexts cx where cx.context_id=i.context_id and cx.tenant_id=tenant and cx.world=p_world;
  if not found or ctx.consumer_id is distinct from i.consumer_id then raise exception 'local delivery denied' using errcode='42501';end if;
  select co.* into consumer from ontology.objects co where co.tenant_id=tenant and co.world=p_world and co.object_id=i.consumer_id and co.type_name='Consumer' for update;
  if not found or consumer.nexloop_revision is distinct from ctx.consumer_revision then raise exception 'local delivery denied' using errcode='42501';end if;
  select bp.* into binding from control.nexloop_effect_plan_bindings bp where bp.context_id=i.context_id and bp.tenant_id=tenant and bp.world=p_world;
  if not found or not(binding.submitter_principals ? (source_identity->'binding'->>'subject_principal_id'))
   or source_identity->'binding'->>'tenant_id' is distinct from tenant then raise exception 'local delivery denied' using errcode='42501';end if;
  if verb='recover_reserve' then
   -- Governor supplied the genuine original reservation command. Never mint a
   -- permit from an in-progress replay and never insert a quota reservation.
   command:=authz.nexloop_effect_assert_claim(i,p->'action_claim','reserve',p->>'action_request_text');
   if command->>'lease_expires_at' is null or (command->>'lease_expires_at')::timestamptz>o.lease_until then raise exception 'local recovery lease invalid' using errcode='42501';end if;
   reserved:=authz.nexloop_action_claim_command(p_digest,p_world,p->'action_claim'->>'text',p->'action_claim'->>'signature',p->'action_claim'->>'payload');
   if reserved->>'disposition' is distinct from 'claimed' then raise exception 'local recovery claim busy' using errcode='42501';end if;
   update runtime.nexloop_effect_attempts ea set attempt_revision=ea.attempt_revision+1,effect_fence=o.fence,
    action_claim_revision=(reserved->'claim'->>'claim_revision')::bigint,action_fencing_token=reserved->'claim'->>'fencing_token'
    where ea.intent_id=i.intent_id and ea.tenant_id=tenant and ea.world=p_world returning ea.* into a;
   select ca.* into ac from runtime.nexloop_action_claims ca where ca.tenant_id=tenant and ca.world=p_world and ca.action_name=i.action_name and ca.intent_id=i.intent_id::text;
  end if;
  if verb='recover_reserve' then
   insert into runtime.nexloop_local_delivery_recoveries as lr (intent_id,tenant_id,world,effect_fence,attempt_revision,action_claim_revision,action_fencing_token,reserved_at)
    values(i.intent_id,tenant,p_world,o.fence,a.attempt_revision,a.action_claim_revision,a.action_fencing_token,clock_timestamp())
    on conflict(intent_id) do update set effect_fence=excluded.effect_fence,attempt_revision=excluded.attempt_revision,
     action_claim_revision=excluded.action_claim_revision,action_fencing_token=excluded.action_fencing_token,reserved_at=excluded.reserved_at
    where lr.tenant_id=tenant and lr.world=p_world;
  elsif verb='recover_deliver' and not exists(select 1 from runtime.nexloop_local_delivery_recoveries lr
   where lr.intent_id=i.intent_id and lr.tenant_id=tenant and lr.world=p_world and lr.effect_fence=o.fence
    and lr.attempt_revision=a.attempt_revision and lr.action_claim_revision=a.action_claim_revision and lr.action_fencing_token=a.action_fencing_token)
  then raise exception 'local recovery permit missing' using errcode='42501';end if;
  result:=jsonb_build_object('intent_id',i.intent_id,'parameters',i.frozen_request->'parameters','payload_digest',i.provider_payload_digest,
   'effect_fence',o.fence,'attempt_revision',a.attempt_revision,'action_claim_result',reserved);
 elsif verb='resolve' then
  -- Private trusted proof preparation only; never returned over HTTP.
  result:=jsonb_build_object('_run_digest',r.token_digest,'origin_run_id',r.run_id,'intent_id',i.intent_id,'effect_fence',o.fence,'attempt_revision',a.attempt_revision,'lease_until',o.lease_until,'frozen_request',i.frozen_request,'action_definition',i.action_definition,'capability',i.capability);
 else result:=jsonb_build_object('intent_id',i.intent_id,'payload_digest',i.provider_payload_digest);end if;
 if verb in ('deliver','recover_reserve','recover_deliver') then
  perform authz.nexloop_assert_effect_plan(i.context_id,tenant,p_world);
  perform authz.nexloop_assert_action_authority(r.token_digest,p_world,p->'origin_proof');
  if (ac.claim->>'lease_expires_at')::timestamptz<=clock_timestamp() or r.expires_at<=clock_timestamp() or link.valid_until<=clock_timestamp()
   or (p->'origin_proof'->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'local delivery denied' using errcode='42501';end if;
 end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 if (c->>'expires_at')::timestamptz<=clock_timestamp() or (not terminal_read and (o.lease_until is null or o.lease_until<=clock_timestamp())) then raise exception 'local delivery denied' using errcode='42501';end if;
 return result;
end $$;

create function authz.nexloop_effect_consumer_action_resources(p_digest text,p_world text,p_consumer text) returns jsonb
 language plpgsql volatile security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant text:=ident->'binding'->>'tenant_id';v integer;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') then raise exception 'effect registration unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 select o.schema_version into v from ontology.objects o where o.tenant_id=v_tenant and o.world=p_world and o.type_name='Consumer' and o.object_id=p_consumer;
 if v is null then return null;end if;
 return (select jsonb_object_agg(ea.stable_name,authz.nexloop_effect_bind_action_resource(v_tenant,p_world,ea.slot_identity,v)) from control.nexloop_effect_actions ea);
end $$;
alter function authz.nexloop_effect_consumer_action_resources(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_consumer_action_resources(text,text,text) from public;
grant execute on function authz.nexloop_effect_consumer_action_resources(text,text,text) to nexloop_api,nexloop_domain_worker;

comment on table control.nexloop_effect_action_admissions is 'option B effect Action admissions; erasure: admitted_by and retired_by hold role labels only (checks: human:owner or human:operator, admitted_by also publication from M2.3), never a member identity, so neither the staff nor the Consumer erasure needs to reach this table';

-- 6. -------------------------------------------------------------------------------------------------------------------------
create or replace function control.nexloop_retire_effect_action_admission(p_tenant text,p_stable_name text,p_version integer,p_actor text,p_reason text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cur control.nexloop_effect_action_admissions;
begin
 if session_user is distinct from 'nexloop_configurator' then raise exception 'trusted configuration only' using errcode='42501';end if;
 if coalesce(p_actor,'') !~ '^human:(owner|operator)$' or char_length(coalesce(p_reason,'')) not between 1 and 500 then
  raise exception 'invalid effect Action retirement' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform 1 from control.nexloop_effect_action_admissions where tenant_id=p_tenant and world='real' and stable_name=p_stable_name
  and version=p_version and retired_at is null for update;
 if exists(select 1 from control.nexloop_effect_contexts x where x.tenant_id=p_tenant and x.world='real'
   and x.action_resource='eios:action:'||p_stable_name||':'||p_version and x.valid_until>clock_timestamp()) then
  return jsonb_build_object('retired',false,'admission_id',null,'code','referenced_by_unexpired_context');end if;
 update control.nexloop_effect_action_admissions set retired_at=clock_timestamp(),retired_by=p_actor,retired_reason=p_reason
  where tenant_id=p_tenant and world='real' and stable_name=p_stable_name and version=p_version and retired_at is null returning * into cur;
 return jsonb_build_object('retired',cur.admission_id is not null,'admission_id',cur.admission_id);
end $$;
