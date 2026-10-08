-- Registered role mapping constraints and CREATE commit tail; prior checksums immutable.
create function ontology.nexloop_role_mapping_constraints() returns trigger
language plpgsql set search_path=pg_catalog as $$
declare p jsonb:=new.properties; endpoint ontology.objects%rowtype;
begin
 if new.type_name not in ('RoleDefinition','ConsumerRoleLink') then return new;end if;
 if coalesce(p->>'valid_from','') !~ '(Z|\+00:00)$' or coalesce(p->>'valid_until','') !~ '(Z|\+00:00)$' then raise exception 'UTC-aware validity required' using errcode='22023';end if;
 if jsonb_typeof(p->'active') is distinct from 'boolean'
  or (p->>'valid_from')::timestamptz is null or (p->>'valid_until')::timestamptz is null
  or (p->>'valid_from')::timestamptz>=(p->>'valid_until')::timestamptz then raise exception 'role metadata invalid' using errcode='22023';end if;
 if new.type_name='RoleDefinition' then
  if coalesce(length(p->>'name'),0)=0 or coalesce(length(p->>'responsibility'),0)=0 or coalesce(length(p->>'ceiling_ref'),0)=0 then
   raise exception 'role definition incomplete' using errcode='22023';end if;
 else
  if coalesce(length(p->>'scope'),0)=0 then raise exception 'role scope missing' using errcode='22023';end if;
  if tg_op='UPDATE' and (p->>'consumer_id' is distinct from old.properties->>'consumer_id' or p->>'role_id' is distinct from old.properties->>'role_id') then
   raise exception 'role mapping endpoints immutable' using errcode='22023';end if;
  select * into endpoint from ontology.objects where tenant_id=new.tenant_id and world=new.world and object_id=p->>'consumer_id' and type_name='Consumer' for share;
  if not found then raise exception 'role mapping unavailable' using errcode='42501';end if;
  select * into endpoint from ontology.objects where tenant_id=new.tenant_id and world=new.world and object_id=p->>'role_id' and type_name='RoleDefinition' for share;
  if not found then raise exception 'role mapping unavailable' using errcode='42501';end if;
 end if;
 return new;
end $$;
alter function ontology.nexloop_role_mapping_constraints() owner to nexloop_owner;
revoke all on function ontology.nexloop_role_mapping_constraints() from public;
create trigger nexloop_role_mapping_constraints before insert or update on ontology.objects for each row execute function ontology.nexloop_role_mapping_constraints();
create unique index nexloop_consumer_role_pair on ontology.objects(tenant_id,world,(properties->>'consumer_id'),(properties->>'role_id')) where type_name='ConsumerRoleLink';

-- Append current CREATE tail; published 0010 remains unchanged.
create or replace function authz.nexloop_create_object_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;stored runtime.nexloop_action_claims%rowtype;
 permit jsonb:=a->'permit';v_claim jsonb:=permit->'claim';ref jsonb:=v_claim->'binding'->'action_reference';d jsonb;s jsonb;v_id text;v_outcome jsonb;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>262144 then raise exception 'instance command too large' using errcode='22023';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-object-create-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-object-create-v1'
  or a->>'resource_id' is distinct from 'eios:action:'||(ref->>'stable_name')||':'||(ref->>'version')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'instance permit rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=a->>'tenant_id' and world=p_world and resource_id=a->>'resource_id' and active;
 if not found or d is distinct from a->'definition' or d->'governance'->>'approval_mode'<>'none'
  or d->'governance'->>'risk_level'<>'low' or jsonb_array_length(d->'governance'->'policy_refs')<>0
  or d->'capability_binding'->>'capability_name' not in ('consumer.create','ontology.object.create')
  or not (d->'governance'->'change_scope'->'target_systems' @> '["postgres"]'::jsonb)
  or jsonb_array_length(d->'governance'->'change_scope'->'properties')<>0
 then raise exception 'instance Action contract unavailable' using errcode='42501';end if;

 if body->>'type_name' in ('RoleDefinition','ConsumerRoleLink') then
  if not (coalesce(d->'required_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
   or not exists(select 1 from control.nexloop_action_definitions ad where ad.tenant_id=a->>'tenant_id' and ad.world=p_world and ad.resource_id=a->>'resource_id' and ad.active and coalesce(ad.capability->'required_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
   or not (coalesce(authz.nexloop_service_identity_snapshot(p_digest,p_world)->'binding'->'requested_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
  then raise exception 'current role management scope required' using errcode='42501';end if;
 end if;
 if not exists(select 1 from jsonb_array_elements(d->'governance'->'change_scope'->'object_types') r
  where r->>'tenant_id'=a->>'tenant_id' and r->>'stable_name'=body->>'type_name' and (r->>'version')::integer=(a->'schema'->>'version')::integer) then
  raise exception 'instance type outside Action scope' using errcode='42501';end if;
 select definition into s from ontology.object_type_versions where tenant_id=a->>'tenant_id' and type_name=body->>'type_name' and version=(a->'schema'->>'version')::integer;
 if not found or s is distinct from a->'schema' or s->>'only_edit_via_actions'<>'true' then
  raise exception 'instance schema stale' using errcode='42501';end if;
 select * into stored from runtime.nexloop_action_claims where tenant_id=a->>'tenant_id' and world=p_world
  and action_name=v_claim->'key'->>'action_stable_name' and intent_id=v_claim->'key'->>'idempotency_key' for update;
 if not found or stored.principal_id is distinct from a->>'principal_id' or stored.claim is distinct from v_claim
  or v_claim->>'state'<>'active' or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or (permit->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'instance claim fenced' using errcode='40001';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 v_id:=encode(sha256(convert_to(jsonb_build_array(a->>'tenant_id',p_world,body->>'type_name',stored.intent_id)::text,'UTF8')),'hex');
 insert into ontology.objects(tenant_id,world,type_name,object_id,schema_version,properties,source_system,source_ref,created_at,updated_at)
  values(a->>'tenant_id',p_world,body->>'type_name',v_id,(s->>'version')::integer,body->'properties','nexloop-action',stored.intent_id,clock_timestamp(),clock_timestamp());
 v_outcome:=jsonb_build_object('outcome_id',v_id,'outcome_revision',1,'status','succeeded','outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;

 -- All writes and terminal claim remain provisional until current authority tail.
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=a->>'tenant_id' and world=p_world and resource_id=a->>'resource_id' and active for share;
 if not found or d is distinct from a->'definition' then raise exception 'create Action stale at commit' using errcode='42501';end if;
 select definition into s from ontology.object_type_versions where tenant_id=a->>'tenant_id' and type_name=body->>'type_name' and version=(a->'schema'->>'version')::integer for share;
 if not found or s is distinct from a->'schema' then raise exception 'create Schema stale at commit' using errcode='42501';end if;
 if not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active and key_material=k)
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp()
  or permit->>'expires_at' is null or (permit->>'expires_at')::timestamptz<=clock_timestamp()
  or v_claim->>'lease_expires_at' is null or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
 then raise exception 'create authority expired at commit' using errcode='42501';end if;

 if body->>'type_name' in ('RoleDefinition','ConsumerRoleLink') then
  if not (coalesce(d->'required_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
   or not exists(select 1 from control.nexloop_action_definitions ad where ad.tenant_id=a->>'tenant_id' and ad.world=p_world and ad.resource_id=a->>'resource_id' and ad.active and coalesce(ad.capability->'required_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
   or not (coalesce(authz.nexloop_service_identity_snapshot(p_digest,p_world)->'binding'->'requested_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
  then raise exception 'current role management scope required' using errcode='42501';end if;
 end if;
 return jsonb_build_object('object_id',v_id,'type_name',body->>'type_name','world',p_world);
end $$;
alter function authz.nexloop_create_object_action(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_create_object_action(text,text,text,text,text) from public;
grant execute on function authz.nexloop_create_object_action(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

-- Apply explicit management scope without altering published 0054.
-- Candidate append-only commit-tail protection; published 0015/0053 immutable.
create or replace function authz.nexloop_edit_object_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;stored runtime.nexloop_action_claims%rowtype;
 permit jsonb:=a->'permit';v_claim jsonb:=permit->'claim';ref jsonb:=v_claim->'binding'->'action_reference';d jsonb;s jsonb;v_id text;v_outcome jsonb;v_object ontology.objects%rowtype;field text;proof jsonb;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>262144 then raise exception 'instance command too large' using errcode='22023';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-object-edit-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-object-edit-v1'
  or a->>'resource_id' is distinct from 'eios:action:'||(ref->>'stable_name')||':'||(ref->>'version')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'instance permit rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=a->>'tenant_id' and world=p_world and resource_id=a->>'resource_id' and active;
 if not found or d is distinct from a->'definition' or d->'governance'->>'approval_mode'<>'none'
  or d->'governance'->>'risk_level'<>'low' or jsonb_array_length(d->'governance'->'policy_refs')<>0
  or d->'capability_binding'->>'capability_name' not in ('consumer.edit','ontology.object.edit')
  or not (d->'governance'->'change_scope'->'target_systems' @> '["postgres"]'::jsonb)
  or jsonb_array_length(d->'governance'->'change_scope'->'properties')<>0
 then raise exception 'instance Action contract unavailable' using errcode='42501';end if;

 if body->>'type_name' in ('RoleDefinition','ConsumerRoleLink') then
  if not (coalesce(d->'required_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
   or not exists(select 1 from control.nexloop_action_definitions ad where ad.tenant_id=a->>'tenant_id' and ad.world=p_world and ad.resource_id=a->>'resource_id' and ad.active and coalesce(ad.capability->'required_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
   or not (coalesce(authz.nexloop_service_identity_snapshot(p_digest,p_world)->'binding'->'requested_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
  then raise exception 'current role management scope required' using errcode='42501';end if;
 end if;
 if not exists(select 1 from jsonb_array_elements(d->'governance'->'change_scope'->'object_types') r
  where r->>'tenant_id'=a->>'tenant_id' and r->>'stable_name'=body->>'type_name' and (r->>'version')::integer=(a->'schema'->>'version')::integer) then
  raise exception 'instance type outside Action scope' using errcode='42501';end if;
 select definition into s from ontology.object_type_versions where tenant_id=a->>'tenant_id' and type_name=body->>'type_name' and version=(a->'schema'->>'version')::integer;
 if not found or s is distinct from a->'schema' or s->>'only_edit_via_actions'<>'true' then
  raise exception 'instance schema stale' using errcode='42501';end if;
 select * into stored from runtime.nexloop_action_claims where tenant_id=a->>'tenant_id' and world=p_world
  and action_name=v_claim->'key'->>'action_stable_name' and intent_id=v_claim->'key'->>'idempotency_key' for update;
 if not found or stored.principal_id is distinct from a->>'principal_id' or stored.claim is distinct from v_claim
  or v_claim->>'state'<>'active' or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or (permit->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'instance claim fenced' using errcode='40001';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 v_id:=body->>'object_id';
 if v_id !~ '^[a-f0-9]{64}$' or jsonb_typeof(body->'properties') is distinct from 'object'
  or (body->>'expected_revision')::bigint<1 or body->'properties'='{}'::jsonb
  or a->'object_authority'->>'target_resource' is distinct from 'eios:object:'||(body->>'type_name')||'/'||v_id then
  raise exception 'object edit target invalid' using errcode='42501';end if;
 perform authz.nexloop_assert_edit_authority(p_digest,p_world,a->'object_authority');
 for field in select jsonb_object_keys(body->'properties') order by 1 loop
  select value into proof from jsonb_array_elements(a->'property_authorities')
   where value->>'target_resource'='eios:property:'||(body->>'type_name')||'/'||v_id||'/'||field;
  if not found then raise exception 'property edit proof missing' using errcode='42501';end if;
  perform authz.nexloop_assert_edit_authority(p_digest,p_world,proof);
 end loop;
 select * into v_object from ontology.objects where tenant_id=a->>'tenant_id' and world=p_world
  and type_name=body->>'type_name' and object_id=v_id for update;
 if not found then raise exception 'object unavailable' using errcode='42501';end if;
 if v_object.schema_version<>(s->>'version')::integer or v_object.nexloop_revision<>(body->>'expected_revision')::bigint then
  raise exception 'object revision changed' using errcode='40001';end if;
 update ontology.objects set properties=properties||(body->'properties'),nexloop_revision=nexloop_revision+1,updated_at=clock_timestamp()
  where tenant_id=a->>'tenant_id' and world=p_world and type_name=body->>'type_name' and object_id=v_id;
 v_outcome:=jsonb_build_object('outcome_id',v_id,'outcome_revision',v_object.nexloop_revision+1,'status','succeeded','outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;
 -- Commit-tail validation: the UPDATE and terminal claim are provisional until all
 -- current proofs, original permit/lease and published contracts remain valid.
 if a->>'expires_at' is null or permit->>'expires_at' is null
  or v_claim->>'lease_expires_at' is null
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp()
  or permit->>'expires_at' is null or (permit->>'expires_at')::timestamptz<=clock_timestamp()
  or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
 then raise exception 'edit authority expired at commit' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform authz.nexloop_assert_edit_authority(p_digest,p_world,a->'object_authority');
 for field in select jsonb_object_keys(body->'properties') order by 1 loop
  select value into proof from jsonb_array_elements(a->'property_authorities')
   where value->>'target_resource'='eios:property:'||(body->>'type_name')||'/'||v_id||'/'||field;
  if not found or proof->>'expires_at' is null then raise exception 'edit property proof missing' using errcode='42501';end if;
  perform authz.nexloop_assert_edit_authority(p_digest,p_world,proof);
 end loop;
 select definition into d from control.nexloop_action_definitions where tenant_id=a->>'tenant_id'
  and world=p_world and resource_id=a->>'resource_id' and active for share;
 if not found or d is distinct from a->'definition' then raise exception 'edit Action contract changed' using errcode='42501';end if;
 select definition into s from ontology.object_type_versions where tenant_id=a->>'tenant_id'
  and type_name=body->>'type_name' and version=(a->'schema'->>'version')::integer for share;
 if not found or s is distinct from a->'schema' then raise exception 'edit schema changed' using errcode='42501';end if;
 if not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active and key_material=k)
  or a->'object_authority'->>'expires_at' is null
  or (a->'object_authority'->>'expires_at')::timestamptz<=clock_timestamp()
  or exists(select 1 from jsonb_array_elements(a->'property_authorities') p
     where p->>'expires_at' is null or (p->>'expires_at')::timestamptz<=clock_timestamp())
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp()
  or permit->>'expires_at' is null or (permit->>'expires_at')::timestamptz<=clock_timestamp()
  or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
 then raise exception 'edit authority expired at commit' using errcode='42501';end if;

 if body->>'type_name' in ('RoleDefinition','ConsumerRoleLink') then
  if not (coalesce(d->'required_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
   or not exists(select 1 from control.nexloop_action_definitions ad where ad.tenant_id=a->>'tenant_id' and ad.world=p_world and ad.resource_id=a->>'resource_id' and ad.active and coalesce(ad.capability->'required_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
   or not (coalesce(authz.nexloop_service_identity_snapshot(p_digest,p_world)->'binding'->'requested_scopes','[]'::jsonb) @> '["ontology.roles.manage"]'::jsonb)
  then raise exception 'current role management scope required' using errcode='42501';end if;
 end if;
 return jsonb_build_object('object_id',v_id,'type_name',body->>'type_name','world',p_world,'revision',v_object.nexloop_revision+1);
end $$;
