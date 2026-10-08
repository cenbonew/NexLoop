-- Service object/property reads using actual frozen EIOS authority facts.
create function authz.nexloop_assert_edit_authority(p_digest text,p_world text,p_claims jsonb)
 returns jsonb language plpgsql security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_identity jsonb;v_entry jsonb;v_key text[];v_payload jsonb;v_snapshot jsonb;
begin
 -- Lock the credential through this short transaction, serializing revocation.
 perform 1 from authz.nexloop_service_credentials where token_digest=p_digest for share;
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if p_claims->>'tenant_id' is distinct from v_identity->'binding'->>'tenant_id'
  or p_claims->>'credential_id' is distinct from v_identity->'binding'->>'credential_id'
  or p_claims->>'principal_id' is distinct from v_identity->'binding'->>'subject_principal_id'
  or p_claims->>'world' is distinct from p_world
  or p_claims->>'directory_hash' is distinct from v_identity->>'directory_hash'
  or p_claims->>'resource_id' is distinct from p_claims->>'target_resource'
  or p_claims->>'operation' is distinct from 'edit'
  or (p_claims->>'expires_at')::timestamptz <= clock_timestamp()
  or jsonb_typeof(p_claims->'facts') is distinct from 'array'
  or jsonb_array_length(p_claims->'facts')<>12
 then raise exception 'read authority binding stale or invalid' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_claims->>'tenant_id',true);
 -- Fixed ordering prevents cross-request lock-order inversion.
 for v_entry in select value from jsonb_array_elements(p_claims->'facts') order by value->>'kind',(value->'key')::text loop
  select array_agg(value) into v_key from jsonb_array_elements_text(v_entry->'key');
  select payload into v_payload from authz.nexloop_authority_facts
   where tenant_id=p_claims->>'tenant_id' and fact_kind=v_entry->>'kind' and entity_key=v_key for share;
  if not found then raise exception 'read authority dependency missing' using errcode='42501';end if;
  v_snapshot:=authz.nexloop_load_authority_fact_snapshot(p_digest,p_world,v_entry->>'kind',v_key);
  if v_snapshot->>'record_hash' is distinct from v_entry->>'record_hash' then
   raise exception 'read authority revision changed' using errcode='42501';
  end if;
 end loop;
 -- Tenant changes and remove/restore cycles must not resurrect an old permit.
 -- Lock this realm row after fact locks (matching publisher lock ordering).
 perform 1 from control.nexloop_tenants where tenant_id=p_claims->>'tenant_id' for share;
 v_identity:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v_identity->>'directory_hash' is distinct from p_claims->>'directory_hash'
  or (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'read authority epoch changed' using errcode='42501';
 end if;
 return v_identity->'binding';
end $$;
alter function authz.nexloop_assert_edit_authority(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_edit_authority(text,text,jsonb) from public;

-- Generic protected Action instance adapter: no raw application DML.
alter table ontology.objects add column nexloop_revision bigint not null default 1 check(nexloop_revision>0);
create function authz.nexloop_edit_object_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
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
 update ontology.objects set properties=properties||body->'properties',nexloop_revision=nexloop_revision+1,updated_at=clock_timestamp()
  where tenant_id=a->>'tenant_id' and world=p_world and type_name=body->>'type_name' and object_id=v_id;
 v_outcome:=jsonb_build_object('outcome_id',v_id,'outcome_revision',v_object.nexloop_revision+1,'status','succeeded','outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;
 return jsonb_build_object('object_id',v_id,'type_name',body->>'type_name','world',p_world,'revision',v_object.nexloop_revision+1);
end $$;
alter function authz.nexloop_edit_object_action(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_edit_object_action(text,text,text,text,text) from public;
grant execute on function authz.nexloop_edit_object_action(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;

create or replace function authz.nexloop_read_object(p_digest text,p_world text,p_text text,p_signature text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;k bytea;r ontology.objects%rowtype;field text;proof jsonb;values jsonb:='{}'::jsonb;target text;
begin
 if octet_length(p_text)>1048576 or jsonb_typeof(a->'fields') is distinct from 'array' or jsonb_array_length(a->'fields')>64 then
  raise exception 'invalid object read' using errcode='22023';end if;
 target:='eios:object:'||(a->>'type_name')||'/'||(a->>'object_id');
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or a->>'protocol' is distinct from 'nexloop-object-read-v1'
  or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-object-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'resource_id' is distinct from target or a->>'target_resource' is distinct from target
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'object read authority rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 select * into r from ontology.objects where tenant_id=a->>'tenant_id' and world=p_world and type_name=a->>'type_name' and object_id=a->>'object_id' for share;
 if not found then raise exception 'object unavailable' using errcode='42501';end if;
 for field in select value from jsonb_array_elements_text(a->'fields') loop
  select value into proof from jsonb_array_elements(a->'property_authorities') where value->>'target_resource'='eios:property:'||r.type_name||'/'||r.object_id||'/'||field;
  if not found or not (r.properties ? field) then raise exception 'property unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
  values:=values||jsonb_build_object(field,r.properties->field);
 end loop;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 return jsonb_build_object('object_id',r.object_id,'type_name',r.type_name,'world',r.world,'schema_version',r.schema_version,'properties',values,'revision',r.nexloop_revision);
end $$;
alter function authz.nexloop_read_object(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_object(text,text,text,text) from public;
grant execute on function authz.nexloop_read_object(text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;
