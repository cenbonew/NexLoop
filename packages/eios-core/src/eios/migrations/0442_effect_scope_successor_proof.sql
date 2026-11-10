-- A Schema publication's successor proves the effect Action's Consumer scope (ADR addendum A', owner 2026-11-10; stage 11-09/10:
-- after Consumer v2 every relay reply to an existing customer failed - the registrar named :1 (0179) whose scope holds Consumer:1, and
-- the effect Action stays :1 (control.nexloop_effect_actions, Host and contracts pin it) while 0183 required its scope to hold the
-- Consumer's current version). Temporary number; the dispatcher renumbers. Two places only, nothing granted:
-- 1. authz.nexloop_effect_context_command (0179, copied; two substitutions): the registrar resource is an exact active version of the
--    verb's stable name (was ':1'), and the definition reference names that version; every scope/snapshot check is unchanged, so the
--    registrar version must cover the four objects exactly (the caller chooses it, effect_contexts.py; a wrong choice is refused).
-- 2. authz.nexloop_effect_intent_command_v0040 (0183, copied; one substitution): when the executed effect Action's scope does not hold
--    the Consumer's current version v, authz.nexloop_effect_scope_successor_proof may prove it - A'-1..A'-4 and L6 D1/D2: an active
--    successor of the same stable name whose definition equals the executed one without versions, whose scope holds Consumer:v and
--    has the same types, published by a human F3 publication with an unchanged permission digest; and Consumer v extends the
--    executed scope's Consumer version b cumulatively (b's properties a prefix of v's, each added property optional, nullable, not
--    hidden, without default; other keys equal). The proof is recorded (executed resource, proof resource, publication). Transitional:
--    removed when effect Actions may be versioned (option B; tests/test_effect_scope_successor_proof_pg.py guards it).

create table control.nexloop_effect_scope_proofs (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,context_id uuid not null,
 executed_resource text not null,scope_proof_resource text not null,publication_id uuid not null,consumer_version integer not null check(consumer_version>1),
 recorded_at timestamptz not null default clock_timestamp(),
 primary key(context_id,executed_resource,consumer_version)
);
alter table control.nexloop_effect_scope_proofs owner to nexloop_owner;
alter table control.nexloop_effect_scope_proofs enable row level security;
alter table control.nexloop_effect_scope_proofs force row level security;
create policy effect_scope_proofs_owner on control.nexloop_effect_scope_proofs to nexloop_owner using(true) with check(true);
revoke all on control.nexloop_effect_scope_proofs from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity,nexloop_metrics;

create function authz.nexloop_effect_scope_successor_proof(p_context uuid,p_tenant text,p_world text,p_executed control.nexloop_action_definitions,p_version integer)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare b integer;tb jsonb;tv jsonb;nb integer;s control.nexloop_action_definitions;v_pub uuid;
begin
 -- The executed Action's own Consumer version (exactly one Consumer reference).
 select min((r->>'version')::integer),count(*) into b,nb from jsonb_array_elements(p_executed.definition->'governance'->'change_scope'->'object_types') r
  where r->>'tenant_id'=p_tenant and r->>'stable_name'='Consumer';
 if nb<>1 or b is null or p_version is null or p_version<=b then return null;end if;
 -- D1/D2: Consumer v extends b cumulatively.
 select definition into tb from ontology.object_type_versions where tenant_id=p_tenant and type_name='Consumer' and version=b;
 select definition into tv from ontology.object_type_versions where tenant_id=p_tenant and type_name='Consumer' and version=p_version;
 if tb is null or tv is null or (tb-'version'-'properties') is distinct from (tv-'version'-'properties')
  or jsonb_array_length(coalesce(tv->'properties','[]'::jsonb))<=jsonb_array_length(coalesce(tb->'properties','[]'::jsonb))
  or exists(select 1 from jsonb_array_elements(coalesce(tb->'properties','[]'::jsonb)) with ordinality o(e,n)
   where e is distinct from tv->'properties'->(n::integer-1))
  or exists(select 1 from jsonb_array_elements(tv->'properties') with ordinality a(e,n) where n>jsonb_array_length(coalesce(tb->'properties','[]'::jsonb))
   and (coalesce((e->>'required')::boolean,false) or coalesce((e->>'nullable')::boolean,false) is not true
    or coalesce(e->>'visibility','normal')='hidden' or coalesce(e->'default_value','null'::jsonb)<>'null'::jsonb))
 then return null;end if;
 -- A'-1/A'-2: an active successor equal without versions, its scope holding Consumer:v, from a human F3 publication.
 select d.* into s from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world=p_world and d.active
  and d.resource_id<>p_executed.resource_id and d.definition->>'stable_name'=p_executed.definition->>'stable_name'
  and exists(select 1 from jsonb_array_elements(d.definition->'governance'->'change_scope'->'object_types') r
   where r->>'tenant_id'=p_tenant and r->>'stable_name'='Consumer' and (r->>'version')::integer=p_version)
  and d.capability=p_executed.capability
  and authz.nexloop_effect_definition_unversioned(d.definition)=authz.nexloop_effect_definition_unversioned(p_executed.definition)
  order by (d.definition->>'version')::integer desc limit 1;
 if not found then return null;end if;
 select p.publication_id into v_pub from ontology.nexloop_schema_publications p where p.tenant_id=p_tenant and p.world=p_world
  and s.resource_id=any(p.successor_actions) and p.published_by like 'human:%' and p.permission_digest_before=p.permission_digest_after
  order by p.published_at limit 1;
 if v_pub is null then return null;end if;
 -- A'-4: the executed and the proving resource are both recorded.
 insert into control.nexloop_effect_scope_proofs(tenant_id,world,context_id,executed_resource,scope_proof_resource,publication_id,consumer_version)
  values(p_tenant,p_world,p_context,p_executed.resource_id,s.resource_id,v_pub,p_version) on conflict do nothing;
 return jsonb_build_object('executed_resource',p_executed.resource_id,'scope_proof_resource',s.resource_id,'publication_id',v_pub,'consumer_version',p_version);
end $$;
alter function authz.nexloop_effect_scope_successor_proof(uuid,text,text,control.nexloop_action_definitions,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_scope_successor_proof(uuid,text,text,control.nexloop_action_definitions,integer) from public;

-- An Action definition without its versions: version, contract digest, and each object type reference's version and schema digest.
create function authz.nexloop_effect_definition_unversioned(p jsonb) returns jsonb language sql immutable set search_path=pg_catalog,pg_temp as $$
 select jsonb_set(jsonb_set(p-'version'-'contract_digest','{object_types}',
   coalesce((select jsonb_agg(r-'version'-'schema_digest' order by n) from jsonb_array_elements(p->'object_types') with ordinality q(r,n)),'[]'::jsonb)),
  '{governance,change_scope,object_types}',
   coalesce((select jsonb_agg(r-'version'-'schema_digest' order by n) from jsonb_array_elements(p->'governance'->'change_scope'->'object_types') with ordinality q(r,n)),'[]'::jsonb))
$$;
alter function authz.nexloop_effect_definition_unversioned(jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_definition_unversioned(jsonb) from public;

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
 if not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->'executor_proof'->>'resource_id')
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
   or not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->'run_proof'->>'resource_id') then raise exception 'Run required' using errcode='42501';end if;
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
    and ea.resource_id=c->'run_proof'->>'resource_id' and ea.resource_id=c->'executor_proof'->>'resource_id')
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
   if existing.goal_id<>goal.object_id or existing.goal_revision<>goal.nexloop_revision or existing.step_revision<>step.nexloop_revision
    or existing.control_id<>ctl.object_id or existing.control_revision<>ctl.nexloop_revision or existing.submitter_principals is distinct from step.properties->'submitter_principals'
   then raise exception 'effect context conflict' using errcode='P0001';end if;
  else
   context_uuid:=gen_random_uuid();
   insert into control.nexloop_effect_contexts values(context_uuid,c->>'tenant_id',p_world,consumer.object_id,'goal:'||goal.object_id,
    'goal:'||goal.object_id||':revision:'||goal.nexloop_revision||':step:'||step.nexloop_revision||':control:'||ctl.nexloop_revision,
    'step:'||step.object_id,(select ea.slot_identity from control.nexloop_effect_actions ea where ea.stable_name=step.properties->>'action_name'),consumer.nexloop_revision,ctl.nexloop_revision,true,least(l.valid_until,(goal.properties->>'valid_until')::timestamptz),
    l.budget_units,0,jsonb_build_array(jsonb_build_object('resource_ref','consumer:'||consumer.object_id,'revision',consumer.nexloop_revision)),l.executor_principal);
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
end $function$
;

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
  or c->>'protocol' is distinct from 'nexloop-effect-intent-v1' or not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->>'resource_id') or not exists(select 1 from control.nexloop_effect_actions ev where ev.resource_id=c->>'resource_id' and ev.version::text=p->>'action_version')
  or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'expires_at' is null or (c->>'expires_at')::timestamptz<=clock_timestamp() or (c->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds'
  or p->>'verb' is null or p->>'verb' not in ('submit','find') then raise exception 'effect authority unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,c);
 identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if identity->'run_context' is null or identity->'run_context'='null'::jsonb then raise exception 'effect Run required' using errcode='42501';end if;
 select * into run_row from authz.nexloop_run_credentials where token_digest=p_digest for share;
 if not found then raise exception 'effect Run required' using errcode='42501';end if;
 select * into link from control.nexloop_effect_run_contexts where run_id=run_row.run_id and slot_identity=(select ea.slot_identity from control.nexloop_effect_actions ea where ea.resource_id=c->>'resource_id') for share;
 if not found or link.valid_until<=clock_timestamp() then raise exception 'effect context unavailable' using errcode='42501';end if;
 select * into ctx from control.nexloop_effect_contexts where context_id=link.context_id
  and tenant_id=c->>'tenant_id' and world=p_world for update;
 if not found or not ctx.allow_effect or ctx.valid_until<=clock_timestamp() then raise exception 'effect controls unavailable' using errcode='42501';end if;
 select ea.stable_name into v_action from control.nexloop_effect_actions ea where ea.slot_identity=ctx.slot_identity and ea.resource_id=c->>'resource_id';
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

-- 3. The registrar version a caller should name (advisory, L6 Q1/Q2; the command above checks the exact resource): the highest active
--    version of the verb's stable name whose scope holds every object's exact (type, version) - bind: the step, its goal, control
--    and Consumer; configure: the control - and on which the calling session holds a live execute grant. Null: none.
create function authz.nexloop_effect_registrar_version(p_digest text,p_world text,p_verb text,p_payload jsonb) returns integer
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant text:=ident->'binding'->>'tenant_id';
 v_principal text:=ident->'binding'->>'subject_principal_id';v_name text;need jsonb;step ontology.objects;v_now timestamptz:=clock_timestamp();v integer;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or p_verb not in ('configure','bind') or jsonb_typeof(p_payload) is distinct from 'object' then
  raise exception 'effect registration unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 v_name:=case p_verb when 'configure' then 'nexloop.effect.configure_control' else 'nexloop.plan.bind_effect_context' end;
 if p_verb='configure' then
  select jsonb_agg(jsonb_build_object('t',o.type_name,'v',o.schema_version)) into need from ontology.objects o
   where o.tenant_id=v_tenant and o.world=p_world and o.type_name='EffectControl' and o.object_id=p_payload->>'control_id';
 else
  select * into step from ontology.objects where tenant_id=v_tenant and world=p_world and type_name='PlanStep' and object_id=p_payload->>'step_id';
  if not found then return null;end if;
  select jsonb_agg(jsonb_build_object('t',o.type_name,'v',o.schema_version)) into need from ontology.objects o
   where o.tenant_id=v_tenant and o.world=p_world and o.object_id in(step.object_id,step.properties->>'goal_id',step.properties->>'control_id',step.properties->>'consumer_id');
  if jsonb_array_length(coalesce(need,'[]'::jsonb))<>4 then return null;end if;
 end if;
 if need is null then return null;end if;
 select (d.definition->>'version')::integer into v from control.nexloop_action_definitions d
  where d.tenant_id=v_tenant and d.world=p_world and d.active and d.definition->>'stable_name'=v_name
   and not exists(select 1 from jsonb_array_elements(need) x where not exists(select 1 from jsonb_array_elements(d.definition->'governance'->'change_scope'->'object_types') t
    where t->>'tenant_id'=v_tenant and t->>'stable_name'=x->>'t' and (t->>'version')::integer=(x->>'v')::integer))
   and exists(select 1 from authz.nexloop_authority_facts f cross join lateral jsonb_array_elements(coalesce(f.payload->'grants','[]'::jsonb)) g
    where f.tenant_id=v_tenant and f.fact_kind='grants' and f.entity_key=array[v_principal,d.resource_id]
     and (f.payload->>'valid_until' is null or (f.payload->>'valid_until')::timestamptz>v_now)
     and coalesce(g->'operations','[]'::jsonb) ? 'execute'
     and (g->>'valid_from' is null or (g->>'valid_from')::timestamptz<=v_now)
     and (g->>'valid_until' is null or (g->>'valid_until')::timestamptz>v_now))
  order by (d.definition->>'version')::integer desc limit 1;
 return v;
end $$;
alter function authz.nexloop_effect_registrar_version(text,text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_registrar_version(text,text,text,jsonb) from public;
grant execute on function authz.nexloop_effect_registrar_version(text,text,text,jsonb) to nexloop_api,nexloop_domain_worker;
