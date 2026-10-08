-- Service object/property reads using actual frozen EIOS authority facts.
create function authz.nexloop_assert_read_authority(p_digest text,p_world text,p_claims jsonb)
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
  or p_claims->>'operation' is distinct from 'read'
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
alter function authz.nexloop_assert_read_authority(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_read_authority(text,text,jsonb) from public;

create function authz.nexloop_read_object(p_digest text,p_world text,p_text text,p_signature text)
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
 return jsonb_build_object('object_id',r.object_id,'type_name',r.type_name,'world',r.world,'schema_version',r.schema_version,'properties',values);
end $$;
alter function authz.nexloop_read_object(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_object(text,text,text,text) from public;
grant execute on function authz.nexloop_read_object(text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;
