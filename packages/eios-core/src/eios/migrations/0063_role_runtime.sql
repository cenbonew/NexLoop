-- Strict TTL shape precedes existing signed Object/Property READ verification.
create function authz.nexloop_role_strict_read(p_digest text,p_world text,p_text text,p_signature text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;proof jsonb;v jsonb;
begin
 if a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or jsonb_typeof(a->'property_authorities') is distinct from 'array' then raise exception 'current Source READ required' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(a->'property_authorities') loop
  if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'current Source property READ required' using errcode='42501';end if;
 end loop;
 v:=authz.nexloop_read_object(p_digest,p_world,p_text,p_signature);
 if (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'current Source READ expired' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(a->'property_authorities') loop
  if (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'current Source property READ expired' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 if (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'current Source READ expired' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(a->'property_authorities') loop if (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'current Source property READ expired' using errcode='42501';end if;end loop;return v;
end $$;
alter function authz.nexloop_role_strict_read(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_strict_read(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

-- DRAFT: technical RoleBinding, not a formal business object or authority grant.
-- Never catalog this primitive without runtime/effect current-verifier wrappers.
create table authz.nexloop_role_run_bindings (
 run_id uuid primary key references authz.nexloop_run_credentials(run_id),
 source_digest text not null, tenant_id text not null,world text not null check(world='real'),
 consumer_id text not null,link_id text not null,role_id text not null,step_id text not null,
 link_revision bigint not null,role_revision bigint not null,step_revision bigint not null,
 role_ref text not null,scope text not null,expires_at timestamptz not null
);
alter table authz.nexloop_role_run_bindings owner to nexloop_owner;
alter table authz.nexloop_role_run_bindings enable row level security;
alter table authz.nexloop_role_run_bindings force row level security;
create policy role_run_tenant on authz.nexloop_role_run_bindings to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on authz.nexloop_role_run_bindings from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

create function authz.nexloop_role_run_validate(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;k bytea;ident jsonb;r authz.nexloop_run_credentials;
 tenant text;principal text;env jsonb;v jsonb;link jsonb;role jsonb;step jsonb;expiry timestamptz;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker','nexloop_action_worker')
  or p_text is null or p_payload is null or octet_length(p_text)>4096 or octet_length(p_payload)>524288
  or jsonb_typeof(c) is distinct from 'object' or jsonb_typeof(p) is distinct from 'object'
  or (select count(*) from jsonb_object_keys(c))<>3 or (select count(*) from jsonb_object_keys(p))<>6
  or c->>'protocol' is distinct from 'nexloop-role-run-v1'
  or c->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or p_world is distinct from 'real'
 then raise exception 'role binding unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=c->>'key_id' and active for share;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-role-run-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
 then raise exception 'role binding unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb then raise exception 'nested role binding denied' using errcode='42501';end if;
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',tenant,true);
 select * into r from authz.nexloop_run_credentials where run_id=(p->>'run_id')::uuid for share;
 if not found or r.source_digest is distinct from p_digest or r.world is distinct from p_world
  or r.status is distinct from 'active' or r.expires_at is null or r.expires_at<=clock_timestamp()
 then raise exception 'role Run unavailable' using errcode='42501';end if;
 perform authz.nexloop_service_identity_snapshot(r.token_digest,p_world);
 if jsonb_typeof(p->'reads') is distinct from 'object' or (select count(*) from jsonb_object_keys(p->'reads'))<>3
  or not (p->'reads' ?& array['link','role','step']) then raise exception 'role READ unavailable' using errcode='42501';end if;
 -- Same stable object-ID lock order used by formal mutation and related readers.
 perform 1 from ontology.objects where tenant_id=tenant and world=p_world
  and object_id in(p->>'link_id',p->>'role_id',p->>'step_id') order by object_id for share;
 for env in select value from jsonb_each(p->'reads') order by key loop
  if jsonb_typeof(env) is distinct from 'object' or (select count(*) from jsonb_object_keys(env))<>2
   then raise exception 'role READ unavailable' using errcode='42501';end if;
  v:=authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');
  if v->>'type_name'='ConsumerRoleLink' and v->>'object_id'=p->>'link_id'
   and (env->>'text')::jsonb->'fields'='["active","consumer_id","role_id","scope","valid_from","valid_until"]'::jsonb then link:=v;
  elsif v->>'type_name'='RoleDefinition' and v->>'object_id'=p->>'role_id'
   and (env->>'text')::jsonb->'fields'='["active","ceiling_ref","name","responsibility","valid_from","valid_until"]'::jsonb then role:=v;
  elsif v->>'type_name'='PlanStep' and v->>'object_id'=p->>'step_id'
   and (env->>'text')::jsonb->'fields'='["consumer_id","state","submitter_principals"]'::jsonb then step:=v;
  else raise exception 'role READ unavailable' using errcode='42501';end if;
 end loop;
 if link is null or role is null or step is null
  or link->'properties'->>'consumer_id' is distinct from p->>'consumer_id'
  or link->'properties'->>'role_id' is distinct from p->>'role_id'
  or step->'properties'->>'consumer_id' is distinct from p->>'consumer_id'
  or step->'properties'->>'state' is distinct from 'ready'
  or jsonb_typeof(step->'properties'->'submitter_principals') is distinct from 'array'
  or not (step->'properties'->'submitter_principals' ? principal)
  or link->'properties'->'active' is distinct from 'true'::jsonb or role->'properties'->'active' is distinct from 'true'::jsonb
 then raise exception 'role selection unavailable' using errcode='42501';end if;
 expiry:=r.expires_at;
 for v in select x from (values(link),(role)) t(x) loop
  if v->'properties'->>'valid_from' is null or v->'properties'->>'valid_until' is null
   or v->'properties'->>'valid_from' !~ '(Z|\+00:00)$' or v->'properties'->>'valid_until' !~ '(Z|\+00:00)$'
   or (v->'properties'->>'valid_from')::timestamptz>clock_timestamp()
   or (v->'properties'->>'valid_until')::timestamptz<=clock_timestamp()
   or (v->'properties'->>'valid_from')::timestamptz >= (v->'properties'->>'valid_until')::timestamptz
  then raise exception 'role validity unavailable' using errcode='42501';end if;
  expiry:=least(expiry,(v->'properties'->>'valid_until')::timestamptz);
 end loop;
 return jsonb_build_object('run_id',r.run_id,'source_digest',p_digest,'tenant_id',tenant,'world',p_world,
  'consumer_id',p->>'consumer_id','link_id',p->>'link_id','role_id',p->>'role_id','step_id',p->>'step_id',
  'link_revision',link->'revision','role_revision',role->'revision','step_revision',step->'revision',
  'role_ref','role:'||(p->>'role_id')||':mapping:'||(p->>'link_id'),
  'scope',link->'properties'->>'scope','expires_at',expiry);
end $$;
alter function authz.nexloop_role_run_validate(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_run_validate(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

create function authz.nexloop_role_run_bind(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare v jsonb;again jsonb;existing authz.nexloop_role_run_bindings;
begin
 if session_user<>'nexloop_api' then raise exception 'role binder unavailable' using errcode='42501';end if;
 v:=authz.nexloop_role_run_validate(p_digest,p_world,p_text,p_signature,p_payload);
 -- Admission is irreversible for this binding. The run credential SHARE lock
 -- serializes with the registration path's credential locking, but final
 -- registration wrapper must also check binding (not implemented in DRAFT).
 if exists(select 1 from authz.nexloop_runtime_run_bindings where run_id=(v->>'run_id')::uuid)
 then raise exception 'admitted Run cannot bind role' using errcode='42501';end if;
 select * into existing from authz.nexloop_role_run_bindings where run_id=(v->>'run_id')::uuid for update;
 if found then
  if to_jsonb(existing) is distinct from v then raise exception 'role binding immutable' using errcode='42501';end if;
 else
  insert into authz.nexloop_role_run_bindings select * from jsonb_populate_record(null::authz.nexloop_role_run_bindings,v);
 end if;
 again:=authz.nexloop_role_run_validate(p_digest,p_world,p_text,p_signature,p_payload);
 if again is distinct from v then raise exception 'role binding changed' using errcode='42501';end if;
 return v-'source_digest'||jsonb_build_object('grants_authority',false,'dispatch_permit',false);
end $$;
alter function authz.nexloop_role_run_bind(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_run_bind(text,text,text,text,text) from public,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_role_run_bind(text,text,text,text,text) to nexloop_api;

-- Owner-only lifecycle verifier: signed *fresh* Source READ envelope, exact
-- immutable selection/revisions. Call at admission/submit/dispatch/finalize
-- before AND after each corresponding governed command in the same transaction.
create function authz.nexloop_role_run_current(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare v jsonb;b authz.nexloop_role_run_bindings;definition jsonb;
begin
 v:=authz.nexloop_role_run_validate(p_digest,p_world,p_text,p_signature,p_payload);
 select * into b from authz.nexloop_role_run_bindings where run_id=(v->>'run_id')::uuid for share;
 if not found or to_jsonb(b) is distinct from v or b.expires_at is null or b.expires_at<=clock_timestamp()
 then raise exception 'current role binding unavailable' using errcode='42501';end if;
 select properties into definition from ontology.objects where tenant_id=b.tenant_id and world=b.world
  and object_id=b.role_id and type_name='RoleDefinition' and nexloop_revision=b.role_revision;
 if not found then raise exception 'current role definition unavailable' using errcode='42501';end if;
 return jsonb_build_object('binding',v-'source_digest','definition',definition,
  'definition_provenance','eios:object:'||b.role_id,'mapping_provenance','eios:object:'||b.link_id,'grants_authority',false);
end $$;
alter function authz.nexloop_role_run_current(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_run_current(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

create function authz.nexloop_role_run_hint(p_run_digest text,p_world text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;run authz.nexloop_run_credentials;b authz.nexloop_role_run_bindings;
begin
 ident:=authz.nexloop_service_identity_snapshot(p_run_digest,p_world);
 if ident->'run_context' is null or ident->'run_context'='null'::jsonb then raise exception 'actual Run required' using errcode='42501';end if;
 perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 select * into run from authz.nexloop_run_credentials where token_digest=p_run_digest;
 select * into b from authz.nexloop_role_run_bindings where run_id=run.run_id;
 if not found then return null;end if;
 if b.tenant_id is distinct from ident->'binding'->>'tenant_id' or b.world is distinct from p_world
  or b.source_digest is distinct from run.source_digest then raise exception 'role Run unavailable' using errcode='42501';end if;
 return jsonb_build_object('_source_digest',run.source_digest,'run_id',run.run_id,'consumer_id',b.consumer_id,
  'role_id',b.role_id,'link_id',b.link_id,'step_id',b.step_id);
end $$;
alter function authz.nexloop_role_run_hint(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_run_hint(text,text) from public,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_role_run_hint(text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) rename to nexloop_runtime_activation_command_before_roles_v0062;
revoke all on function authz.nexloop_runtime_activation_command_before_roles_v0062(text,text,text,text,text)
 from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_runtime_activation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;command jsonb:=(p->>'command_text')::jsonb;
 result jsonb;run authz.nexloop_run_credentials;b authz.nexloop_role_run_bindings;env jsonb;before jsonb;after jsonb;cb record;ar record;artifact_proof jsonb;
begin
 -- Serializes bind/admission before baseline registration writes. Exact Queue
 -- signature, ownership, Run proofs and Action checks remain baseline-owned.
 if p->>'verb'='register' then
  perform 1 from authz.nexloop_run_credentials where token_digest=p->>'run_digest' for update;
 end if;
 result:=authz.nexloop_runtime_activation_command_before_roles_v0062(p_digest,p_world,p_text,p_signature,p_payload);
 perform set_config('eios.tenant_id',a->>'tenant_id',true);
 select * into b from authz.nexloop_role_run_bindings where run_id=(command->>'run_id')::uuid;
 if not found then
  if command->>'role_ref' ~ '^role:[a-f0-9]{64}:mapping:[a-f0-9]{64}$'
   then raise exception 'bound role mandatory' using errcode='42501';end if;
  return result;
 end if;
 if b.tenant_id is distinct from a->>'tenant_id' or b.world is distinct from p_world
  or command->>'role_ref' is distinct from b.role_ref or command->>'consumer_ref' is distinct from 'consumer:'||b.consumer_id
 then raise exception 'role command mismatch' using errcode='42501';end if;
 select * into cb from runtime.nexloop_role_context_artifacts where run_id=b.run_id and tenant_id=b.tenant_id and world=p_world;
 if not found then raise exception 'role Context mandatory' using errcode='42501';end if;
 if cb.source_digest is distinct from b.source_digest or cb.command_binding is distinct from authz.nexloop_context_command_binding(command)
  or command->>'context_manifest_ref' is distinct from 'artifact:'||cb.artifact_id or command->>'credential_ref' is distinct from 'run:'||b.run_id::text
  or (p->>'input_digest' is not null and p->>'input_digest' is distinct from cb.pack_digest)
  then raise exception 'role Context command mismatch' using errcode='42501';end if;
 select * into ar from runtime.nexloop_local_artifacts where tenant_id=b.tenant_id and world=p_world and artifact_id=cb.artifact_id for share;
 if not found or ar.status is distinct from 'available' or (ar.retention_until is null or ar.retention_until<=clock_timestamp()) or ar.sha256 is distinct from cb.pack_digest
  or ar.size_bytes is distinct from octet_length(cb.pack_text) or ar.object_key is distinct from cb.namespace||'/'||cb.artifact_id
  then raise exception 'role Context Artifact unavailable' using errcode='42501';end if;
 -- Resolve is private metadata only, not an authorization response. Create
 -- cannot leave the trusted port before authorize(), which requires READ.
 if p->>'verb'='resolve' then return result||jsonb_build_object('_context_source_digest',cb.source_digest,'_context_catalog',cb.pack_text::jsonb->'supply','_context_consumer_id',b.consumer_id);
 elsif p->>'verb'='create' then return result;end if;
 select * into run from authz.nexloop_run_credentials where run_id=b.run_id;
 env:=a->'context_role_envelope';
 if jsonb_typeof(env) is distinct from 'object' then raise exception 'role READ mandatory' using errcode='42501';end if;
 before:=authz.nexloop_role_run_current(run.source_digest,p_world,env->>'text',env->>'signature',env->>'payload');
 if before->'binding'->>'run_id' is distinct from b.run_id::text then raise exception 'role proof Run mismatch' using errcode='42501';end if;
 -- Baseline is idempotent within this transaction. Tail rechecks current Role
 -- READ/definitions after any potentially blocking technical writes.
 result:=authz.nexloop_runtime_activation_command_before_roles_v0062(p_digest,p_world,p_text,p_signature,p_payload);
 after:=authz.nexloop_role_run_current(run.source_digest,p_world,env->>'text',env->>'signature',env->>'payload');
 if after is distinct from before then raise exception 'role changed' using errcode='42501';end if;
 if p->>'verb'='authorize' then
  artifact_proof:=a->'context_artifact_proof';
  if artifact_proof->>'operation' is distinct from 'read' or artifact_proof->>'expires_at' is null then raise exception 'role Artifact READ mandatory' using errcode='42501';end if;
  perform authz.nexloop_assert_artifact_authority(cb.source_digest,p_world,artifact_proof);
  result:=result||jsonb_build_object('context_artifact',jsonb_build_object('artifact_ref','artifact:'||cb.artifact_id,'sha256',cb.pack_digest,'command_binding_digest',cb.pack_text::jsonb->'bindings'->>'command_digest'));
 end if;
 return result;
end $$;
alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_runtime_activation_command(text,text,text,text,text) from public,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_runtime_activation_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_scheduler;

alter function authz.nexloop_effect_intent_command(text,text,text,text,text) rename to nexloop_effect_intent_command_before_roles_v0062;
revoke all on function authz.nexloop_effect_intent_command_before_roles_v0062(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_effect_intent_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c jsonb:=p_text::jsonb;result jsonb;run authz.nexloop_run_credentials;b authz.nexloop_role_run_bindings;env jsonb;checked jsonb;
begin
 result:=authz.nexloop_effect_intent_command_before_roles_v0062(p_digest,p_world,p_text,p_signature,p_payload);
 select * into run from authz.nexloop_run_credentials where token_digest=p_digest;
 if not found then return result;end if;
 perform set_config('eios.tenant_id',c->>'tenant_id',true);
 select * into b from authz.nexloop_role_run_bindings where run_id=run.run_id;
 if not found then return result;end if;
 env:=c->'role_envelope';
 if jsonb_typeof(env) is distinct from 'object' then raise exception 'role READ mandatory' using errcode='42501';end if;
 checked:=authz.nexloop_role_run_current(run.source_digest,p_world,env->>'text',env->>'signature',env->>'payload');
 if checked->'binding'->>'run_id' is distinct from run.run_id::text then raise exception 'role Run mismatch' using errcode='42501';end if;
 return result;
end $$;
alter function authz.nexloop_effect_intent_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_intent_command(text,text,text,text,text) from public,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_effect_intent_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

alter function authz.nexloop_effect_execution_command(text,text,text,text,text) rename to nexloop_effect_execution_command_before_roles_v0062;
revoke all on function authz.nexloop_effect_execution_command_before_roles_v0062(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_effect_execution_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare c jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;result jsonb;i runtime.nexloop_effect_intents;
 run authz.nexloop_run_credentials;b authz.nexloop_role_run_bindings;env jsonb;checked jsonb;
begin
 -- QUERY, unknown and durable observation deliberately preserve the original
 -- independent current Worker QUERY authority/fence. They never authorize POST
 -- or mark a formal Action succeeded. Baseline checks remain mandatory.
 result:=authz.nexloop_effect_execution_command_before_roles_v0062(p_digest,p_world,p_text,p_signature,p_payload);
 if p->>'verb' not in('admit','finalize') then return result;end if;
 perform set_config('eios.tenant_id',c->>'tenant_id',true);
 select * into i from runtime.nexloop_effect_intents where intent_id=(p->>'intent_id')::uuid and tenant_id=c->>'tenant_id' and world=p_world;
 if not found then raise exception 'role intent unavailable' using errcode='42501';end if;
 select * into run from authz.nexloop_run_credentials where run_id=i.origin_run_id;
 select * into b from authz.nexloop_role_run_bindings where run_id=i.origin_run_id;
 if not found then return result;end if;
 env:=c->'role_envelope';
 if jsonb_typeof(env) is distinct from 'object' then raise exception 'role READ mandatory' using errcode='42501';end if;
 checked:=authz.nexloop_role_run_current(run.source_digest,p_world,env->>'text',env->>'signature',env->>'payload');
 if checked->'binding'->>'run_id' is distinct from i.origin_run_id::text then raise exception 'role Run mismatch' using errcode='42501';end if;
 return result;
end $$;
alter function authz.nexloop_effect_execution_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_execution_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_effect_execution_command(text,text,text,text,text) to nexloop_action_worker;

create table runtime.nexloop_role_trigger_events (
 event_id uuid not null,run_id uuid primary key references authz.nexloop_run_credentials(run_id),
 tenant_id text not null,world text not null,source_principal text not null,source_digest text not null,
 body text not null check(length(body) between 1 and 8192),command_binding jsonb not null,created_at timestamptz not null,
 unique(tenant_id,world,event_id)
);
create table runtime.nexloop_role_context_artifacts (
 run_id uuid primary key references runtime.nexloop_role_trigger_events(run_id),tenant_id text not null,world text not null,
 source_principal text not null,source_digest text not null,context_id uuid not null,artifact_id text not null,
 namespace text not null,pack_text text not null,pack_digest text not null,command_binding jsonb not null,created_at timestamptz not null
);
alter table runtime.nexloop_role_trigger_events owner to nexloop_owner;
alter table runtime.nexloop_role_context_artifacts owner to nexloop_owner;
alter table runtime.nexloop_role_trigger_events enable row level security;
alter table runtime.nexloop_role_trigger_events force row level security;
alter table runtime.nexloop_role_context_artifacts enable row level security;
alter table runtime.nexloop_role_context_artifacts force row level security;
create policy role_trigger_tenant on runtime.nexloop_role_trigger_events to nexloop_owner using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy role_context_tenant on runtime.nexloop_role_context_artifacts to nexloop_owner using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_role_trigger_events,runtime.nexloop_role_context_artifacts from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

create function authz.nexloop_role_context_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on set timezone='UTC' as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;command jsonb:=p->'command';k bytea;ident jsonb;tenant text;principal text;
 run authz.nexloop_run_credentials;e runtime.nexloop_role_trigger_events;b runtime.nexloop_role_context_artifacts;
 pub control.nexloop_action_definitions;proof jsonb;role jsonb;catalog jsonb;env jsonb;ctx control.nexloop_effect_contexts;
 plan control.nexloop_effect_plan_bindings;ctl control.nexloop_effect_control_ledger;facts jsonb;snapshot jsonb;binding jsonb;
 artifact runtime.nexloop_local_artifacts;formal_name text;namespace text;chosen_artifact_id text;context_uuid uuid;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or p_text is null or p_payload is null
  or octet_length(p_text)>1048576 or octet_length(p_payload)>131072 or p->>'verb' is null or p->>'verb' not in('stage','snapshot','bind')
  or a->>'protocol' is distinct from 'nexloop-role-context-v1' or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or a->>'resource_id' is distinct from 'eios:action:nexloop.context.bind_role:1'
 then raise exception 'role context unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active for share;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-role-context-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then raise exception 'role context unavailable' using errcode='42501';end if;
 if a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Action proof expired' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb then raise exception 'Source required' using errcode='42501';end if;
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';perform set_config('eios.tenant_id',tenant,true);
 select * into pub from control.nexloop_action_definitions where tenant_id=tenant and world=p_world and resource_id=a->>'resource_id' and active for share;
 if not found or pub.definition is distinct from a->'definition' or pub.capability is distinct from a->'capability'
  or pub.definition->'governance'->>'approval_mode' is distinct from 'none' or pub.definition->'governance'->>'risk_level' is distinct from 'low'
  or pub.definition->'input_schema' is distinct from '{"type":"object","properties":{"run_id":{"type":"string","format":"uuid"},"trigger_event_id":{"type":"string","format":"uuid"},"body":{"type":"string","minLength":1,"maxLength":8192}},"required":["run_id","trigger_event_id","body"],"additionalProperties":false}'::jsonb
  or pub.definition->'parameters' is distinct from '[]'::jsonb or pub.definition->'preconditions' is distinct from '[]'::jsonb
  or pub.definition->'governance'->'policy_refs' is distinct from '[]'::jsonb
 then raise exception 'role context contract unavailable' using errcode='42501';end if;
 select * into run from authz.nexloop_run_credentials where run_id=(command->>'run_id')::uuid for share;
 if not found or run.source_digest is distinct from p_digest or run.world is distinct from p_world then raise exception 'role Source Run mismatch' using errcode='42501';end if;
 perform authz.nexloop_service_identity_snapshot(run.token_digest,p_world);
 if jsonb_typeof(a->'run_proofs') is distinct from 'array' or jsonb_array_length(a->'run_proofs')<1 then raise exception 'Run proof required' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(a->'run_proofs') loop if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Run proof expired' using errcode='42501';end if;perform authz.nexloop_assert_action_authority(run.token_digest,p_world,proof);end loop;
 env:=a->'role_envelope';role:=authz.nexloop_role_run_current(p_digest,p_world,env->>'text',env->>'signature',env->>'payload');
 if role->'binding'->>'run_id' is distinct from run.run_id::text or command->>'role_ref' is distinct from role->'binding'->>'role_ref'
  or command->>'consumer_ref' is distinct from 'consumer:'||(role->'binding'->>'consumer_id') or command->>'tenant_id' is distinct from tenant
  or command->>'world_id' is distinct from p_world or command->>'mode' is distinct from 'real' or command->>'credential_ref' is distinct from 'run:'||run.run_id::text
  or jsonb_typeof(p->'body') is distinct from 'string' or length(p->>'body') not between 1 and 8192
  then raise exception 'role trigger unavailable' using errcode='42501';end if;
 binding:=authz.nexloop_context_command_binding(command);
 if binding is distinct from (p->>'command_binding_text')::jsonb or p->>'command_binding_digest' is distinct from encode(sha256(convert_to(p->>'command_binding_text','UTF8')),'hex') then raise exception 'role command binding unavailable' using errcode='42501';end if;
 if p->>'verb'='stage' then
  insert into runtime.nexloop_role_trigger_events values((command->>'trigger_event_id')::uuid,run.run_id,tenant,p_world,principal,p_digest,p->>'body',binding,clock_timestamp()) on conflict do nothing;
 end if;
 select * into e from runtime.nexloop_role_trigger_events where run_id=run.run_id for share;
 if not found or e.source_digest is distinct from p_digest or e.tenant_id is distinct from tenant or e.world is distinct from p_world
  or e.source_principal is distinct from principal or e.event_id is distinct from (command->>'trigger_event_id')::uuid
  or e.body is distinct from p->>'body' or e.command_binding is distinct from binding then raise exception 'durable role trigger conflict' using errcode='23505';end if;
 if p->>'verb'='stage' then
  if a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Action proof expired' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
  perform authz.nexloop_role_run_current(p_digest,p_world,env->>'text',env->>'signature',env->>'payload');
  for proof in select value from jsonb_array_elements(a->'run_proofs') loop if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Run proof expired' using errcode='42501';end if;perform authz.nexloop_assert_action_authority(run.token_digest,p_world,proof);end loop;
  return jsonb_build_object('event_id',e.event_id,'run_id',e.run_id,'persisted',true,'source_kind','service');
 end if;
 select context_id into context_uuid from control.nexloop_effect_run_contexts where run_id=run.run_id;
 perform authz.nexloop_assert_effect_plan(context_uuid,tenant,p_world);
 select * into plan from control.nexloop_effect_plan_bindings where context_id=context_uuid;
 select * into ctx from control.nexloop_effect_contexts where context_id=context_uuid;
 select * into ctl from control.nexloop_effect_control_ledger where control_id=plan.control_id;
 if plan.step_id is distinct from role->'binding'->>'step_id' or plan.step_revision::text is distinct from role->'binding'->>'step_revision'
  or command->>'goal_version_ref' is distinct from 'goal:'||plan.goal_id||':revision:'||plan.goal_revision||':step:'||plan.step_revision||':control:'||plan.control_revision
  then raise exception 'role shared Plan mismatch' using errcode='42501';end if;
 env:=a->'catalog_envelope';catalog:=authz.nexloop_service_catalog_scope(p_digest,p_world,env->>'text',env->>'signature',env->>'payload');
 if catalog->'allowed' is distinct from 'true'::jsonb or (env->>'payload')::jsonb->>'consumer_id' is distinct from ctx.consumer_id then raise exception 'role supply unavailable' using errcode='42501';end if;

 if jsonb_typeof(a->'formal_reads') is distinct from 'object' or (select count(*) from jsonb_object_keys(a->'formal_reads'))<>4 or not(a->'formal_reads' ?& array['Consumer','Goal','PlanStep','EffectControl']) then raise exception 'formal Source READ required' using errcode='42501';end if;
 for formal_name,env in select key,value from jsonb_each(a->'formal_reads') loop
  proof:=(env->>'text')::jsonb;
  if proof->>'type_name' is distinct from formal_name or proof->>'object_id' is distinct from (case proof->>'type_name' when 'Consumer' then ctx.consumer_id when 'Goal' then plan.goal_id when 'PlanStep' then plan.step_id when 'EffectControl' then plan.control_id else null end)
   or proof->'fields' is distinct from (case when proof->>'type_name'='EffectControl' then '["allow_effect","budget_units","executor_principal","valid_until"]'::jsonb else '[]'::jsonb end) then raise exception 'formal Source READ mismatch' using errcode='42501';end if;
  perform authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');
 end loop;
 for env in select value from jsonb_each((a->'catalog_envelope'->>'payload')::jsonb->'reads') loop perform authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');end loop;
 namespace:=encode(sha256(convert_to(tenant,'UTF8')||decode('00','hex')||convert_to(p_world,'UTF8')),'hex');
 if (p->>'artifact_identity_text')::jsonb is distinct from jsonb_build_array(tenant,p_world,principal,'context:'||run.run_id::text) then raise exception 'role artifact identity mismatch' using errcode='42501';end if;
 chosen_artifact_id:=substr(encode(sha256(convert_to(p->>'artifact_identity_text','UTF8')),'hex'),1,32);
 select jsonb_agg(jsonb_build_object('type',o.type_name,'id',o.object_id,'revision',o.nexloop_revision,'provenance','eios:object:'||o.object_id) order by o.type_name) into facts
 from ontology.objects o where o.tenant_id=tenant and o.world=p_world and o.object_id in(ctx.consumer_id,plan.goal_id,plan.step_id,plan.control_id);
 snapshot:=jsonb_build_object('schema_version','nexloop.context-pack.v3',
  'bindings',jsonb_build_object('tenant_id',tenant,'world_id',p_world,'run_id',run.run_id,'source_principal',principal,'context_id',context_uuid,'namespace',namespace,'artifact_id',chosen_artifact_id,'command_digest',p->>'command_binding_digest'),
  'trigger_statement',jsonb_build_object('kind','service_trigger','event_id',e.event_id,'source_principal',e.source_principal,'body',e.body,'provenance','eios:role-trigger:'||e.event_id::text),
  'formal_facts',facts,'role_binding',role,'supply',catalog->'supply',
  'current_constraints',jsonb_build_object('action','nexloop.service.request:1','allow_effect',true,'budget_units',ctl.budget_units,'reserved_units',ctl.reserved_units,'valid_until',least(ctx.valid_until,ctl.valid_until,run.expires_at),'executor_principal',ctl.executor_principal));
 if p->>'verb'='bind' then
  if p->>'pack_text' is null or octet_length(p->>'pack_text')>65536 or (p->>'pack_text')::jsonb is distinct from snapshot
   or p->>'pack_digest' is distinct from encode(sha256(convert_to(p->>'pack_text','UTF8')),'hex') then raise exception 'role pack snapshot mismatch' using errcode='42501';end if;
  if jsonb_typeof(a->'artifact_proofs') is distinct from 'array' or jsonb_array_length(a->'artifact_proofs')<>2
   or (select jsonb_agg(v->>'operation' order by v->>'operation') from jsonb_array_elements(a->'artifact_proofs') t(v)) is distinct from '["create","read"]'::jsonb then raise exception 'Artifact CREATE/READ required' using errcode='42501';end if;
  for proof in select value from jsonb_array_elements(a->'artifact_proofs') loop if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Artifact proof expired' using errcode='42501';end if;perform authz.nexloop_assert_artifact_authority(p_digest,p_world,proof);end loop;
  select * into artifact from runtime.nexloop_local_artifacts where tenant_id=tenant and world=p_world and runtime.nexloop_local_artifacts.artifact_id=chosen_artifact_id for share;
  if not found or artifact.status is distinct from 'available' or artifact.principal_id is distinct from principal or artifact.sha256 is distinct from p->>'pack_digest'
   or artifact.size_bytes is distinct from octet_length(p->>'pack_text') or artifact.object_key is distinct from namespace||'/'||chosen_artifact_id
   or (artifact.retention_until is null or artifact.retention_until<=clock_timestamp()) then raise exception 'role Artifact unavailable' using errcode='42501';end if;
  insert into runtime.nexloop_role_context_artifacts values(run.run_id,tenant,p_world,principal,p_digest,context_uuid,chosen_artifact_id,namespace,p->>'pack_text',p->>'pack_digest',binding,clock_timestamp()) on conflict do nothing;
  select * into b from runtime.nexloop_role_context_artifacts where run_id=run.run_id;
  if b.pack_text is distinct from p->>'pack_text' or b.command_binding is distinct from binding then raise exception 'role Artifact conflict' using errcode='23505';end if;
 end if;
 if a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Action proof expired' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 env:=a->'role_envelope';perform authz.nexloop_role_run_current(p_digest,p_world,env->>'text',env->>'signature',env->>'payload');
 env:=a->'catalog_envelope';perform authz.nexloop_service_catalog_scope(p_digest,p_world,env->>'text',env->>'signature',env->>'payload');
 if p->>'verb'='bind' then
  for proof in select value from jsonb_array_elements(a->'artifact_proofs') loop if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Artifact proof expired' using errcode='42501';end if;perform authz.nexloop_assert_artifact_authority(p_digest,p_world,proof);end loop;
  if (artifact.retention_until is null or artifact.retention_until<=clock_timestamp()) then raise exception 'role Artifact expired' using errcode='42501';end if;
 end if;

 if jsonb_typeof(a->'formal_reads') is distinct from 'object' or (select count(*) from jsonb_object_keys(a->'formal_reads'))<>4 or not(a->'formal_reads' ?& array['Consumer','Goal','PlanStep','EffectControl']) then raise exception 'formal Source READ required' using errcode='42501';end if;
 for formal_name,env in select key,value from jsonb_each(a->'formal_reads') loop
  proof:=(env->>'text')::jsonb;
  if proof->>'type_name' is distinct from formal_name or proof->>'object_id' is distinct from (case proof->>'type_name' when 'Consumer' then ctx.consumer_id when 'Goal' then plan.goal_id when 'PlanStep' then plan.step_id when 'EffectControl' then plan.control_id else null end)
   or proof->'fields' is distinct from (case when proof->>'type_name'='EffectControl' then '["allow_effect","budget_units","executor_principal","valid_until"]'::jsonb else '[]'::jsonb end) then raise exception 'formal Source READ mismatch' using errcode='42501';end if;
  perform authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');
 end loop;
 for env in select value from jsonb_each((a->'catalog_envelope'->>'payload')::jsonb->'reads') loop perform authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');end loop;
 for proof in select value from jsonb_array_elements(a->'run_proofs') loop if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Run proof expired' using errcode='42501';end if;perform authz.nexloop_assert_action_authority(run.token_digest,p_world,proof);end loop;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Action proof expired' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(a->'run_proofs') loop if (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Run proof expired' using errcode='42501';end if;end loop;
 if p->>'verb'='bind' then
  for proof in select value from jsonb_array_elements(a->'artifact_proofs') loop perform authz.nexloop_assert_artifact_authority(p_digest,p_world,proof);if (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Artifact proof expired' using errcode='42501';end if;end loop;
  if artifact.retention_until is null or artifact.retention_until<=clock_timestamp() then raise exception 'role Artifact expired' using errcode='42501';end if;
 end if;
 return snapshot;
end $$;
alter function authz.nexloop_role_context_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_context_command(text,text,text,text,text) from public,nexloop_scheduler,nexloop_domain_worker,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_role_context_command(text,text,text,text,text) to nexloop_api;

-- Typed managed Context-copy dependency discovery. Actual artifact row chooses
-- branch; callers cannot select a media/protocol or borrow the producer identity.
create function authz.nexloop_context_artifact_read_dependency_v2(p_digest text,p_world text,p_permit text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare r jsonb;b runtime.nexloop_role_context_artifacts;e runtime.nexloop_role_trigger_events;ident jsonb;pack jsonb;message_id text;refs jsonb;
begin
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 select * into b from runtime.nexloop_role_context_artifacts where tenant_id=ident->'binding'->>'tenant_id' and world=p_world and artifact_id=p_payload::jsonb->>'artifact_id';
 if not found then
  message_id:=authz.nexloop_context_artifact_read_dependency(p_digest,p_world,p_permit,p_payload);
  if message_id is null then return null;end if;
  return jsonb_build_object('kind','message','message_id',message_id);
 end if;
 r:=authz.nexloop_read_local_artifact_v0060(p_digest,p_world,p_permit,p_payload);
 if r->>'media_type' is distinct from 'application/vnd.nexloop.context+json' then raise exception 'role Context media unavailable' using errcode='42501';end if;
 select * into e from runtime.nexloop_role_trigger_events where run_id=b.run_id;
 pack:=b.pack_text::jsonb;
 if not found or b.tenant_id is distinct from ident->'binding'->>'tenant_id' or e.source_principal is distinct from ident->'binding'->>'subject_principal_id'
  or e.world is distinct from p_world or e.tenant_id is distinct from b.tenant_id
  or pack->>'schema_version' is distinct from 'nexloop.context-pack.v3'
  or pack->'trigger_statement' is distinct from jsonb_build_object('kind','service_trigger','event_id',e.event_id,'source_principal',e.source_principal,'body',e.body,'provenance','eios:role-trigger:'||e.event_id::text)
  or b.pack_digest is distinct from r->>'sha256' then raise exception 'role Context source unavailable' using errcode='42501';end if;
 refs:=jsonb_build_object(
  'role',jsonb_build_object('type_name','RoleDefinition','object_id',pack->'role_binding'->'binding'->>'role_id','fields','["active","ceiling_ref","name","responsibility","valid_from","valid_until"]'::jsonb),
  'link',jsonb_build_object('type_name','ConsumerRoleLink','object_id',pack->'role_binding'->'binding'->>'link_id','fields','["active","consumer_id","role_id","scope","valid_from","valid_until"]'::jsonb),
  'step',jsonb_build_object('type_name','PlanStep','object_id',pack->'role_binding'->'binding'->>'step_id','fields','["consumer_id","state","submitter_principals"]'::jsonb),
  'offering',jsonb_build_object('type_name','ServiceOffering','object_id',pack->'supply'->>'offering_id','fields','["active","allowed_discounts","allowed_guarantees","content_kind","currency","delivery_action","eligibility","evidence_kind","price_amount","service_code","title","valid_until"]'::jsonb),
  'offering_binding',jsonb_build_object('type_name','ConsumerServiceOffering','object_id',pack->'supply'->>'binding_id','fields','["active","consumer_id","offering_id","offering_revision","source_principal"]'::jsonb));
 refs:=refs||(select jsonb_object_agg('fact_'||(f->>'type'),jsonb_build_object('type_name',f->>'type','object_id',f->>'id','fields',
   case when f->>'type'='EffectControl' then '["allow_effect","budget_units","executor_principal","valid_until"]'::jsonb else '[]'::jsonb end)) from jsonb_array_elements(pack->'formal_facts') t(f));
 return jsonb_build_object('kind','role','context_version','nexloop.context-pack.v3','run_id',b.run_id,'event_id',e.event_id,'reads',refs,'artifact',r);
end $$;
alter function authz.nexloop_context_artifact_read_dependency_v2(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_artifact_read_dependency_v2(text,text,text,text) from public,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_context_artifact_read_dependency_v2(text,text,text,text) to nexloop_api,nexloop_domain_worker;

alter function authz.nexloop_read_local_artifact(text,text,text,text) rename to nexloop_read_local_artifact_before_roles_v0062;
revoke all on function authz.nexloop_read_local_artifact_before_roles_v0062(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_read_local_artifact(p_digest text,p_world text,p_permit text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare dep jsonb;r jsonb;d jsonb:=p_payload::jsonb->'context_dependency';name text;reference jsonb;env jsonb;a jsonb;row jsonb;proof jsonb;ident jsonb;artifact_proof jsonb;
begin
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 if not exists(select 1 from runtime.nexloop_role_context_artifacts where tenant_id=ident->'binding'->>'tenant_id' and world=p_world and artifact_id=p_payload::jsonb->>'artifact_id') then
  return authz.nexloop_read_local_artifact_before_roles_v0062(p_digest,p_world,p_permit,p_payload);
 end if;
 dep:=authz.nexloop_context_artifact_read_dependency_v2(p_digest,p_world,p_permit,p_payload);
 if dep is null or dep->>'kind'='message' then return authz.nexloop_read_local_artifact_before_roles_v0062(p_digest,p_world,p_permit,p_payload);end if;
 if dep->>'kind' is distinct from 'role' or jsonb_typeof(d) is distinct from 'object' or d->>'kind' is distinct from 'role'
  or d->>'run_id' is distinct from dep->>'run_id' or d->>'event_id' is distinct from dep->>'event_id'
  or jsonb_typeof(d->'reads') is distinct from 'object' or (select count(*) from jsonb_object_keys(d->'reads'))<>(select count(*) from jsonb_object_keys(dep->'reads'))
 then raise exception 'role Context source READ required' using errcode='42501';end if;
 for name,reference in select key,value from jsonb_each(dep->'reads') order by key loop
  env:=d->'reads'->name;a:=(env->>'text')::jsonb;
  if env->>'text' is null or env->>'signature' is null or a->>'type_name' is distinct from reference->>'type_name'
   or a->>'object_id' is distinct from reference->>'object_id' or a->'fields' is distinct from reference->'fields'
   or a->>'operation' is distinct from 'read' or a->>'expires_at' is null then raise exception 'role Context source READ required' using errcode='42501';end if;
  row:=authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');
 end loop;
 r:=dep->'artifact';
 -- Final actual reader/source dependency and all proofs after lock waits. Never
 -- use b.source_digest to supply another reader's permissions. Historic READ
 -- may retain evidence after role.end; execution independently requires active
 -- bound Role revisions through role_run_current, which is not this READ API.
 select claims into artifact_proof from authz.nexloop_artifact_permits where tenant_id=r->>'tenant_id' and permit_id=p_permit;
 if artifact_proof is null or artifact_proof->>'expires_at' is null or (artifact_proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'role Context artifact READ expired' using errcode='42501';end if;
 perform authz.nexloop_assert_artifact_authority(p_digest,p_world,artifact_proof);
 for name,reference in select key,value from jsonb_each(dep->'reads') order by key loop
  env:=d->'reads'->name;perform authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');
 end loop;
 if (artifact_proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'role Context artifact READ expired' using errcode='42501';end if;
 perform authz.nexloop_assert_artifact_authority(p_digest,p_world,artifact_proof);
 return r;
end $$;
alter function authz.nexloop_read_local_artifact(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_local_artifact(text,text,text,text) from public,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_read_local_artifact(text,text,text,text) to nexloop_api,nexloop_domain_worker;
